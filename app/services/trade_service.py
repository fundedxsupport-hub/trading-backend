from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.database import trade_events, trades, users
from app.models import AccountType, CloseTradeRequest, TradeExecutionStatus, TradeRequest, TradeSide
from app.services.account_service import get_risk_profile, get_user_or_404, get_wallet
from app.services.broker_adapter import broker_adapter
from app.utils import clean_dict, new_uuid, now_utc


def _capital_field(account_type: AccountType) -> str:
    return "challenge_virtual_capital" if account_type == AccountType.challenge else "real_capital"


def _profit_field(account_type: AccountType, profit_loss: float) -> str:
    if account_type == AccountType.challenge:
        return "challenge_profit" if profit_loss >= 0 else "challenge_loss"
    return "real_profit" if profit_loss >= 0 else "real_loss"


def _margin_field(account_type: AccountType) -> str:
    return "challenge_margin_used" if account_type == AccountType.challenge else "real_margin_used"


def _available_balance(wallet: Dict[str, Any]) -> float:
    available = wallet.get("available_balance")
    if available is None:
        return float(wallet["balance"])
    return float(available)


def _entry_price(request: TradeRequest) -> float:
    if request.order_type.upper() == "LIMIT" and request.limit_price:
        return float(request.limit_price)
    if request.entry_price:
        return float(request.entry_price)
    return float(request.amount)


def _required_margin(request: TradeRequest) -> float:
    return round(_entry_price(request) * float(request.quantity), 2)


def _normalize_order_type(order_type: str) -> str:
    normalized = (order_type or "MARKET").strip().upper()
    if normalized not in {"MARKET", "LIMIT", "SL", "SL-M"}:
        return "MARKET"
    return normalized


def _log_trade_event(trade_id: str, event_type: str, payload: Dict[str, Any]) -> None:
    trade_events.insert_one(
        {
            "event_id": new_uuid(),
            "trade_id": trade_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": now_utc(),
        }
    )


def _side_multiplier(side: str) -> int:
    return 1 if side.upper() == TradeSide.buy.value else -1


def _calculate_profit_loss(side: str, entry_price: float, exit_price: float, quantity: float) -> float:
    multiplier = _side_multiplier(side)
    return round((exit_price - entry_price) * quantity * multiplier, 2)


def _risk_breach_reason(user: Dict[str, Any], account_type: AccountType) -> Optional[str]:
    wallet = get_wallet(user["user_id"], account_type)
    balance = float(wallet["balance"])
    capital = float(user.get(_capital_field(account_type), 0))
    max_loss_limit = float(user.get("max_loss_limit", 0) or 0)
    max_drawdown_limit = float(user.get("max_drawdown_limit", 0) or 0)

    if max_loss_limit > 0 and balance <= capital - max_loss_limit:
        return "Max loss limit hit"
    if max_drawdown_limit > 0 and balance <= capital - max_drawdown_limit:
        return "Max drawdown hit"
    return None


def _apply_risk_lock(user: Dict[str, Any], account_type: AccountType) -> None:
    reason = _risk_breach_reason(user, account_type)
    if not reason:
        return
    users.update_one(
        {"user_id": user["user_id"]},
        {
            "$set": {
                "trading_enabled": False,
                "risk_lock_reason": reason,
                "updated_at": now_utc(),
            }
        },
    )


def open_trade(request: TradeRequest) -> Dict[str, Any]:
    user = get_user_or_404(request.user_id)
    if not user.get("trading_enabled", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Trading is disabled for this account")

    wallet = get_wallet(user["user_id"], request.account_type)
    available_balance = _available_balance(wallet)
    required_margin = broker_adapter.estimate_margin(request)
    order_type = _normalize_order_type(request.order_type)
    risk_profile = get_risk_profile(user["user_id"])

    max_position_size = int(risk_profile.get("max_position_size", 0) or 0)
    if max_position_size > 0 and request.quantity > max_position_size:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Max position size exceeded")

    if available_balance < required_margin:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient virtual funded balance")

    if request.account_type == AccountType.real and user.get("real_status") not in {None, "active", "running"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Real account is not approved")

    timestamp = now_utc()
    trade_id = new_uuid()
    entry_price = _entry_price(request)
    broker_result = broker_adapter.place_order(request)
    execution_status = broker_result.get("status", TradeExecutionStatus.pending)
    if execution_status == TradeExecutionStatus.failed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=broker_result.get("message") or "Broker order failed")

    is_open = execution_status in {TradeExecutionStatus.success, TradeExecutionStatus.demo, TradeExecutionStatus.pending}
    trade = {
        "trade_id": trade_id,
        "user_id": user["user_id"],
        "external_uid": user.get("external_uid"),
        "client_id": user["client_id"],
        "account_type": request.account_type.value,
        "symbol": request.symbol,
        "side": request.side.value,
        "amount": request.amount,
        "quantity": request.quantity,
        "entry_price": entry_price,
        "current_price": entry_price,
        "exit_price": None,
        "instrument_token": request.instrument_token,
        "option_type": request.option_type,
        "strike": request.strike,
        "order_type": order_type,
        "limit_price": request.limit_price,
        "trigger_price": request.trigger_price,
        "manual_stop_loss": request.stop_loss,
        "backend_stop_loss": request.trigger_price,
        "broker_order_id": broker_result.get("broker_order_id"),
        "execution_status": execution_status.value if hasattr(execution_status, "value") else str(execution_status),
        "status": "OPEN" if is_open else "REJECTED",
        "is_open": is_open,
        "required_margin": required_margin,
        "product": request.product,
        "created_at": timestamp,
        "updated_at": timestamp,
        "opened_at": timestamp,
        "closed_at": None,
        "profit_loss": 0.0,
        "broker_message": broker_result.get("message"),
    }
    trades.insert_one(trade)
    users.update_one(
        {"user_id": user["user_id"]},
        {"$inc": {_margin_field(request.account_type): required_margin}, "$set": {"updated_at": timestamp}},
    )
    _log_trade_event(
        trade_id,
        "trade_opened",
        {
            "user_id": user["user_id"],
            "client_id": user["client_id"],
            "account_type": request.account_type.value,
            "execution_status": trade["execution_status"],
            "required_margin": required_margin,
            "broker_order_id": trade["broker_order_id"],
        },
    )

    refreshed_wallet = get_wallet(user["user_id"], request.account_type)
    return {
        "trade_id": trade_id,
        "user_id": user["user_id"],
        "account_type": request.account_type,
        "status": execution_status,
        "balance": refreshed_wallet["balance"],
        "available_balance": refreshed_wallet.get("available_balance"),
        "broker_order_id": trade["broker_order_id"],
        "execution_price": entry_price,
        "message": broker_result.get("message") or "Trade placed",
    }


def estimate_margin(request: TradeRequest) -> Dict[str, Any]:
    user = get_user_or_404(request.user_id)
    wallet = get_wallet(user["user_id"], request.account_type)
    order_type = _normalize_order_type(request.order_type)
    normalized_request = request.model_copy(update={"order_type": order_type})
    required_margin = broker_adapter.estimate_margin(normalized_request)
    return {
        "required_margin": required_margin,
        "available_balance": _available_balance(wallet),
        "balance": float(wallet["balance"]),
        "quantity": int(request.quantity),
        "order_type": order_type,
        "side": request.side.value,
        "product": request.product or "I",
    }


def close_trade(request: CloseTradeRequest) -> Dict[str, Any]:
    trade = trades.find_one({"trade_id": request.trade_id})
    if not trade:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")
    if not trade.get("is_open", True):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Trade already closed")

    user = get_user_or_404(trade["user_id"])
    account_type = AccountType(trade["account_type"])
    entry_price = float(trade.get("entry_price") or trade.get("amount") or 0)
    exit_price = float(request.exit_price or request.current_price or trade.get("current_price") or entry_price)
    quantity = float(trade.get("quantity") or 0)
    profit_loss = float(request.profit_loss or _calculate_profit_loss(trade.get("side", "BUY"), entry_price, exit_price, quantity))

    broker_result = broker_adapter.close_order(trade)
    if broker_result.get("status") == TradeExecutionStatus.failed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=broker_result.get("message") or "Broker close failed")

    timestamp = now_utc()
    users.update_one(
        {"user_id": user["user_id"]},
        {
            "$inc": {
                _profit_field(account_type, profit_loss): abs(profit_loss),
                _margin_field(account_type): -float(trade.get("required_margin", 0) or 0),
            },
            "$set": {"updated_at": timestamp},
        },
    )
    trades.update_one(
        {"trade_id": request.trade_id},
        {
            "$set": {
                "is_open": False,
                "status": "CLOSED",
                "closed_at": timestamp,
                "updated_at": timestamp,
                "profit_loss": profit_loss,
                "exit_price": exit_price,
                "current_price": exit_price,
                "close_triggered_by": request.triggered_by,
                "close_broker_order_id": broker_result.get("broker_order_id"),
                "close_broker_message": broker_result.get("message"),
            }
        },
    )
    _log_trade_event(
        request.trade_id,
        "trade_closed",
        {
            "triggered_by": request.triggered_by,
            "profit_loss": profit_loss,
            "exit_price": exit_price,
            "broker_order_id": broker_result.get("broker_order_id"),
        },
    )
    refreshed_user = get_user_or_404(user["user_id"])
    _apply_risk_lock(refreshed_user, account_type)
    wallet = get_wallet(user["user_id"], account_type)
    return {
        "trade_id": request.trade_id,
        "user_id": user["user_id"],
        "account_type": account_type,
        "balance": wallet["balance"],
        "available_balance": wallet.get("available_balance"),
        "profit_loss": profit_loss,
        "message": "Trade closed and wallet updated",
    }


def list_trades(account_type: Optional[AccountType] = None, user_id: Optional[str] = None, include_closed: bool = True) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {}
    if account_type:
        query["account_type"] = account_type.value
    if user_id:
        user = get_user_or_404(user_id)
        query["user_id"] = user["user_id"]
    if not include_closed:
        query["is_open"] = True
    return [clean_dict(trade) for trade in trades.find(query).sort("created_at", -1)]


def list_portfolio(account_type: AccountType, user_id: str) -> List[Dict[str, Any]]:
    return list_trades(account_type=account_type, user_id=user_id, include_closed=False)


def list_trade_history(account_type: AccountType, user_id: str) -> List[Dict[str, Any]]:
    return list_trades(account_type=account_type, user_id=user_id, include_closed=True)


def get_trade(trade_id: str) -> Dict[str, Any]:
    trade = trades.find_one({"trade_id": trade_id})
    if not trade:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")
    return clean_dict(trade)
