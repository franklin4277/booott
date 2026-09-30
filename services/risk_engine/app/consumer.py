import asyncio
import json
import logging

from pydantic import ValidationError

from event_bus.redis_bus import RedisEventBus
from schemas.events import EventEnvelope
from schemas.messages import AIAnalysisResult, ExecutionReport, PatternSetupSignal
from services.risk_engine.app.engine import RiskEngine
from services.risk_engine.app.models import PortfolioSnapshot

logger = logging.getLogger(__name__)
PATTERN_CHANNEL = "signals.pattern"
AI_ANALYSIS_CHANNEL = "signals.ai"
PORTFOLIO_CHANNEL = "risk.portfolio"
EXECUTION_CHANNEL = "execution.reports"
SAFE_MODE_CHANNEL = "system.safe_mode"
KILL_SWITCH_CHANNEL = "system.kill_switch"


class RiskEventConsumer:
    def __init__(self, bus: RedisEventBus, engine: RiskEngine) -> None:
        self.bus = bus
        self.engine = engine

    async def process_event(self, event: EventEnvelope) -> None:
        try:
            if event.event_type == "PatternSetupSignal":
                signal = PatternSetupSignal.model_validate_json(
                    json.dumps(event.payload)
                )
                await self.engine.handle_pattern_signal(
                    signal.model_copy(update={"trace_id": event.trace_id})
                )
            elif event.event_type == "AIAnalysisResult":
                result = AIAnalysisResult.model_validate_json(
                    json.dumps(event.payload)
                )
                await self.engine.handle_ai_result(
                    result.model_copy(update={"trace_id": event.trace_id})
                )
            elif event.event_type == "PortfolioSnapshot":
                snapshot = PortfolioSnapshot.model_validate_json(
                    json.dumps(event.payload)
                )
                await self.engine.handle_portfolio(snapshot)
            elif event.event_type == "ExecutionReport":
                report = ExecutionReport.model_validate_json(json.dumps(event.payload))
                await self.engine.handle_execution_status(
                    str(report.order_id),
                    report.status.value,
                )
            elif event.event_type == "SafeModeState":
                enabled = event.payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("SafeModeState.enabled must be a boolean")
                self.engine.handle_safe_mode(enabled)
            elif event.event_type == "KillSwitchState":
                mode = event.payload.get("mode")
                if mode not in {"RUNNING", "NO_NEW_TRADES", "SAFE_MODE", "EMERGENCY_FLAT"}:
                    raise ValueError("KillSwitchState.mode is invalid")
                self.engine.handle_kill_switch(mode)
            else:
                logger.warning("Ignoring unsupported risk event type %s", event.event_type)
        except ValidationError:
            logger.exception(
                "Rejected malformed risk event event_id=%s type=%s",
                event.event_id,
                event.event_type,
            )

    async def run(self) -> None:
        async def consume(channel: str) -> None:
            async for event in self.bus.subscribe(channel):
                try:
                    await self.process_event(event)
                except Exception:
                    logger.exception(
                        "Risk event processing failed event_id=%s channel=%s",
                        event.event_id,
                        channel,
                    )

        await asyncio.gather(
            consume(PATTERN_CHANNEL),
            consume(AI_ANALYSIS_CHANNEL),
            consume(PORTFOLIO_CHANNEL),
            consume(EXECUTION_CHANNEL),
            consume(SAFE_MODE_CHANNEL),
            consume(KILL_SWITCH_CHANNEL),
        )
