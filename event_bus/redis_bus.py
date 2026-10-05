import asyncio
import contextvars
import json
import logging
import os
import random
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from schemas.events import EventEnvelope

logger = logging.getLogger(__name__)

try:
    import fakeredis  # type: ignore[import-untyped]

    _FAKE_REDIS_AVAILABLE = True
except ImportError:
    _FAKE_REDIS_AVAILABLE = False
    fakeredis = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
_current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "redis_event_trace_id",
    default=None,
)


class RedisEventBusError(RuntimeError):
    """Raised when an event cannot be published or decoded."""


@contextmanager
def trace_id_context(trace_id: str) -> Iterator[None]:
    if not trace_id:
        raise ValueError("trace_id must not be empty")
    token = _current_trace_id.set(trace_id)
    try:
        yield
    finally:
        _current_trace_id.reset(token)


class RedisEventBus:
    def __init__(
        self,
        redis_url: str | None = None,
        *,
        publish_attempts: int = 3,
        retry_base_seconds: float = 0.25,
        retry_max_seconds: float = 5.0,
    ) -> None:
        if publish_attempts < 1:
            raise ValueError("publish_attempts must be at least 1")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("retry delays must be positive and max >= base")

        self._redis_url = redis_url or self._redis_url_from_environment()
        self._publish_attempts = publish_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._client: Redis | None = None
        self._closed = False

    @staticmethod
    def _redis_url_from_environment() -> str:
        configured_url = os.environ.get("REDIS_URL")
        if configured_url:
            return configured_url

        host = os.environ.get("REDIS_HOST", "localhost")
        port = os.environ.get("REDIS_PORT", "6379")
        password = os.environ.get("REDIS_PASSWORD")
        if password:
            escaped_password = quote(password, safe="")
            return f"redis://:{escaped_password}@{host}:{port}/0"
        return f"redis://{host}:{port}/0"

    def _make_client(self) -> Redis:
        url = self._redis_url
        use_fakeredis = os.environ.get("REDIS_USE_FAKEREDIS", "false").lower() in {
            "1",
            "true",
            "yes",
        }
        if use_fakeredis and _FAKE_REDIS_AVAILABLE:
            logger.debug("Using fakeredis for local URL %s", url)
            return fakeredis.FakeAsyncRedis.from_url(  # type: ignore[union-attr]
                url,
                decode_responses=True,
            )
        return Redis.from_url(
            url,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

    async def _get_client(self) -> Redis:
        if self._closed:
            raise RedisEventBusError("RedisEventBus is closed")
        if self._client is None:
            self._client = self._make_client()
        return self._client

    async def _reset_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _retry_delay(self, attempt: int) -> float:
        ceiling = min(
            self._retry_base_seconds * (2**attempt),
            self._retry_max_seconds,
        )
        return random.uniform(ceiling / 2, ceiling)

    @staticmethod
    def _payload_dict(payload: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(payload, BaseModel):
            values = payload.model_dump(mode="json")
        elif isinstance(payload, Mapping):
            values = dict(payload)
        else:
            raise TypeError("payload must be a Pydantic model or mapping")
        if not all(isinstance(key, str) for key in values):
            raise ValueError("event payload keys must be strings")
        return values

    async def publish(
        self,
        channel: str,
        payload: BaseModel | Mapping[str, Any],
        *,
        trace_id: str | None = None,
        event_type: str | None = None,
    ) -> int:
        """Publish a JSON event envelope, retrying after Redis connection failures."""
        if not channel:
            raise ValueError("channel must not be empty")

        values = self._payload_dict(payload)
        if trace_id is not None:
            resolved_trace_id = trace_id
        else:
            resolved_trace_id = _current_trace_id.get()
        if resolved_trace_id is None:
            resolved_trace_id = values.get("trace_id")
        if resolved_trace_id is None:
            resolved_trace_id = uuid4().hex
        if not isinstance(resolved_trace_id, str) or not resolved_trace_id:
            raise ValueError("trace_id must be a non-empty string")

        inferred_event_type = (
            payload.__class__.__name__
            if isinstance(payload, BaseModel)
            else channel
        )
        envelope = EventEnvelope(
            event_type=event_type if event_type is not None else inferred_event_type,
            trace_id=resolved_trace_id,
            payload=values,
        ).model_dump_json()

        for attempt in range(self._publish_attempts):
            try:
                client = await self._get_client()
                return int(await client.publish(channel, envelope))
            except RedisError as exc:
                logger.warning(
                    "Redis publish failed for channel %s (attempt %d/%d)",
                    channel,
                    attempt + 1,
                    self._publish_attempts,
                    exc_info=exc,
                )
                await self._reset_client()
                if attempt + 1 == self._publish_attempts:
                    raise RedisEventBusError(
                        f"Could not publish event to Redis channel {channel!r}"
                    ) from exc
                await asyncio.sleep(self._retry_delay(attempt))

        raise RedisEventBusError(f"Could not publish event to Redis channel {channel!r}")

    async def set_state(self, key: str, value: Mapping[str, Any]) -> None:
        if not key:
            raise ValueError("state key must not be empty")
        body = json.dumps(dict(value), separators=(",", ":"))
        for attempt in range(self._publish_attempts):
            try:
                client = await self._get_client()
                await client.set(key, body)
                return
            except RedisError as exc:
                logger.warning(
                    "Redis state write failed for key %s (attempt %d/%d)",
                    key,
                    attempt + 1,
                    self._publish_attempts,
                    exc_info=exc,
                )
                await self._reset_client()
                if attempt + 1 == self._publish_attempts:
                    raise RedisEventBusError(
                        f"Could not persist Redis state for {key!r}"
                    ) from exc
                await asyncio.sleep(self._retry_delay(attempt))

    async def get_state(self, key: str) -> dict[str, Any] | None:
        if not key:
            raise ValueError("state key must not be empty")
        value = None
        for attempt in range(self._publish_attempts):
            try:
                client = await self._get_client()
                value = await client.get(key)
                break
            except RedisError as exc:
                logger.warning(
                    "Redis state read failed for key %s (attempt %d/%d)",
                    key,
                    attempt + 1,
                    self._publish_attempts,
                    exc_info=exc,
                )
                await self._reset_client()
                if attempt + 1 == self._publish_attempts:
                    raise RedisEventBusError(
                        f"Could not read Redis state for {key!r}"
                    ) from exc
                await asyncio.sleep(self._retry_delay(attempt))
        if value is None:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RedisEventBusError(
                f"Invalid persisted Redis state for {key!r}"
            ) from exc
        if not isinstance(decoded, dict):
            raise RedisEventBusError(
                f"Persisted Redis state for {key!r} is not an object"
            )
        return decoded

    async def subscribe(self, channel: str) -> AsyncIterator[EventEnvelope]:
        """Yield events and resubscribe with backoff after Redis connection loss."""
        if not channel:
            raise ValueError("channel must not be empty")

        reconnect_attempt = 0
        while not self._closed:
            pubsub = None
            try:
                client = await self._get_client()
                pubsub = client.pubsub()
                await pubsub.subscribe(channel)

                while not self._closed:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if message is None or message.get("type") != "message":
                        continue

                    try:
                        event = EventEnvelope.model_validate_json(message["data"])
                    except (ValidationError, TypeError, ValueError) as exc:
                        raise RedisEventBusError(
                            f"Invalid event received on Redis channel {channel!r}"
                        ) from exc

                    reconnect_attempt = 0
                    with trace_id_context(event.trace_id):
                        yield event
            except RedisError:
                if self._closed:
                    return
                logger.exception(
                    "Redis subscription lost for channel %s; reconnecting",
                    channel,
                )
                await self._reset_client()
                await asyncio.sleep(self._retry_delay(reconnect_attempt))
                reconnect_attempt += 1
            finally:
                if pubsub is not None:
                    await pubsub.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._reset_client()

    async def __aenter__(self) -> Self:
        if self._closed:
            raise RedisEventBusError("RedisEventBus is closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
