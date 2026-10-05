import os
from datetime import UTC, datetime


class StaleMarketTickError(ValueError):
    pass


def market_tick_age_seconds(
    timestamp: datetime,
    *,
    now: datetime | None = None,
) -> float:
    current_time = now or datetime.now(UTC)
    return (current_time - timestamp.astimezone(UTC)).total_seconds()


def is_fresh_market_tick(
    timestamp: datetime,
    *,
    now: datetime | None = None,
) -> bool:
    try:
        max_age_seconds = float(os.environ.get("MT5_MARKET_MAX_TICK_AGE_SECONDS", "30"))
    except ValueError as exc:
        raise ValueError("MT5_MARKET_MAX_TICK_AGE_SECONDS must be numeric") from exc
    if max_age_seconds <= 0:
        raise ValueError("MT5_MARKET_MAX_TICK_AGE_SECONDS must be positive")
    age_seconds = market_tick_age_seconds(timestamp, now=now)
    return -5 <= age_seconds <= max_age_seconds


def require_fresh_market_tick(
    timestamp: datetime,
    *,
    now: datetime | None = None,
) -> None:
    if is_fresh_market_tick(timestamp, now=now):
        return
    age_seconds = market_tick_age_seconds(timestamp, now=now)
    max_age_seconds = os.environ.get("MT5_MARKET_MAX_TICK_AGE_SECONDS", "30")
    raise StaleMarketTickError(
        f"Market tick is stale or future-dated (age={age_seconds:.1f}s, "
        f"maximum={max_age_seconds}s)."
    )
