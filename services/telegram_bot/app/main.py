import asyncio
import logging
import os
import shlex
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    generate_latest,
)
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from database.models import Account, Trade
from database.session import create_database_engine
from event_bus.redis_bus import RedisEventBus, RedisEventBusError
from services.reconciliation.app.mt5_client import (
    MT5AdapterClient,
    MT5AdapterUnavailable,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EXECUTION_CHANNEL = "execution.reports"
RISK_REJECTION_CHANNEL = "risk.rejections"
SYSTEM_ALERT_CHANNEL = "system.alerts"
SAFE_MODE_CHANNEL = "system.safe_mode"
KILL_SWITCH_CHANNEL = "system.kill_switch"
AI_STATE_CHANNEL = "ai.state"
KILL_SWITCH_KEY = "system:kill_switch"
SAFE_MODE_KEY = "system:safe_mode"

ACCOUNT_EQUITY = Gauge("trading_account_equity", "Latest reconciled MT5 account equity.")
ACCOUNT_DRAWDOWN = Gauge(
    "trading_drawdown_percent", "Account drawdown from the daily starting equity."
)
HOST_CPU_PERCENT = Gauge("mt5_host_cpu_percent", "Windows host CPU utilization.")
HOST_MEMORY_PERCENT = Gauge(
    "mt5_host_memory_percent", "Windows host memory utilization."
)
WIN_RATE = Gauge("trading_win_rate_percent", "Closed-trade win rate percentage.")
MT5_CONNECTED = Gauge("trading_mt5_connected", "Whether the MT5 host bridge is reachable.")
REDIS_CONNECTED = Gauge("trading_redis_connected", "Whether Telegram can read shared Redis state.")
DATABASE_CONNECTED = Gauge(
    "trading_database_connected", "Whether the Telegram service can query its database."
)
SAFE_MODE_ENABLED = Gauge(
    "trading_safe_mode_enabled", "Whether the reconciled global SAFE_MODE is enabled."
)
KILL_SWITCH_ENABLED = Gauge(
    "trading_kill_switch_enabled", "Whether the global kill switch is latched."
)
SAFE_MODE_ENABLED.set(1)
KILL_SWITCH_ENABLED.set(0)
DB_CONNECTIONS = Gauge(
    "trading_db_connections",
    "Current SQLAlchemy database connections in the Telegram service pool.",
    ["state"],
)
NOTIFICATIONS = Counter(
    "telegram_notifications_total", "Telegram notification send outcomes.", ["status"]
)
_last_ai_state: dict[str, Any] = {}


class TelegramControlBot:
    def __init__(
        self,
        bus: RedisEventBus,
        mt5: MT5AdapterClient,
        *,
        token: str | None = None,
        allowed_user_ids: set[int] | None = None,
        notification_chat_ids: set[str] | None = None,
        client: httpx.AsyncClient | None = None,
        reconciliation_url: str | None = None,
        reconciliation_token: str | None = None,
    ) -> None:
        self.bus = bus
        self.mt5 = mt5
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.allowed_user_ids = (
            allowed_user_ids
            if allowed_user_ids is not None
            else self._parse_user_ids(os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))
        )
        configured_chats = (
            os.environ.get("TELEGRAM_NOTIFICATION_CHAT_IDS")
            or os.environ.get("TELEGRAM_CHAT_ID", "")
        )
        self.notification_chat_ids = (
            notification_chat_ids
            if notification_chat_ids is not None
            else {item.strip() for item in configured_chats.split(",") if item.strip()}
        )
        self.reconciliation_url = (
            reconciliation_url
            or os.environ.get("RECONCILIATION_URL", "http://localhost:8010")
        ).rstrip("/")
        self.reconciliation_token = (
            reconciliation_token
            or os.environ.get("RECONCILIATION_API_TOKEN", "")
        )
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(35, connect=5),
        )
        self._offset = 0
        self._last_snapshot_at: datetime | None = None
        self._last_drawdown = Decimal(0)
        self._drawdown_available = False
        self._database_healthy = False
        self._stop_polling = asyncio.Event()
        self.drawdown_warning_pct = Decimal(
            os.environ.get("TELEGRAM_DRAWDOWN_WARNING_PCT", "3.0")
        )
        if not Decimal(0) < self.drawdown_warning_pct <= Decimal(100):
            raise ValueError("TELEGRAM_DRAWDOWN_WARNING_PCT must be greater than 0 and at most 100")
        self.telemetry_interval_seconds = int(
            os.environ.get("TELEGRAM_STATUS_INTERVAL_SECONDS", "15")
        )
        if self.telemetry_interval_seconds < 5:
            raise ValueError("TELEGRAM_STATUS_INTERVAL_SECONDS must be at least 5")

    @staticmethod
    def _parse_user_ids(value: str) -> set[int]:
        try:
            user_ids = {
                int(item.strip()) for item in value.split(",") if item.strip()
            }
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must be comma-separated integers") from exc
        if any(user_id <= 0 for user_id in user_ids):
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain positive user IDs")
        return user_ids

    def _log_telegram_error(self, operation: str, exc: Exception) -> None:
        detail = str(exc).replace(self.token, "[REDACTED]") if self.token else str(exc)
        logger.error("Telegram %s failed (%s): %s", operation, type(exc).__name__, detail)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @property
    def commands_enabled(self) -> bool:
        return self.enabled and bool(self.allowed_user_ids)

    async def _telegram_call(self, method: str, payload: dict[str, Any]) -> Any:
        response = await self.client.post(
            f"https://api.telegram.org/bot{self.token}/{method}",
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError(f"Telegram API rejected {method} request")
        return body.get("result")

    async def send_message(self, chat_id: str | int, text: str) -> None:
        if not self.enabled:
            logger.warning("Telegram notification skipped because bot is not configured")
            NOTIFICATIONS.labels(status="disabled").inc()
            return
        try:
            await self._telegram_call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text[:4000],
                    "disable_web_page_preview": True,
                },
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            NOTIFICATIONS.labels(status="failed").inc()
            self._log_telegram_error("sendMessage", exc)
            return
        NOTIFICATIONS.labels(status="sent").inc()

    async def notify_event(self, channel: str, event: Any) -> None:
        if not self.notification_chat_ids:
            return
        payload = event.payload
        if channel == EXECUTION_CHANNEL:
            text = (
                "Trade execution\n"
                f"Status: {payload.get('status', 'unknown')}\n"
                f"Symbol: {payload.get('symbol', 'unknown')}\n"
                f"Order: {payload.get('order_id', 'unknown')}"
            )
        elif channel == RISK_REJECTION_CHANNEL:
            text = (
                "Risk warning\n"
                f"Code: {payload.get('code', 'unknown')}\n"
                f"Symbol: {payload.get('symbol', 'unknown')}\n"
                f"Reason: {payload.get('reason', 'unspecified')}"
            )
        elif channel == SAFE_MODE_CHANNEL:
            SAFE_MODE_ENABLED.set(1 if payload.get("enabled", True) else 0)
            text = (
                "Trading SAFE_MODE "
                + ("ENABLED" if payload.get("enabled", True) else "cleared")
                + f"\nReason: {payload.get('reason') or 'not provided'}"
            )
        elif channel == KILL_SWITCH_CHANNEL:
            KILL_SWITCH_ENABLED.set(1 if payload.get("enabled", True) else 0)
            text = f"Kill switch: {payload.get('mode', 'unknown')}"
        elif channel == AI_STATE_CHANNEL:
            _last_ai_state.clear()
            _last_ai_state.update(payload)
            text = (
                f"AI gateway: {payload.get('ai_state', 'unknown')} "
                f"({payload.get('circuit_state', 'unknown')})"
            )
        else:
            text = (
                f"System alert: {payload.get('event', event.event_type)}\n"
                f"Severity: {payload.get('severity', 'error')}\n"
                f"Details: {payload.get('reason') or payload.get('summary') or 'See system logs.'}"
            )
        for chat_id in self.notification_chat_ids:
            await self.send_message(chat_id, text)

    async def _set_kill_switch(self, mode: str, actor_id: int) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        state = {
            "mode": mode,
            "enabled": mode != "RUNNING",
            "actor_id": actor_id,
            "updated_at": now,
        }
        await self.bus.set_state(KILL_SWITCH_KEY, state)
        await self.bus.publish(
            KILL_SWITCH_CHANNEL,
            state,
            trace_id=f"telegram-{actor_id}-{int(datetime.now(UTC).timestamp())}",
            event_type="KillSwitchState",
        )
        return state

    async def _activate_safe_mode(self, reason: str) -> dict[str, Any]:
        if len(self.reconciliation_token.encode("utf-8")) < 32:
            raise RuntimeError("Reconciliation authorization is not configured.")
        response = await self.client.post(
            f"{self.reconciliation_url}/safe-mode/activate",
            params={"reason": reason},
            headers={"X-Reconciliation-Token": self.reconciliation_token},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("enabled") is not True:
            raise RuntimeError("Reconciliation did not confirm SAFE_MODE activation.")
        return result

    async def _flat_positions(self) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.mt5.base_url.rstrip('/')}/v1/emergency-flat",
            headers={"Authorization": self.mt5.token},
            timeout=60,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
            raise TypeError("MT5 adapter returned an invalid flatten result.")
        return result

    async def _status_text(self) -> str:
        try:
            snapshot = await self.mt5.get_account_status()
            self._last_snapshot_at = datetime.now(UTC)
            MT5_CONNECTED.set(1)
            account = snapshot.account
            if snapshot.host_cpu_percent is not None:
                HOST_CPU_PERCENT.set(float(snapshot.host_cpu_percent))
            if snapshot.host_memory_percent is not None:
                HOST_MEMORY_PERCENT.set(float(snapshot.host_memory_percent))
            equity_line = f"Equity: {account.equity} {account.currency}"
            if self._drawdown_available:
                equity_line += f"\nDaily drawdown: {self._last_drawdown:.2f}%"
            else:
                equity_line += "\nDaily drawdown: unavailable"
            connection = f"Connected (account {account.login})"
        except MT5AdapterUnavailable:
            MT5_CONNECTED.set(0)
            connection = "Disconnected"
            equity_line = "Equity/drawdown: unavailable"

        try:
            safe_mode = await self.bus.get_state(SAFE_MODE_KEY)
            kill_switch = await self.bus.get_state(KILL_SWITCH_KEY)
            redis_line = "healthy"
            REDIS_CONNECTED.set(1)
        except RedisEventBusError:
            safe_mode = None
            kill_switch = None
            redis_line = "unavailable"
            REDIS_CONNECTED.set(0)
        SAFE_MODE_ENABLED.set(
            1 if safe_mode is None or safe_mode.get("enabled", True) else 0
        )
        KILL_SWITCH_ENABLED.set(
            1 if kill_switch is not None and kill_switch.get("enabled", True) else 0
        )
        ai_state = _last_ai_state
        return (
            "Trading system status\n"
            f"Database: {'healthy' if self._database_healthy else 'unavailable'}\n"
            f"Redis: {redis_line}\n"
            f"MT5: {connection}\n"
            f"{equity_line}\n"
            f"SAFE_MODE: {'ON' if safe_mode is None or safe_mode.get('enabled', True) else 'OFF'}"
            + (
                f" ({safe_mode.get('reason') or 'unspecified'})"
                if safe_mode and safe_mode.get("enabled", True)
                else (" (state unavailable; fail-closed)" if safe_mode is None else "")
            )
            + f"\nKill switch: {(kill_switch or {}).get('mode', 'RUNNING')}"
            + f"\nAI gateway: {ai_state.get('ai_state', 'unknown')}"
        )

    async def _handle_command(self, message: dict[str, Any]) -> str | None:
        sender = message.get("from")
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return None
        user_id = sender.get("id")
        chat_id = chat.get("id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return None
        if user_id not in self.allowed_user_ids:
            logger.warning("Ignoring Telegram control command from unauthorized user")
            return None
        if chat.get("type") != "private":
            return "For security, use bot controls in a private chat."
        if not isinstance(text, str):
            return None
        try:
            parts = shlex.split(text.strip())
        except ValueError:
            return "Invalid command syntax."
        if not parts:
            return None
        command = parts[0].split("@", 1)[0].casefold()
        if command == "/status" and len(parts) == 1:
            return await self._status_text()
        if command == "/resume" and len(parts) == 1:
            current = await self.bus.get_state(KILL_SWITCH_KEY)
            mode = (current or {}).get("mode", "RUNNING")
            if mode in {"SAFE_MODE", "EMERGENCY_FLAT"}:
                safe_mode = await self.bus.get_state(SAFE_MODE_KEY)
                if safe_mode is None or safe_mode.get("enabled", True):
                    return "Resume refused: clear SAFE_MODE through the authenticated reconciliation procedure first."
                await self._set_kill_switch("RUNNING", user_id)
                return "Reconciliation cleared SAFE_MODE; the global kill-switch latch is now released."
            if mode != "NO_NEW_TRADES":
                return "Resume refused: no Telegram-releasable kill switch is active."
            await self._set_kill_switch("RUNNING", user_id)
            return "NO_NEW_TRADES cleared. Trading may resume only if SAFE_MODE is also clear."
        if command != "/kill" or len(parts) != 2:
            return "Commands: /status, /kill NO_NEW_TRADES, /kill SAFE_MODE, /kill EMERGENCY_FLAT, /resume"

        mode = parts[1].upper()
        if mode == "NO_NEW_TRADES":
            await self._set_kill_switch(mode, user_id)
            return "NO_NEW_TRADES enabled. Existing positions are unchanged. Use /resume to release this latch."
        if mode == "SAFE_MODE":
            await self._activate_safe_mode(
                f"Telegram SAFE_MODE command from authorized operator {user_id}."
            )
            await self._set_kill_switch(mode, user_id)
            return "SAFE_MODE enabled and persisted. A successful reconciliation and manual clear are required to resume."
        if mode == "EMERGENCY_FLAT":
            await self._activate_safe_mode(
                f"Telegram EMERGENCY_FLAT command from authorized operator {user_id}."
            )
            await self._set_kill_switch(mode, user_id)
            result = await self._flat_positions()
            return (
                "EMERGENCY_FLAT completed and broker state is flat."
                if result["success"]
                else "CRITICAL: SAFE_MODE is latched, but MT5 reports open positions or pending orders remain."
            )
        return "Unknown mode. Choose NO_NEW_TRADES, SAFE_MODE, or EMERGENCY_FLAT."

    async def poll_commands(self) -> None:
        if not self.enabled:
            logger.warning("Telegram bot is disabled: TELEGRAM_BOT_TOKEN is not set")
            return
        if not self.allowed_user_ids:
            logger.error(
                "Telegram command polling is disabled: TELEGRAM_ALLOWED_USER_IDS is empty"
            )
            return
        while not self._stop_polling.is_set():
            try:
                result = await self._telegram_call(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": ["message"],
                    },
                )
                if not isinstance(result, list):
                    raise TypeError("Telegram getUpdates returned an invalid result.")
                for update in result:
                    if not isinstance(update, dict):
                        continue
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = max(self._offset, update_id + 1)
                    message = update.get("message")
                    if not isinstance(message, dict):
                        continue
                    try:
                        reply = await self._handle_command(message)
                    except (httpx.HTTPError, RuntimeError, TypeError, ValueError):
                        logger.exception("Telegram operator command failed")
                        chat = message.get("chat")
                        reply = (
                            "Command failed. Check reconciliation/MT5 health and operator logs."
                            if isinstance(chat, dict)
                            else None
                        )
                    if reply is not None:
                        chat = message.get("chat")
                        if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                            await self.send_message(chat["id"], reply)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                self._log_telegram_error("polling (retrying shortly)", exc)
                await asyncio.sleep(3)

    async def notifications_worker(self) -> None:
        channels = (
            EXECUTION_CHANNEL,
            RISK_REJECTION_CHANNEL,
            SYSTEM_ALERT_CHANNEL,
            SAFE_MODE_CHANNEL,
            KILL_SWITCH_CHANNEL,
            AI_STATE_CHANNEL,
        )

        async def consume(channel: str) -> None:
            async for event in self.bus.subscribe(channel):
                try:
                    await self.notify_event(channel, event)
                except Exception:
                    logger.exception("Failed to handle notification on %s", channel)

        await asyncio.gather(*(consume(channel) for channel in channels))

    async def telemetry_worker(self, engine) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        warning_threshold = self.drawdown_warning_pct
        interval = self.telemetry_interval_seconds
        warning_active = False
        adapter_was_available: bool | None = None
        database_was_healthy: bool | None = None
        while not self._stop_polling.is_set():
            try:
                safe_mode = await self.bus.get_state(SAFE_MODE_KEY)
                kill_switch = await self.bus.get_state(KILL_SWITCH_KEY)
                REDIS_CONNECTED.set(1)
                SAFE_MODE_ENABLED.set(
                    1 if safe_mode is None or safe_mode.get("enabled", True) else 0
                )
                KILL_SWITCH_ENABLED.set(
                    1
                    if kill_switch is not None
                    and kill_switch.get("enabled", True)
                    else 0
                )
            except RedisEventBusError:
                REDIS_CONNECTED.set(0)
                SAFE_MODE_ENABLED.set(1)
                KILL_SWITCH_ENABLED.set(1)
            try:
                snapshot = await self.mt5.get_account_status()
            except asyncio.CancelledError:
                raise
            except MT5AdapterUnavailable:
                MT5_CONNECTED.set(0)
                logger.warning("Telegram telemetry cannot reach the MT5 adapter")
                if adapter_was_available is not False:
                    await self.send_notification(
                        "System error: MT5 account-state adapter is unavailable."
                    )
                adapter_was_available = False
                snapshot = None
            else:
                if adapter_was_available is False:
                    await self.send_notification(
                        "System recovery: MT5 account-state adapter is responding again."
                    )
                adapter_was_available = True
            if snapshot is not None:
                MT5_CONNECTED.set(1)
                self._last_snapshot_at = datetime.now(UTC)
                if snapshot.host_cpu_percent is not None:
                    HOST_CPU_PERCENT.set(float(snapshot.host_cpu_percent))
                if snapshot.host_memory_percent is not None:
                    HOST_MEMORY_PERCENT.set(float(snapshot.host_memory_percent))
                drawdown_notification: str | None = None
                try:
                    async with sessions() as session:
                        account_result = await session.execute(
                            select(Account).where(
                                Account.account_id == snapshot.account.login
                            )
                        )
                        account = account_result.scalar_one_or_none()
                        if account is not None and account.daily_starting_equity > 0:
                            self._drawdown_available = True
                            self._last_drawdown = max(
                                Decimal(0),
                                (account.daily_starting_equity - snapshot.account.equity)
                                / account.daily_starting_equity
                                * Decimal(100),
                            )
                            ACCOUNT_EQUITY.set(float(snapshot.account.equity))
                            ACCOUNT_DRAWDOWN.set(float(self._last_drawdown))
                            if (
                                self._last_drawdown >= warning_threshold
                                and not warning_active
                            ):
                                drawdown_notification = (
                                    "Drawdown warning\n"
                                    f"Daily drawdown is {self._last_drawdown:.2f}% "
                                    f"(warning threshold {warning_threshold}%)."
                                )
                                warning_active = True
                            elif self._last_drawdown < warning_threshold:
                                warning_active = False
                        total = await session.scalar(
                            select(func.count(Trade.trade_id)).where(
                                Trade.status == "CLOSED"
                            )
                        )
                        winners = await session.scalar(
                            select(func.count(Trade.trade_id)).where(
                                Trade.status == "CLOSED",
                                Trade.realized_pnl > 0,
                            )
                        )
                        WIN_RATE.set(
                            100.0 * (winners or 0) / total if total else 0.0
                        )
                    self._database_healthy = True
                    DATABASE_CONNECTED.set(1)
                    if database_was_healthy is False:
                        await self.send_notification(
                            "System recovery: PostgreSQL access is healthy again."
                        )
                    database_was_healthy = True
                    pool = engine.pool
                    DB_CONNECTIONS.labels(state="checked_out").set(
                        pool.checkedout()
                    )
                    DB_CONNECTIONS.labels(state="pool_size").set(pool.size())
                    if drawdown_notification is not None:
                        await self.send_notification(drawdown_notification)
                except asyncio.CancelledError:
                    raise
                except SQLAlchemyError:
                    self._database_healthy = False
                    DATABASE_CONNECTED.set(0)
                    logger.exception("Telegram telemetry database refresh failed")
                    if database_was_healthy is not False:
                        await self.send_notification(
                            "System error: Telegram telemetry cannot query PostgreSQL."
                        )
                    database_was_healthy = False
            else:
                try:
                    async with sessions() as session:
                        await session.execute(select(1))
                    self._database_healthy = True
                    DATABASE_CONNECTED.set(1)
                    if database_was_healthy is False:
                        await self.send_notification(
                            "System recovery: PostgreSQL access is healthy again."
                        )
                    database_was_healthy = True
                    pool = engine.pool
                    DB_CONNECTIONS.labels(state="checked_out").set(
                        pool.checkedout()
                    )
                    DB_CONNECTIONS.labels(state="pool_size").set(pool.size())
                except asyncio.CancelledError:
                    raise
                except SQLAlchemyError:
                    self._database_healthy = False
                    DATABASE_CONNECTED.set(0)
                    logger.exception("Telegram telemetry database health check failed")
                    if database_was_healthy is not False:
                        await self.send_notification(
                            "System error: Telegram telemetry cannot query PostgreSQL."
                        )
                    database_was_healthy = False
            try:
                await asyncio.wait_for(self._stop_polling.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def send_notification(self, text: str) -> None:
        for chat_id in self.notification_chat_ids:
            await self.send_message(chat_id, text)

    async def aclose(self) -> None:
        self._stop_polling.set()
        if self._owns_client:
            await self.client.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = RedisEventBus()
    mt5 = MT5AdapterClient()
    bot = TelegramControlBot(bus, mt5)
    database_engine = create_database_engine()
    workers = [
        asyncio.create_task(bot.notifications_worker(), name="telegram-notifications"),
        asyncio.create_task(bot.telemetry_worker(database_engine), name="telegram-telemetry"),
    ]
    if bot.commands_enabled:
        workers.append(asyncio.create_task(bot.poll_commands(), name="telegram-commands"))
    else:
        logger.warning("Telegram commands unavailable; configure bot token and user allowlist")
    app.state.telegram_bot = bot
    try:
        yield
    finally:
        await bot.aclose()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await mt5.aclose()
        await bus.aclose()
        await database_engine.dispose()


app = FastAPI(title="telegram-emergency-control", version="0.1.0", lifespan=lifespan)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    bot: TelegramControlBot = app.state.telegram_bot
    if not bot.enabled:
        return {"status": "degraded", "service": "telegram-bot", "telegram": "disabled"}
    return {
        "status": "ok" if bot.commands_enabled else "degraded",
        "service": "telegram-bot",
        "telegram": "commands-enabled" if bot.commands_enabled else "notifications-only",
    }


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
