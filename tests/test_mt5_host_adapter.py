import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from scripts import mt5_host_adapter


class MT5HostAdapterTests(unittest.TestCase):
    def test_account_status_includes_live_positions(self):
        mt5 = SimpleNamespace(
            POSITION_TYPE_BUY=0,
            account_info=lambda: SimpleNamespace(
                login=12345678,
                currency="USD",
                balance=10000.0,
                equity=10100.0,
                margin=500.0,
                margin_free=9600.0,
            ),
            positions_get=lambda: [
                SimpleNamespace(
                    ticket=42,
                    symbol="EURUSD",
                    type=0,
                    volume=0.1,
                    price_open=1.1,
                    sl=1.09,
                    tp=1.12,
                    profit=10.0,
                    swap=0.0,
                    time=1767225600,
                )
            ],
        )
        with patch.object(mt5_host_adapter, "_mt5", return_value=mt5):
            result = mt5_host_adapter._fetch_account_status()

        self.assertEqual(result["account"]["balance"], 10000)
        self.assertEqual(result["positions"][0]["ticket"], 42)
        self.assertEqual(result["positions"][0]["side"], "buy")
        self.assertEqual(result["positions"][0]["profit"], 10)

    def test_recovers_client_order_id_from_mt5_comment_prefix(self):
        order_id = uuid4()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "orders.sqlite3"
            with patch.object(mt5_host_adapter, "ORDER_LEDGER_PATH", ledger):
                with closing(mt5_host_adapter._connect_order_ledger()) as connection:
                    with connection:
                        connection.execute(
                            "INSERT INTO broker_order_requests"
                            "(order_id, state, created_at) VALUES (?, 'complete', 'now')",
                            (str(order_id),),
                        )

                recovered = mt5_host_adapter._client_order_id(
                    "booott:" + order_id.hex[:25]
                )

        self.assertEqual(recovered, order_id)

    def test_unknown_or_unrelated_order_comment_is_not_correlated(self):
        self.assertIsNone(mt5_host_adapter._client_order_id("broker-comment"))
