import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


StateChangeCallback = Callable[[CircuitState], Awaitable[None]]


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_seconds: float = 300.0,
        clock=time.monotonic,
        on_state_change: StateChangeCallback | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be positive")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._on_state_change = on_state_change
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def recovery_remaining(self) -> float:
        if self._state != CircuitState.OPEN or self._opened_at is None:
            return 0.0
        return max(
            0.0,
            self.recovery_seconds - (self._clock() - self._opened_at),
        )

    async def _change_state(self, state: CircuitState) -> None:
        if self._state == state:
            return
        self._state = state
        if self._on_state_change is not None:
            await self._on_state_change(state)

    async def acquire_permission(self) -> None:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if self.recovery_remaining > 0:
                    raise CircuitOpenError("AI provider circuit is open")
                await self._change_state(CircuitState.HALF_OPEN)
                self._probe_in_flight = True
                return

            if self._state == CircuitState.HALF_OPEN:
                raise CircuitOpenError("AI provider recovery probe is already in flight")

    async def record_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None
            self._probe_in_flight = False
            await self._change_state(CircuitState.CLOSED)

    async def record_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            self._probe_in_flight = False
            if (
                self._state == CircuitState.HALF_OPEN
                or self._consecutive_failures >= self.failure_threshold
            ):
                self._opened_at = self._clock()
                await self._change_state(CircuitState.OPEN)
