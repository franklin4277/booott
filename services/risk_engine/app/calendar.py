import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from pydantic import ValidationError

from services.risk_engine.app.models import EconomicCalendarEvent

logger = logging.getLogger(__name__)


class CalendarUnavailableError(RuntimeError):
    pass


class EconomicCalendarClient:
    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        import os

        self.url = url if url is not None else os.environ.get("CALENDAR_API_URL", "")
        self.api_key = api_key if api_key is not None else os.environ.get("CALENDAR_API_KEY", "")
        self.timeout_seconds = timeout_seconds

    def _fetch_sync(self, start: datetime, end: datetime) -> list[EconomicCalendarEvent]:
        if not self.url:
            raise CalendarUnavailableError("CALENDAR_API_URL is not configured")
        if not self.url.startswith("https://"):
            raise CalendarUnavailableError("economic calendar endpoint must use HTTPS")

        parsed = urlsplit(self.url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update(
            {
                "from": start.astimezone(UTC).isoformat(),
                "to": end.astimezone(UTC).isoformat(),
            }
        )
        request_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
        )
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(request_url, headers=headers)

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read(1_000_001)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise CalendarUnavailableError("economic calendar request failed") from exc

        if len(raw_body) > 1_000_000:
            raise CalendarUnavailableError("economic calendar response exceeded 1 MB")

        try:
            body = json.loads(raw_body)
            items = body["events"] if isinstance(body, dict) else body
            if not isinstance(items, list):
                raise TypeError("events must be a list")
            return [EconomicCalendarEvent.model_validate(item) for item in items]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise CalendarUnavailableError(
                "economic calendar returned an invalid response"
            ) from exc

    async def events_between(
        self,
        start: datetime,
        end: datetime,
    ) -> list[EconomicCalendarEvent]:
        try:
            return await asyncio.to_thread(self._fetch_sync, start, end)
        except CalendarUnavailableError:
            raise
        except Exception as exc:
            logger.exception("Unexpected economic calendar failure")
            raise CalendarUnavailableError(
                "unexpected economic calendar failure"
            ) from exc

    async def has_high_impact_event(
        self,
        currencies: set[str],
        timestamp: datetime,
        *,
        embargo_minutes: int = 15,
    ) -> EconomicCalendarEvent | None:
        start = timestamp - timedelta(minutes=embargo_minutes)
        end = timestamp + timedelta(minutes=embargo_minutes)
        events = await self.events_between(start, end)
        normalized_currencies = {currency.upper() for currency in currencies}
        for event in events:
            event_time = event.timestamp.astimezone(UTC)
            if (
                event.impact.casefold() == "high"
                and event.currency.upper() in normalized_currencies
                and start <= event_time <= end
            ):
                return event
        return None
