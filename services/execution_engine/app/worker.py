import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from prometheus_client import Counter, Gauge
from pydantic import ValidationError

from event_bus.redis_bus import RedisEventBus
from schemas.events import EventEnvelope
from schemas.messages import ExecutionReport, ExecutionStatus, SignedOrderPayload
from services.reconciliation.app.mt5_client import MT5AdapterClient
from utils.security import verify_payload

logger = logging.getLogger(__name__)
APPROVED_ORDERS_CHANNEL = "orders.approved"
EXECUTION_REPORTS_CHANNEL = "execution.reports"
EXECUTION_ORDERS = Counter(
    "execution_orders_total",
    "Signed risk approvals received by the execution service.",
    ["outcome"],
)
EXECUTION_ADAPTER_UP = Gauge(
    "execution_mt5_adapter_up", "Whether the MT5 order adapter last responded."
)


class ExecutionWorker:
    def __init__(
        self,
        bus: RedisEventBus,
        mt5: MT5AdapterClient,
        *,
        paper_trading: bool | None = None,
        live_trading_enabled: bool | None = None,
    ) -> None:
        self.bus = bus
        self.mt5 = mt5
        self.paper_trading = (
            paper_trading
            if paper_trading is not None
            else os.environ.get("FEATURE_PAPER_TRADING", "true").lower()
            in {"1", "true", "yes"}
        )
        self.live_trading_enabled = (
            live_trading_enabled
            if live_trading_enabled is not None
            else os.environ.get("MT5_LIVE_TRADING_ENABLED", "false").lower()
            in {"1", "true", "yes"}
        )
        self.signing_secret = (
            os.environ.get("ORDER_SIGNING_SECRET")
            or os.environ.get("SECRET_KEY")
            or ""
        )
        if len(self.signing_secret.encode("utf-8")) < 32:
            raise ValueError(
                "ORDER_SIGNING_SECRET or SECRET_KEY must contain at least 32 bytes."
            )

    async def process_event(self, event: EventEnvelope) -> None:
        if event.event_type != "SignedOrderPayload":
            logger.warning(
                "Ignoring unsupported approved-order event %s",
                event.event_type,
            )
            return
        try:
            order = SignedOrderPayload.model_validate_json(json.dumps(event.payload))
        except (ValidationError, TypeError, ValueError):
            logger.exception("Rejected malformed approved order event %s", event.event_id)
            EXECUTION_ORDERS.labels(outcome="invalid").inc()
            return

        if order.trace_id != event.trace_id:
            logger.error("Approved order trace ID mismatch order=%s", order.order_id)
            await self._publish_report(
                order,
                ExecutionStatus.REJECTED,
                "Event trace ID does not match the signed order payload.",
            )
            EXECUTION_ORDERS.labels(outcome="rejected").inc()
            return
        if not verify_payload(order, self.signing_secret):
            await self._publish_report(
                order,
                ExecutionStatus.REJECTED,
                "HMAC signature is invalid; order was not sent to MT5.",
            )
            EXECUTION_ORDERS.labels(outcome="rejected").inc()
            return
        if self.paper_trading:
            await self._publish_report(
                order,
                ExecutionStatus.CANCELLED,
                "Paper-trading mode is active; order was recorded but not routed to a broker.",
            )
            EXECUTION_ORDERS.labels(outcome="paper").inc()
            return
        if not self.live_trading_enabled:
            await self._publish_report(
                order,
                ExecutionStatus.REJECTED,
                "Live execution is disabled; set MT5_LIVE_TRADING_ENABLED=true explicitly.",
            )
            EXECUTION_ORDERS.labels(outcome="disabled").inc()
            return

        try:
            safe_mode = await self.bus.get_state("system:safe_mode")
            kill_switch = await self.bus.get_state("system:kill_switch")
        except Exception:
            logger.exception("Cannot verify kill-switch state; refusing broker execution")
            await self._publish_report(
                order,
                ExecutionStatus.REJECTED,
                "Kill-switch state is unavailable; execution fails closed.",
            )
            EXECUTION_ORDERS.labels(outcome="rejected").inc()
            return
        if safe_mode is None or safe_mode.get("enabled", True):
            await self._publish_report(
                order,
                ExecutionStatus.REJECTED,
                "SAFE_MODE is enabled or unavailable; execution fails closed.",
            )
            EXECUTION_ORDERS.labels(outcome="rejected").inc()
            return
        if (kill_switch or {}).get("mode", "RUNNING") != "RUNNING":
            await self._publish_report(
                order,
                ExecutionStatus.REJECTED,
                "The global kill switch is active; broker execution was not attempted.",
            )
            EXECUTION_ORDERS.labels(outcome="rejected").inc()
            return
        if order.expires_at is None or order.expires_at <= datetime.now(UTC):
            await self._publish_report(
                order,
                ExecutionStatus.FAILED,
                "Signed order expired before reaching the MT5 execution service.",
            )
            EXECUTION_ORDERS.labels(outcome="expired").inc()
            return

        try:
            response = await self.mt5.client.post(
                f"{self.mt5.base_url.rstrip('/')}/v1/orders",
                json=order.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {self.mt5.token}"},
                timeout=10,
            )
            response.raise_for_status()
            report = ExecutionReport.model_validate(response.json())
            if report.order_id != order.order_id:
                raise ValueError("MT5 adapter returned a different order ID.")
            EXECUTION_ADAPTER_UP.set(1)
        except (httpx.HTTPError, ValidationError, ValueError):
            EXECUTION_ADAPTER_UP.set(0)
            logger.exception(
                "MT5 adapter order request failed order_id=%s",
                order.order_id,
            )
            await self._publish_report(
                order,
                ExecutionStatus.FAILED,
                "MT5 adapter could not confirm the broker execution outcome; inspect reconciliation before retrying.",
            )
            EXECUTION_ORDERS.labels(outcome="uncertain").inc()
            return

        await self.bus.publish(
            EXECUTION_REPORTS_CHANNEL,
            report,
            trace_id=event.trace_id,
            event_type="ExecutionReport",
        )
        EXECUTION_ORDERS.labels(outcome=report.status.value).inc()

    async def _publish_report(
        self,
        order: SignedOrderPayload,
        status: ExecutionStatus,
        message: str,
    ) -> None:
        report = ExecutionReport(
            order_id=order.order_id,
            status=status,
            executed_volume=Decimal(0),
            timestamp=datetime.now(UTC),
            message=message,
            trace_id=order.trace_id,
        )
        await self.bus.publish(
            EXECUTION_REPORTS_CHANNEL,
            report,
            trace_id=order.trace_id,
            event_type="ExecutionReport",
        )

    async def run(self) -> None:
        async for event in self.bus.subscribe(APPROVED_ORDERS_CHANNEL):
            try:
                await self.process_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Execution event failed event_id=%s",
                    event.event_id,
                )
