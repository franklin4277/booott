from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from schemas.base import StrictModel


class EventEnvelope(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    occurred_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any]
