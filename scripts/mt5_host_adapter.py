import asyncio
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mt5-host-adapter")
TOKEN = os.environ.get("MT5_ADAPTER_TOKEN", "")
TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH")
MT5_OPERATION_LOCK = threading.Lock()


def _authorized(value: str | None) -> bool:
    return bool(value) and secrets.compare_digest(value, f"Bearer {TOKEN}")


def _decimal(value) -> Decimal:
    return Decimal(str(value or 0))


def _client_order_id(comment: str | None) -> UUID | None:
    if not comment:
        return None
    try:
        return UUID(comment.strip())
    except ValueError:
        return None


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
        output_positions.append(
            {
                "ticket": int(item.ticket),
                "identifier": int(getattr(item, "identifier", None) or item.ticket),
                "client_order_id": str(_client_order_id(getattr(item, "comment", None)))
                if _client_order_id(getattr(item, "comment", None))
                else None,
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
        order_type = int(item.type)
        pending_orders.append(
            {
                "ticket": int(item.ticket),
                "client_order_id": str(_client_order_id(getattr(item, "comment", None)))
                if _client_order_id(getattr(item, "comment", None))
                else None,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(TOKEN.encode("utf-8")) < 32:
        raise RuntimeError("MT5_ADAPTER_TOKEN must contain at least 32 bytes")
    yield


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
    except (ImportError, RuntimeError) as exc:
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
