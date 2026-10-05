import asyncio
import logging
import os
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, status
from pydantic import ValidationError

from schemas.messages import (
    ExecutionReport,
    ExecutionStatus,
    OrderType,
    SignedOrderPayload,
)
from services.market_data.app.freshness import (
    is_fresh_market_tick,
    market_tick_age_seconds,
)
from utils.security import verify_payload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mt5-host-adapter")
TOKEN = os.environ.get("MT5_ADAPTER_TOKEN", "")
TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH")
MT5_OPERATION_LOCK = threading.Lock()
ORDER_SIGNING_SECRET = (
    os.environ.get("ORDER_SIGNING_SECRET")
    or os.environ.get("SECRET_KEY")
    or ""
)
_configured_order_ledger = os.environ.get("MT5_ORDER_LEDGER_PATH") or None
ORDER_LEDGER_PATH = Path(
    _configured_order_ledger
    or (
        Path(os.environ.get("PROGRAMDATA", Path.home() / "AppData/Local"))
        / "MT5Trading"
        / "orders.sqlite3"
    )
)
MARKET_TIMEFRAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
}


def _authorized(value: str | None) -> bool:
    return bool(value) and secrets.compare_digest(value, f"Bearer {TOKEN}")


def _decimal(value) -> Decimal:
    return Decimal(str(value or 0))


def _client_order_id(comment: str | None) -> UUID | None:
    if not comment:
        return None
    normalized = comment.strip()
    try:
        return UUID(normalized)
    except ValueError:
        if not normalized.startswith("booott:"):
            return None
        prefix = normalized.removeprefix("booott:").lower()
        if len(prefix) < 16 or any(
            character not in "0123456789abcdef" for character in prefix
        ):
            return None
        with closing(_connect_order_ledger()) as connection:
            matches = connection.execute(
                "SELECT order_id FROM broker_order_requests "
                "WHERE substr(replace(order_id, '-', ''), 1, ?) = ?",
                (len(prefix), prefix),
            ).fetchall()
        return UUID(matches[0][0]) if len(matches) == 1 else None


def _position_risk_amount(mt5, position) -> Decimal | None:
    if position.sl <= 0:
        return None
    result = mt5.order_calc_profit(
        mt5.ORDER_TYPE_BUY
        if position.type == getattr(mt5, "POSITION_TYPE_BUY", 0)
        else mt5.ORDER_TYPE_SELL,
        position.symbol,
        float(position.volume),
        float(position.price_open),
        float(position.sl),
    )
    return abs(_decimal(result)) if result is not None else None


def _mt5():
    import MetaTrader5

    if len(TOKEN.encode("utf-8")) < 32:
        raise RuntimeError("MT5_ADAPTER_TOKEN must contain at least 32 bytes")
    initialized = (
        MetaTrader5.initialize(path=TERMINAL_PATH)
        if TERMINAL_PATH
        else MetaTrader5.initialize()
    )
    if not initialized:
        raise RuntimeError(f"MetaTrader5.initialize failed: {MetaTrader5.last_error()}")
    return MetaTrader5


def _fetch_snapshot(since: datetime | None) -> dict:
    mt5 = _mt5()
    import psutil

    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")
    now = datetime.now(timezone.utc)
    positions = mt5.positions_get()
    if positions is None:
        raise RuntimeError(f"MT5 positions_get failed: {mt5.last_error()}")
    orders = mt5.orders_get()
    if orders is None:
        raise RuntimeError(f"MT5 orders_get failed: {mt5.last_error()}")
    history_start = since or now - timedelta(days=30)
    deals = mt5.history_deals_get(history_start, now)
    if deals is None:
        raise RuntimeError(f"MT5 history_deals_get failed: {mt5.last_error()}")

    entry_out_values = {
        getattr(mt5, "DEAL_ENTRY_OUT", 1),
        getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
        getattr(mt5, "DEAL_ENTRY_INOUT", 2),
    }
    output_positions = []
    for item in positions:
        client_order_id = _client_order_id(getattr(item, "comment", None))
        output_positions.append(
            {
                "ticket": int(item.ticket),
                "identifier": int(getattr(item, "identifier", None) or item.ticket),
                "client_order_id": (
                    str(client_order_id) if client_order_id is not None else None
                ),
                "symbol": item.symbol,
                "side": "buy"
                if item.type == getattr(mt5, "POSITION_TYPE_BUY", 0)
                else "sell",
                "volume": _decimal(item.volume),
                "price_open": _decimal(item.price_open),
                "stop_loss": _decimal(item.sl),
                "take_profit": _decimal(item.tp),
                "profit": _decimal(item.profit),
                "swap": _decimal(item.swap),
                "risk_amount": _position_risk_amount(mt5, item),
                "opened_at": datetime.fromtimestamp(item.time, timezone.utc),
            }
        )

    pending_orders = []
    buy_order_types = {
        getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2),
        getattr(mt5, "ORDER_TYPE_BUY_STOP", 4),
        getattr(mt5, "ORDER_TYPE_BUY_STOP_LIMIT", 6),
    }
    for item in orders:
        client_order_id = _client_order_id(getattr(item, "comment", None))
        order_type = int(item.type)
        pending_orders.append(
            {
                "ticket": int(item.ticket),
                "client_order_id": (
                    str(client_order_id) if client_order_id is not None else None
                ),
                "symbol": item.symbol,
                "order_type": str(order_type),
                "side": "buy" if order_type in buy_order_types else "sell",
                "volume": _decimal(getattr(item, "volume_current", item.volume_initial)),
                "price_open": _decimal(item.price_open),
                "stop_loss": _decimal(item.sl),
                "take_profit": _decimal(item.tp),
                "created_at": datetime.fromtimestamp(item.time_setup, timezone.utc),
            }
        )

    closed_deals = []
    for deal in deals:
        if int(deal.entry) not in entry_out_values:
            continue
        closed_deals.append(
            {
                "ticket": int(deal.ticket),
                "position_id": int(deal.position_id),
                "symbol": deal.symbol,
                "timestamp": datetime.fromtimestamp(deal.time, timezone.utc),
                "price": _decimal(deal.price),
                "volume": _decimal(deal.volume),
                "profit": _decimal(deal.profit),
                "commission": _decimal(deal.commission),
                "swap": _decimal(deal.swap),
                "fee": _decimal(getattr(deal, "fee", 0)),
            }
        )
    return {
        "account": {
            "login": str(account.login),
            "currency": account.currency,
            "balance": _decimal(account.balance),
            "equity": _decimal(account.equity),
            "margin": _decimal(account.margin),
            "free_margin": _decimal(account.margin_free),
            "timestamp": now,
        },
        "positions": output_positions,
        "pending_orders": pending_orders,
        "closed_deals": closed_deals,
        "fetched_at": now,
        "host_cpu_percent": Decimal(str(psutil.cpu_percent(interval=None))),
        "host_memory_percent": Decimal(str(psutil.virtual_memory().percent)),
    }


def _fetch_account_status() -> dict:
    mt5 = _mt5()
    import psutil

    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")
    positions = mt5.positions_get()
    if positions is None:
        raise RuntimeError(f"MT5 positions_get failed: {mt5.last_error()}")
    now = datetime.now(timezone.utc)
    return {
        "account": {
            "login": str(account.login),
            "currency": account.currency,
            "balance": _decimal(account.balance),
            "equity": _decimal(account.equity),
            "margin": _decimal(account.margin),
            "free_margin": _decimal(account.margin_free),
            "timestamp": now,
        },
        "positions": [
            {
                "ticket": int(position.ticket),
                "symbol": position.symbol,
                "side": "buy"
                if position.type == getattr(mt5, "POSITION_TYPE_BUY", 0)
                else "sell",
                "volume": _decimal(position.volume),
                "price_open": _decimal(position.price_open),
                "stop_loss": _decimal(position.sl),
                "take_profit": _decimal(position.tp),
                "profit": _decimal(position.profit),
                "swap": _decimal(position.swap),
                "opened_at": datetime.fromtimestamp(position.time, timezone.utc),
            }
            for position in positions
        ],
        "host_cpu_percent": Decimal(str(psutil.cpu_percent(interval=None))),
        "host_memory_percent": Decimal(str(psutil.virtual_memory().percent)),
        "fetched_at": now,
    }


def _fetch_snapshot_serialized(since: datetime | None) -> dict:
    with MT5_OPERATION_LOCK:
        return _fetch_snapshot(since)


def _fetch_account_status_serialized() -> dict:
    with MT5_OPERATION_LOCK:
        return _fetch_account_status()


def _emergency_flatten(mt5) -> dict:
    """Close all broker positions and remove pending orders, then verify flat state."""
    closed_tickets: set[int] = set()
    cancelled_tickets: set[int] = set()
    failures: list[dict[str, str | int]] = []

    for attempt in range(3):
        positions = mt5.positions_get()
        orders = mt5.orders_get()
        if positions is None or orders is None:
            raise RuntimeError(f"Could not enumerate MT5 orders: {mt5.last_error()}")

        for order in orders:
            ticket = int(order.ticket)
            result = mt5.order_send(
                {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
            )
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                cancelled_tickets.add(ticket)
            else:
                failures.append(
                    {
                        "ticket": ticket,
                        "retcode": int(result.retcode) if result is not None else -1,
                    }
                )

        for position in positions:
            ticket = int(position.ticket)
            tick = mt5.symbol_info_tick(position.symbol)
            symbol_info = mt5.symbol_info(position.symbol)
            if tick is None or symbol_info is None:
                failures.append({"ticket": ticket, "retcode": -1})
                continue
            is_buy = position.type == mt5.POSITION_TYPE_BUY
            filling_mode = (
                mt5.ORDER_FILLING_IOC
                if symbol_info.filling_mode & mt5.SYMBOL_FILLING_IOC
                else mt5.ORDER_FILLING_FOK
            )
            result = mt5.order_send(
                {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": position.symbol,
                    "position": ticket,
                    "volume": float(position.volume),
                    "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                    "price": float(tick.bid if is_buy else tick.ask),
                    "deviation": 100,
                    "magic": 0,
                    "comment": "EMERGENCY_FLAT",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": filling_mode,
                }
            )
            if result is not None and result.retcode in {
                mt5.TRADE_RETCODE_DONE,
                getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -2),
            }:
                closed_tickets.add(ticket)
            else:
                failures.append(
                    {
                        "ticket": ticket,
                        "retcode": int(result.retcode) if result is not None else -1,
                    }
                )

        if attempt < 2:
            remaining = mt5.positions_get()
            pending = mt5.orders_get()
            if remaining is None or pending is None:
                raise RuntimeError(
                    f"Could not verify MT5 orders: {mt5.last_error()}"
                )
            if not remaining and not pending:
                break

    remaining_positions = mt5.positions_get()
    remaining_orders = mt5.orders_get()
    if remaining_positions is None or remaining_orders is None:
        raise RuntimeError(f"Could not verify flat MT5 state: {mt5.last_error()}")

    still_open = [int(position.ticket) for position in remaining_positions]
    still_pending = [int(order.ticket) for order in remaining_orders]
    return {
        "success": not still_open and not still_pending,
        "closed_position_tickets": sorted(closed_tickets),
        "cancelled_order_tickets": sorted(cancelled_tickets),
        "remaining_position_tickets": still_open,
        "remaining_order_tickets": still_pending,
        "failures": failures,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def _emergency_flatten_serialized() -> dict:
    with MT5_OPERATION_LOCK:
        return _emergency_flatten(_mt5())


def _connect_order_ledger() -> sqlite3.Connection:
    ORDER_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(ORDER_LEDGER_PATH, timeout=10)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_order_requests (
            order_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            report_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    return connection


def _market_timeframes(mt5) -> list[tuple[str, int]]:
    names = [
        item.strip().upper()
        for item in os.environ.get("MT5_MARKET_TIMEFRAMES", "M5,M15,H1").split(",")
        if item.strip()
    ]
    invalid = [name for name in names if name not in MARKET_TIMEFRAMES]
    if invalid:
        raise RuntimeError(f"Unsupported MT5 market timeframe(s): {invalid}")
    return [(name, int(getattr(mt5, MARKET_TIMEFRAMES[name]))) for name in names]


def _collect_market_data(
    cursors: dict[tuple[str, str], int],
    tick_cursors: dict[str, int],
) -> list[tuple[tuple[str, str], int, str, dict]]:
    mt5 = _mt5()

    symbols = [
        item.strip()
        for item in os.environ.get("MT5_MARKET_SYMBOLS", "EURUSD").split(",")
        if item.strip()
    ]
    if not symbols:
        raise RuntimeError("MT5_MARKET_SYMBOLS must list at least one symbol.")
    timeframes = _market_timeframes(mt5)
    history_count = max(
        100,
        int(os.environ.get("MT5_MARKET_HISTORY_BARS", "128")),
    )
    messages: list[tuple[tuple[str, str], int, str, dict]] = []
    for symbol in symbols:
        if not mt5.symbol_select(symbol, True):
            logger.error("MT5 could not select configured market symbol %s", symbol)
            continue
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None:
            time_msc = int(getattr(tick, "time_msc", 0) or tick.time * 1000)
            if time_msc > tick_cursors.get(symbol, 0):
                tick_timestamp = datetime.fromtimestamp(
                    time_msc / 1000, timezone.utc
                )
                if not is_fresh_market_tick(tick_timestamp):
                    logger.info(
                        "Ignoring stale MT5 tick for %s (age %.1fs); waiting for a new quote",
                        symbol,
                        market_tick_age_seconds(tick_timestamp),
                    )
                    tick_cursors[symbol] = time_msc
                else:
                    tick_key = (
                        f"{symbol}|{time_msc}|{tick.bid}|{tick.ask}|"
                        f"{getattr(tick, 'last', 0)}|{getattr(tick, 'flags', 0)}"
                    )
                    tick_id = uuid5(UUID("38b3b12a-5a34-4b18-8f11-4e0979a30b60"), tick_key)
                    messages.append(
                        (
                            (symbol, "tick"),
                            time_msc,
                            "tick",
                            {
                                "tick_id": str(tick_id),
                                "symbol": symbol,
                                "timestamp": tick_timestamp.isoformat(),
                                "bid": str(tick.bid),
                                "ask": str(tick.ask),
                                "last": str(tick.last) if tick.last and tick.last > 0 else None,
                                "volume": (
                                    str(tick.volume_real)
                                    if getattr(tick, "volume_real", 0) > 0
                                    else None
                                ),
                                "source": "mt5-host-adapter",
                            },
                        )
                    )

        for timeframe_name, timeframe_value in timeframes:
            cursor_key = (symbol, timeframe_name)
            start_pos = 1
            count = (
                history_count
                if cursor_key not in cursors
                else 1
            )
            rates = mt5.copy_rates_from_pos(
                symbol,
                timeframe_value,
                start_pos,
                count,
            )
            if rates is None:
                logger.warning(
                    "MT5 returned no %s history for %s: %s",
                    timeframe_name,
                    symbol,
                    mt5.last_error(),
                )
                continue
            for rate in sorted(rates, key=lambda item: int(item["time"])):
                timestamp = int(rate["time"])
                if timestamp <= cursors.get(cursor_key, 0):
                    continue
                messages.append(
                    (
                        cursor_key,
                        timestamp,
                        "bar",
                        {
                            "symbol": symbol,
                            "timeframe": timeframe_name,
                            "timestamp": datetime.fromtimestamp(
                                timestamp, timezone.utc
                            ).isoformat(),
                            "open": str(rate["open"]),
                            "high": str(rate["high"]),
                            "low": str(rate["low"]),
                            "close": str(rate["close"]),
                            "tick_volume": int(rate["tick_volume"]),
                            "spread": int(rate["spread"]),
                            "real_volume": str(rate["real_volume"]),
                        },
                    )
                )
    messages.sort(key=lambda message: (message[1], message[2] == "tick"))
    return messages


def _collect_market_data_serialized(
    cursors: dict[tuple[str, str], int],
    tick_cursors: dict[str, int],
) -> list[tuple[tuple[str, str], int, str, dict]]:
    with MT5_OPERATION_LOCK:
        return _collect_market_data(cursors, tick_cursors)


async def _publish_market_data(client: httpx.AsyncClient) -> None:
    if os.environ.get("MT5_MARKET_DATA_ENABLED", "true").lower() not in {
        "1",
        "true",
        "yes",
    }:
        logger.info("MT5 host market-data publishing is disabled")
        return
    ingest_url = os.environ.get(
        "MARKET_DATA_INGEST_URL", "http://127.0.0.1:8020"
    ).rstrip("/")
    token = os.environ.get("MT5_ADAPTER_TOKEN", "")
    cursors: dict[tuple[str, str], int] = {}
    tick_cursors: dict[str, int] = {}
    while True:
        try:
            messages = await asyncio.to_thread(
                _collect_market_data_serialized,
                cursors,
                tick_cursors,
            )
            for key, timestamp, kind, payload in messages:
                response = await client.post(
                    f"{ingest_url}/v1/ingest/{kind}",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                if kind == "tick":
                    tick_cursors[key[0]] = timestamp
                else:
                    cursors[key] = timestamp
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ImportError, RuntimeError, OSError):
            logger.exception("MT5 market-data polling/publishing failed; retrying")
        await asyncio.sleep(max(0.25, float(os.environ.get("MT5_MARKET_POLL_SECONDS", "1"))))


def _mt5_filling_mode(mt5, symbol_info) -> int:
    filling_mode = int(symbol_info.filling_mode)
    if filling_mode & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
        return mt5.ORDER_FILLING_IOC
    if filling_mode & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _execute_signed_order_once(order: SignedOrderPayload) -> ExecutionReport:
    with closing(_connect_order_ledger()) as connection:
        with connection:
            existing = connection.execute(
                "SELECT state, report_json FROM broker_order_requests WHERE order_id = ?",
                (str(order.order_id),),
            ).fetchone()
            if existing is not None:
                if existing[0] == "complete" and existing[1]:
                    return ExecutionReport.model_validate_json(existing[1])
                raise RuntimeError(
                    "The order has an unresolved prior submission; reconcile broker state before retrying."
                )

    mt5 = _mt5()
    account = mt5.account_info()
    if (
        account is None
        or not bool(getattr(account, "trade_allowed", False))
        or not bool(getattr(account, "trade_expert", False))
    ):
        raise RuntimeError(
            "MT5 account or terminal does not allow Expert Advisor trading."
        )
    if not mt5.symbol_select(order.symbol, True):
        raise RuntimeError(f"MT5 could not select symbol {order.symbol}.")
    symbol_info = mt5.symbol_info(order.symbol)
    tick = mt5.symbol_info_tick(order.symbol)
    if symbol_info is None or tick is None:
        raise RuntimeError(f"MT5 has no current quote for {order.symbol}.")
    if (
        order.volume < Decimal(str(symbol_info.volume_min))
        or order.volume > Decimal(str(symbol_info.volume_max))
    ):
        raise RuntimeError(f"Approved volume is outside {order.symbol} broker limits.")
    volume_step = Decimal(str(symbol_info.volume_step))
    steps_from_min = (
        order.volume - Decimal(str(symbol_info.volume_min))
    ) / volume_step
    if steps_from_min != steps_from_min.to_integral_value():
        raise RuntimeError(f"Approved volume does not match {order.symbol} lot step.")

    is_buy = order.side.value == "buy"
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": order.symbol,
        "volume": float(order.volume),
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": float(tick.ask if is_buy else tick.bid),
        "sl": float(order.stop_loss or 0),
        "tp": float(order.take_profit or 0),
        "deviation": int(os.environ.get("MT5_MAX_DEVIATION_POINTS", "20")),
        "magic": int(os.environ.get("MT5_EXPERT_MAGIC", "427700")),
        "comment": "booott:" + order.order_id.hex[:25],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _mt5_filling_mode(mt5, symbol_info),
    }
    check = mt5.order_check(request)
    if check is None or int(check.retcode) != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(
            "MT5 rejected the preflight order check: "
            + (
                str(getattr(check, "comment", ""))
                if check is not None
                else str(mt5.last_error())
            )
        )
    with closing(_connect_order_ledger()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO broker_order_requests(order_id, state, created_at) VALUES (?, 'sending', ?)",
                (str(order.order_id), datetime.now(timezone.utc).isoformat()),
            )
    result = mt5.order_send(request)
    if result is None:
        raise RuntimeError(
            f"MT5 order_send returned no result: {mt5.last_error()}"
        )
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        execution_status = ExecutionStatus.FILLED
    elif result.retcode == getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1):
        execution_status = ExecutionStatus.PARTIALLY_FILLED
    else:
        execution_status = ExecutionStatus.REJECTED
    report = ExecutionReport(
        order_id=order.order_id,
        status=execution_status,
        executed_volume=Decimal(str(getattr(result, "volume", 0) or 0)),
        fill_price=(
            Decimal(str(result.price))
            if getattr(result, "price", 0)
            else None
        ),
        broker_order_id=(
            str(result.order)
            if getattr(result, "order", 0)
            else (
                str(result.deal)
                if getattr(result, "deal", 0)
                else None
            )
        ),
        timestamp=datetime.now(timezone.utc),
        message=str(getattr(result, "comment", ""))[:2000] or None,
        trace_id=order.trace_id,
    )
    with closing(_connect_order_ledger()) as connection:
        with connection:
            connection.execute(
                "UPDATE broker_order_requests SET state = 'complete', report_json = ? WHERE order_id = ?",
                (report.model_dump_json(), str(order.order_id)),
            )
    return report


def _execute_signed_order_serialized(order: SignedOrderPayload) -> ExecutionReport:
    with MT5_OPERATION_LOCK:
        return _execute_signed_order_once(order)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(TOKEN.encode("utf-8")) < 32:
        raise RuntimeError("MT5_ADAPTER_TOKEN must contain at least 32 bytes")
    if len(ORDER_SIGNING_SECRET.encode("utf-8")) < 32:
        raise RuntimeError(
            "ORDER_SIGNING_SECRET or SECRET_KEY must contain at least 32 bytes"
        )
    async with httpx.AsyncClient(timeout=5) as market_client:
        market_worker = asyncio.create_task(
            _publish_market_data(market_client),
            name="mt5-market-data-publisher",
        )
        try:
            yield
        finally:
            market_worker.cancel()
            await asyncio.gather(market_worker, return_exceptions=True)


app = FastAPI(title="MT5 Host Adapter", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "mt5-host-adapter"}


@app.get("/v1/state")
async def get_state(
    since: datetime | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    if not _authorized(authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid adapter authorization token.",
        )
    try:
        return await asyncio.to_thread(_fetch_snapshot_serialized, since)
    except (ImportError, RuntimeError, OSError, sqlite3.Error) as exc:
        logger.exception("Could not fetch live MT5 terminal state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MT5 terminal state is currently unavailable.",
        ) from exc


@app.get("/v1/account")
async def get_account_status(
    authorization: str | None = Header(default=None),
) -> dict:
    if not _authorized(authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid adapter authorization token.",
        )
    try:
        return await asyncio.to_thread(_fetch_account_status_serialized)
    except (ImportError, RuntimeError) as exc:
        logger.exception("Could not fetch live MT5 account status")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MT5 account status is currently unavailable.",
        ) from exc


@app.post("/v1/emergency-flat")
async def emergency_flat(
    authorization: str | None = Header(default=None),
) -> dict:
    if not _authorized(authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid adapter authorization token.",
        )
    try:
        result = await asyncio.to_thread(_emergency_flatten_serialized)
        if not result["success"]:
            logger.critical("Emergency flatten incomplete: %s", result)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=result,
            )
        logger.critical("Emergency flatten completed: %s", result)
        return result
    except HTTPException:
        raise
    except (ImportError, RuntimeError) as exc:
        logger.exception("Emergency MT5 flatten failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MT5 emergency flatten could not be completed.",
        ) from exc


@app.post("/v1/orders", response_model=ExecutionReport)
async def submit_signed_order(
    order: SignedOrderPayload,
    authorization: str | None = Header(default=None),
) -> ExecutionReport:
    if not _authorized(authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid adapter authorization token.",
        )
    if not bool(os.environ.get("MT5_LIVE_TRADING_ENABLED", "").lower() in {"1", "true", "yes"}):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live order execution is disabled on the Windows MT5 host.",
        )
    if order.order_type != OrderType.MARKET:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The MT5 host adapter only accepts market orders.",
        )
    if (
        order.expires_at is None
        or (order.expires_at - order.issued_at).total_seconds() != 5
        or order.expires_at <= datetime.now(timezone.utc)
    ):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Signed order is expired or does not have the required five-second TTL.",
        )
    try:
        if not verify_payload(order, ORDER_SIGNING_SECRET):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Signed order HMAC verification failed.",
            )
        return await asyncio.to_thread(_execute_signed_order_serialized, order)
    except HTTPException:
        raise
    except RuntimeError as exc:
        logger.exception("Signed order execution did not complete order_id=%s", order.order_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except (ImportError, OSError, sqlite3.Error, ValidationError) as exc:
        logger.exception("Signed order execution failed order_id=%s", order.order_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MT5 order execution could not be confirmed; reconcile before retrying.",
        ) from exc
