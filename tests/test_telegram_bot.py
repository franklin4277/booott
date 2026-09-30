import asyncio

import httpx

from scripts import mt5_host_adapter
from services.telegram_bot.app.main import TelegramControlBot


class FakeBus:
    def __init__(self):
        self.states = {}
        self.published = []

    async def get_state(self, key):
        return self.states.get(key)

    async def set_state(self, key, value):
        self.states[key] = value

    async def publish(self, channel, payload, **kwargs):
        self.published.append((channel, payload, kwargs))
        return 1


class FakeAdapter:
    base_url = "http://mt5.test/"
    token = "mt5-adapter-test-token-which-is-more-than-32-bytes"


def command(text, *, user_id=1234, chat_type="private"):
    return {
        "from": {"id": user_id},
        "chat": {"id": user_id, "type": chat_type},
        "text": text,
    }


def test_kill_switch_is_authorized_and_resume_only_releases_no_new_trades():
    async def run():
        bus = FakeBus()
        bot = TelegramControlBot(
            bus,
            FakeAdapter(),
            token="telegram-token",
            allowed_user_ids={1234},
            notification_chat_ids=set(),
        )
        assert await bot._handle_command(
            command("/kill NO_NEW_TRADES", user_id=9999)
        ) is None
        assert bus.states == {}

        response = await bot._handle_command(command("/kill NO_NEW_TRADES"))
        assert "enabled" in response
        assert bus.states["system:kill_switch"]["mode"] == "NO_NEW_TRADES"

        response = await bot._handle_command(command("/resume"))
        assert "cleared" in response
        assert bus.states["system:kill_switch"]["mode"] == "RUNNING"
        await bot.aclose()

    asyncio.run(run())


def test_safe_mode_can_only_be_released_after_reconciliation_clears_it():
    async def run():
        bus = FakeBus()
        bot = TelegramControlBot(
            bus,
            FakeAdapter(),
            token="telegram-token",
            allowed_user_ids={1234},
            notification_chat_ids=set(),
        )
        bus.states["system:kill_switch"] = {"mode": "SAFE_MODE"}
        bus.states["system:safe_mode"] = {"enabled": True}
        response = await bot._handle_command(command("/resume"))
        assert "clear SAFE_MODE" in response
        assert bus.states["system:kill_switch"]["mode"] == "SAFE_MODE"

        bus.states["system:safe_mode"] = {"enabled": False}
        response = await bot._handle_command(command("/resume"))
        assert "Reconciliation cleared" in response
        assert bus.states["system:kill_switch"]["mode"] == "RUNNING"
        await bot.aclose()

    asyncio.run(run())


def test_emergency_flat_latches_safe_mode_before_calling_broker():
    async def run():
        bus = FakeBus()
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/safe-mode/activate":
                return httpx.Response(200, json={"enabled": True})
            if request.url.path == "/v1/emergency-flat":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        bot = TelegramControlBot(
            bus,
            FakeAdapter(),
            token="telegram-token",
            allowed_user_ids={1234},
            notification_chat_ids=set(),
            client=client,
            reconciliation_url="http://reconciliation.test",
            reconciliation_token="reconciliation-test-token-with-over-32-bytes",
        )
        response = await bot._handle_command(command("/kill EMERGENCY_FLAT"))
        assert "completed" in response
        assert calls == ["/safe-mode/activate", "/v1/emergency-flat"]
        assert bus.states["system:kill_switch"]["mode"] == "EMERGENCY_FLAT"
        await bot.aclose()
        await client.aclose()

    asyncio.run(run())


def test_emergency_flat_adapter_verifies_open_positions_and_pending_orders():
    class Result:
        def __init__(self, retcode):
            self.retcode = retcode

    class FakeMT5:
        TRADE_ACTION_REMOVE = 1
        TRADE_ACTION_DEAL = 2
        TRADE_RETCODE_DONE = 100
        TRADE_RETCODE_DONE_PARTIAL = 101
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_FOK = 0
        SYMBOL_FILLING_IOC = 2
        POSITION_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_TYPE_BUY = 0
        ORDER_TIME_GTC = 0

        def __init__(self):
            self.positions = [
                type(
                    "Position",
                    (),
                    {
                        "ticket": 55,
                        "symbol": "EURUSD",
                        "volume": 0.1,
                        "type": self.POSITION_TYPE_BUY,
                    },
                )()
            ]
            self.orders = [type("Order", (), {"ticket": 77})()]

        def positions_get(self):
            return list(self.positions)

        def orders_get(self):
            return list(self.orders)

        def symbol_info_tick(self, symbol):
            return type("Tick", (), {"bid": 1.1, "ask": 1.2})()

        def symbol_info(self, symbol):
            return type("Symbol", (), {"filling_mode": self.SYMBOL_FILLING_IOC})()

        def order_send(self, request):
            if request["action"] == self.TRADE_ACTION_REMOVE:
                self.orders.clear()
            else:
                self.positions.clear()
            return Result(self.TRADE_RETCODE_DONE)

        def last_error(self):
            return (0, "ok")

    mt5 = FakeMT5()
    result = mt5_host_adapter._emergency_flatten(mt5)
    assert result["success"] is True
    assert result["closed_position_tickets"] == [55]
    assert result["cancelled_order_tickets"] == [77]
