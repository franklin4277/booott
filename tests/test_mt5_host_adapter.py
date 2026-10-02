import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from scripts import mt5_host_adapter


class MT5HostAdapterTests(unittest.TestCase):
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
