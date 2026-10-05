import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from services.risk_engine.app.models import EconomicCalendarEvent

logger = logging.getLogger(__name__)
_MAX_CALENDAR_BYTES = 1_000_000
_MAX_CALENDAR_EVENTS = 10_000


class CalendarUnavailableError(RuntimeError):
    pass


class EconomicCalendarClient:
    """Reads the fresh MT5 economic-calendar snapshot written by its MQL5 bridge."""

    def __init__(
        self,
        file_path: str | Path | None = None,
        *,
        max_age_seconds: float | None = None,
    ) -> None:
        configured_path = (
            str(file_path)
            if file_path is not None
            else os.environ.get("MT5_CALENDAR_FILE", "")
        )
        if configured_path:
            self.file_path = Path(configured_path)
        else:
            app_data = os.environ.get("APPDATA")
            if not app_data:
                raise ValueError(
                    "Set MT5_CALENDAR_FILE or APPDATA for the MT5 calendar bridge."
                )
            self.file_path = (
                Path(app_data)
                / "MetaQuotes"
                / "Terminal"
                / "Common"
                / "Files"
                / "booott_economic_calendar.tsv"
            )
        self.max_age_seconds = (
            max_age_seconds
            if max_age_seconds is not None
            else float(os.environ.get("MT5_CALENDAR_MAX_AGE_SECONDS", "180"))
        )
        if self.max_age_seconds <= 0:
            raise ValueError("MT5_CALENDAR_MAX_AGE_SECONDS must be positive")

    def _read_sync(self) -> list[EconomicCalendarEvent]:
        try:
            size = self.file_path.stat().st_size
            if size > _MAX_CALENDAR_BYTES:
                raise CalendarUnavailableError(
                    "MT5 economic calendar snapshot exceeded 1 MB"
                )
            contents = self.file_path.read_text(encoding="utf-16")
        except CalendarUnavailableError:
            raise
        except OSError as exc:
            raise CalendarUnavailableError(
                "MT5 economic calendar snapshot is unavailable"
            ) from exc

        lines = contents.splitlines()
        if not lines or not lines[0].startswith("generated_at_utc="):
            raise CalendarUnavailableError(
                "MT5 economic calendar snapshot has an invalid header"
            )
        try:
            generated_at = datetime.fromtimestamp(
                int(lines[0].partition("=")[2]),
                UTC,
            )
        except (OverflowError, OSError, ValueError) as exc:
            raise CalendarUnavailableError(
                "MT5 economic calendar snapshot has an invalid generation time"
            ) from exc

        age_seconds = (datetime.now(UTC) - generated_at).total_seconds()
        if age_seconds < -30 or age_seconds > self.max_age_seconds:
            raise CalendarUnavailableError(
                "MT5 economic calendar snapshot is stale or future-dated "
                f"(age={age_seconds:.1f}s, maximum={self.max_age_seconds:.1f}s)"
            )

        events: list[EconomicCalendarEvent] = []
        try:
            for line_number, line in enumerate(lines[1:], start=2):
                if not line:
                    continue
                fields = line.split("\t", 4)
                if len(fields) != 5:
                    raise ValueError(f"invalid field count on line {line_number}")
                event_id, currency, impact, timestamp, title = fields
                event = EconomicCalendarEvent(
                    event_id=event_id,
                    currency=currency,
                    impact=impact,
                    timestamp=datetime.fromtimestamp(int(timestamp), UTC),
                    title=title,
                )
                events.append(event)
                if len(events) > _MAX_CALENDAR_EVENTS:
                    raise ValueError("event count exceeded the configured limit")
        except (OverflowError, OSError, TypeError, ValueError, ValidationError) as exc:
            raise CalendarUnavailableError(
                "MT5 economic calendar snapshot contains invalid event data"
            ) from exc
        return events

    async def events_between(
        self,
        start: datetime,
        end: datetime,
    ) -> list[EconomicCalendarEvent]:
        try:
            events = await asyncio.to_thread(self._read_sync)
        except CalendarUnavailableError:
            raise
        except Exception as exc:
            logger.exception("Unexpected MT5 economic calendar failure")
            raise CalendarUnavailableError(
                "unexpected MT5 economic calendar failure"
            ) from exc
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        return [
            event
            for event in events
            if start_utc <= event.timestamp.astimezone(UTC) <= end_utc
        ]

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
            if (
                event.impact.casefold() == "high"
                and event.currency.upper() in normalized_currencies
                and start <= event.timestamp.astimezone(UTC) <= end
            ):
                return event
        return None
