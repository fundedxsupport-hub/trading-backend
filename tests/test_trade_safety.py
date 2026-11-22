import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.models import (
    AccountType,
    AppCloseTradeRequest,
    TradeExecutionStatus,
    TradeRequest,
    TradeSide,
)
from app.services import account_service, trade_service


class TradeSafetyTests(unittest.TestCase):
    def test_challenge_open_is_virtual_and_skips_broker(self):
        user = {
            "user_id": "client-1",
            "client_id": "FXI001",
            "challenge_virtual_capital": 50000,
            "real_capital": 0,
            "trading_enabled": True,
        }
        wallet = {"balance": 50000, "available_balance": 50000}
        request = TradeRequest(
            user_id="client-1",
            symbol="RELIANCE",
            side=TradeSide.buy,
            amount=100,
            entry_price=100,
            quantity=2,
            account_type=AccountType.challenge,
            execute_on_broker=True,
        )

        with (
            patch.object(trade_service, "get_user_or_404", return_value=user),
            patch.object(trade_service, "get_wallet", return_value=wallet),
            patch.object(trade_service, "get_risk_profile", return_value={}),
            patch.object(trade_service, "_current_total_loss", return_value=0),
            patch.object(trade_service, "_validate_stop_loss"),
            patch.object(
                trade_service,
                "broker_adapter",
                SimpleNamespace(
                    is_live=False,
                    estimate_margin=MagicMock(side_effect=AssertionError("broker margin call")),
                    place_order=MagicMock(side_effect=AssertionError("broker order call")),
                ),
            ) as adapter,
            patch.object(trade_service, "trades", SimpleNamespace(insert_one=MagicMock())),
            patch.object(trade_service, "users", SimpleNamespace(update_one=MagicMock())),
            patch.object(trade_service, "trade_events", SimpleNamespace(insert_one=MagicMock())),
        ):
            response = trade_service.open_trade(request)

        self.assertEqual(response["status"], TradeExecutionStatus.demo)
        self.assertIsNone(response["broker_order_id"])
        adapter.estimate_margin.assert_not_called()
        adapter.place_order.assert_not_called()

    def test_client_cannot_close_another_clients_trade(self):
        user = {"user_id": "client-1"}
        with (
            patch.object(trade_service, "trades", SimpleNamespace(find_one=MagicMock(return_value={"trade_id": "t-2", "user_id": "client-2", "account_type": "real"}))),
            patch.object(trade_service, "close_trade") as close,
        ):
            with self.assertRaises(HTTPException) as raised:
                trade_service.close_app_trade(user, AppCloseTradeRequest(trade_id="t-2"))

        self.assertEqual(raised.exception.status_code, 403)
        close.assert_not_called()

    def test_challenge_close_stays_virtual_even_when_broker_is_live(self):
        trade = {
            "trade_id": "challenge-1",
            "user_id": "client-1",
            "account_type": "challenge",
            "side": "BUY",
            "entry_price": 100,
            "current_price": 110,
            "quantity": 2,
            "filled_quantity": 2,
            "required_margin": 200,
            "is_open": True,
            "real_exposure_reserved": False,
        }
        user = {"user_id": "client-1", "challenge_virtual_capital": 50000}
        adapter = SimpleNamespace(is_live=True, close_order=MagicMock(side_effect=AssertionError("broker close call")))
        with (
            patch.object(trade_service, "trades", SimpleNamespace(find_one=MagicMock(return_value=trade), update_one=MagicMock())),
            patch.object(trade_service, "users", SimpleNamespace(update_one=MagicMock())),
            patch.object(trade_service, "trade_events", SimpleNamespace(insert_one=MagicMock())),
            patch.object(trade_service, "broker_adapter", adapter),
            patch.object(trade_service, "get_user_or_404", return_value=user),
            patch.object(trade_service, "get_wallet", return_value={"balance": 50020, "available_balance": 50020}),
            patch.object(trade_service, "_apply_risk_lock"),
        ):
            result = trade_service.close_trade(trade_service.CloseTradeRequest(trade_id="challenge-1", current_price=110))

        self.assertEqual(result["profit_loss"], 20)
        adapter.close_order.assert_not_called()

    def test_per_client_exposure_uses_atomic_cap_condition(self):
        user = {"user_id": "client-1", "real_capital": 1000}
        result = MagicMock(modified_count=1)
        with (
            patch.object(trade_service, "broker_adapter", SimpleNamespace(is_live=False)),
            patch.object(trade_service, "users", SimpleNamespace(update_one=MagicMock(return_value=result))) as users,
        ):
            master_reserved = trade_service._reserve_real_exposure(user, 400)

        self.assertFalse(master_reserved)
        update = users.update_one
        query = update.call_args.args[0]
        self.assertIn("$expr", query)
        self.assertEqual(query["$expr"]["$lte"][1], 1000)
        self.assertEqual(update.call_args.args[1]["$inc"]["real_exposure_used"], 400)

    def test_per_client_exposure_rejects_cap_overflow(self):
        user = {"user_id": "client-1", "real_capital": 1000}
        with (
            patch.object(trade_service, "broker_adapter", SimpleNamespace(is_live=False)),
            patch.object(trade_service, "users", SimpleNamespace(update_one=MagicMock(return_value=SimpleNamespace(modified_count=0)))),
        ):
            with self.assertRaises(HTTPException) as raised:
                trade_service._reserve_real_exposure(user, 1001)

        self.assertEqual(raised.exception.status_code, 400)

    def test_admin_cannot_reduce_real_capital_below_reserved_exposure(self):
        user = {"user_id": "client-1", "real_exposure_used": 500}
        with (
            patch.object(account_service, "get_user_or_404", return_value=user),
            patch.object(account_service.users, "update_one") as update,
            patch.object(account_service.wallet_logs, "insert_one") as log,
        ):
            with self.assertRaises(HTTPException) as raised:
                account_service.update_wallet("client-1", AccountType.real, 499, "test")

        self.assertEqual(raised.exception.status_code, 400)
        update.assert_not_called()
        log.assert_not_called()

    def test_live_mode_fails_closed_without_shared_account_approval(self):
        user = {"user_id": "client-1", "real_capital": 1000}
        with (
            patch.object(trade_service, "broker_adapter", SimpleNamespace(is_live=True)),
            patch.object(trade_service, "settings", SimpleNamespace(upstox_shared_account_approved=False)),
            patch.object(trade_service, "users", SimpleNamespace(update_one=MagicMock())) as users,
        ):
            with self.assertRaises(HTTPException) as raised:
                trade_service._reserve_real_exposure(user, 100)

        self.assertEqual(raised.exception.status_code, 503)
        users.update_one.assert_not_called()

    def test_live_mode_fails_closed_without_master_capital_limit(self):
        user = {"user_id": "client-1", "real_capital": 1000}
        user_update = MagicMock(return_value=SimpleNamespace(modified_count=1))
        with (
            patch.object(trade_service, "broker_adapter", SimpleNamespace(is_live=True)),
            patch.object(trade_service, "settings", SimpleNamespace(upstox_shared_account_approved=True, upstox_master_capital_limit=0)),
            patch.object(trade_service, "trades", SimpleNamespace(count_documents=MagicMock(return_value=0))),
            patch.object(trade_service, "users", SimpleNamespace(update_one=user_update)),
            patch.object(trade_service, "master_broker", SimpleNamespace(update_one=MagicMock())) as master,
        ):
            with self.assertRaises(HTTPException) as raised:
                trade_service._reserve_real_exposure(user, 100)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(user_update.call_count, 2)  # reserve, then compensate
        self.assertEqual(user_update.call_args.args[1]["$inc"]["real_exposure_used"], -100)
        master.update_one.assert_not_called()

    def test_live_mode_rolls_back_client_reservation_when_master_cap_would_be_exceeded(self):
        user = {"user_id": "client-1", "real_capital": 1000}
        user_update = MagicMock(return_value=SimpleNamespace(modified_count=1))
        with (
            patch.object(trade_service, "broker_adapter", SimpleNamespace(is_live=True)),
            patch.object(trade_service, "settings", SimpleNamespace(upstox_shared_account_approved=True, upstox_master_capital_limit=500)),
            patch.object(trade_service, "trades", SimpleNamespace(count_documents=MagicMock(return_value=0))),
            patch.object(trade_service, "users", SimpleNamespace(update_one=user_update)),
            patch.object(trade_service, "master_broker", SimpleNamespace(update_one=MagicMock(return_value=SimpleNamespace(modified_count=0)))),
        ):
            with self.assertRaises(HTTPException) as raised:
                trade_service._reserve_real_exposure(user, 200)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(user_update.call_count, 2)
        self.assertEqual(user_update.call_args.args[1]["$inc"]["real_exposure_used"], -200)

    def test_loss_calculation_nets_open_and_realized_pnl(self):
        user = {
            "user_id": "client-1",
            "real_capital": 1000,
            "real_profit": 25,
            "real_loss": 100,
        }
        open_trade = {
            "entry_price": 100,
            "current_price": 80,
            "filled_quantity": 6,
            "side": "BUY",
        }
        cursor = MagicMock()
        cursor.__iter__.return_value = iter([open_trade])
        with patch.object(trade_service, "trades", SimpleNamespace(find=MagicMock(return_value=cursor))):
            current_loss = trade_service._current_total_loss(user, AccountType.real)

        self.assertEqual(current_loss, 195)

    def test_partial_close_applies_only_confirmed_filled_quantity(self):
        trade = {
            "trade_id": "trade-1",
            "user_id": "client-1",
            "client_id": "FXI001",
            "account_type": "real",
            "side": "BUY",
            "entry_price": 100,
            "filled_quantity": 10,
            "requested_quantity": 10,
            "close_requested_quantity": 10,
            "required_margin": 1000,
            "reserved_exposure": 1000,
            "real_exposure_reserved": True,
            "master_exposure_reserved": True,
            "broker_order_id": "entry-id",
            "close_broker_order_id": "close-id",
        }
        details = {"status": "complete", "filled_quantity": 4, "average_price": 90}
        user_update = MagicMock()
        trade_update = MagicMock(return_value=SimpleNamespace(modified_count=1))

        with (
            patch.object(trade_service, "users", SimpleNamespace(update_one=user_update)),
            patch.object(trade_service, "trades", SimpleNamespace(update_one=trade_update)),
            patch.object(trade_service, "master_broker", SimpleNamespace(update_one=MagicMock())),
            patch.object(trade_service, "trade_events", SimpleNamespace(insert_one=MagicMock())),
            patch.object(trade_service, "_apply_risk_lock"),
        ):
            fully_closed = trade_service._apply_live_close_fill(trade, details)

        self.assertFalse(fully_closed)
        self.assertEqual(trade["filled_quantity"], 6)
        self.assertEqual(trade["required_margin"], 600)
        self.assertEqual(trade["reserved_exposure"], 600)
        user_update.assert_called_once()
        self.assertEqual(user_update.call_args.args[1]["$inc"]["real_loss"], 40)
        self.assertEqual(user_update.call_args.args[1]["$inc"]["real_margin_used"], -400)
        self.assertEqual(trade_update.call_args.args[1]["$set"]["status"], "OPEN")

    def test_duplicate_close_fill_reconciliation_is_idempotent(self):
        trade = {
            "trade_id": "trade-1",
            "user_id": "client-1",
            "client_id": "FXI001",
            "account_type": "real",
            "side": "BUY",
            "entry_price": 100,
            "filled_quantity": 10,
            "requested_quantity": 10,
            "close_requested_quantity": 10,
            "required_margin": 1000,
            "reserved_exposure": 1000,
            "real_exposure_reserved": True,
            "master_exposure_reserved": True,
            "close_pending": True,
            "broker_order_id": "entry-id",
            "close_broker_order_id": "close-id",
        }
        stale_copy = dict(trade)
        details = {"status": "complete", "filled_quantity": 10, "average_price": 90}
        trade_update = MagicMock(
            side_effect=[SimpleNamespace(modified_count=1), SimpleNamespace(modified_count=0)]
        )
        user_update = MagicMock()

        with (
            patch.object(trade_service, "users", SimpleNamespace(update_one=user_update)),
            patch.object(trade_service, "trades", SimpleNamespace(update_one=trade_update)),
            patch.object(trade_service, "master_broker", SimpleNamespace(update_one=MagicMock())),
            patch.object(trade_service, "trade_events", SimpleNamespace(insert_one=MagicMock())),
            patch.object(trade_service, "get_user_or_404", return_value={"user_id": "client-1", "real_capital": 1000}),
            patch.object(trade_service, "_apply_risk_lock"),
        ):
            first = trade_service._apply_live_close_fill(trade, details)
            second = trade_service._apply_live_close_fill(stale_copy, details)

        self.assertTrue(first)
        self.assertFalse(second)
        user_update.assert_called_once()

    def test_close_claim_prevents_duplicate_broker_orders(self):
        user = {"user_id": "client-1"}
        trade = {
            "trade_id": "trade-1",
            "user_id": "client-1",
            "client_id": "FXI001",
            "account_type": "real",
            "broker_live": True,
            "broker_order_id": "entry-id",
            "broker_tag": "fx-entry-tag",
            "side": "BUY",
            "entry_price": 100,
            "current_price": 100,
            "quantity": 1,
            "requested_quantity": 1,
            "filled_quantity": 1,
            "required_margin": 100,
            "real_exposure_reserved": False,
            "master_exposure_reserved": False,
            "close_pending": False,
            "close_broker_order_id": None,
        }
        trade_collection = MagicMock()
        trade_collection.update_one.side_effect = [
            SimpleNamespace(modified_count=1),  # clear reconciliation flag
            SimpleNamespace(modified_count=1),  # record verified entry fill
            SimpleNamespace(modified_count=0),  # another caller claimed the close
        ]
        adapter = SimpleNamespace(
            is_live=True,
            get_order_details=MagicMock(return_value={"status": "complete", "filled_quantity": 1, "pending_quantity": 0, "average_price": 100}),
            close_order=MagicMock(),
        )
        with (
            patch.object(trade_service, "broker_adapter", adapter),
            patch.object(trade_service, "trades", trade_collection),
            patch.object(trade_service, "users", SimpleNamespace(update_one=MagicMock())),
            patch.object(trade_service, "master_broker", SimpleNamespace(update_one=MagicMock())),
            patch.object(trade_service, "get_wallet", return_value={"balance": 1000, "available_balance": 900}),
        ):
            result = trade_service._close_live_trade(
                trade,
                trade_service.CloseTradeRequest(trade_id="trade-1", current_price=100),
                user,
            )

        self.assertIn("no duplicate was sent", result["message"])
        adapter.close_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
