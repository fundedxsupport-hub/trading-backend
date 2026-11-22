from typing import Any, Dict, List, Optional

import requests

from fastapi import HTTPException, status

from app.config import get_settings
from app.database import master_broker, trade_events, trades, users
from app.models import AccountType, AppCloseTradeRequest, AppTradeRequest, CloseTradeRequest, TradeExecutionStatus, TradeRequest, TradeSide
from app.services.account_service import get_risk_profile, get_user_default_account_type, get_user_or_404, get_wallet
from app.services.broker_adapter import broker_adapter
from app.utils import clean_dict, new_uuid, now_utc

settings = get_settings()
_INSTRUMENT_CACHE: Dict[str, str] = {}
DEFAULT_MAX_LOSS_PERCENT = 0.20


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


def _challenge_margin(request: TradeRequest) -> float:
    """Estimate virtual challenge exposure locally; never query the broker."""
    notional = _required_margin(request)
    return notional if request.side == TradeSide.buy else round(notional * 5, 2)


def _reserve_real_exposure(user: Dict[str, Any], exposure: float) -> bool:
    """Atomically reserve client exposure and, in live mode, master capacity."""
    if broker_adapter.is_live and not settings.upstox_shared_account_approved:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live shared-account trading is locked pending documented Upstox approval",
        )
    if broker_adapter.is_live and trades.count_documents({
        "account_type": AccountType.real.value,
        "is_open": True,
        "broker_live": {"$ne": True},
    }):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live trading is locked while simulated real-account trades remain open",
        )
    capital = _account_capital(user, AccountType.real)
    if capital <= 0:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Real account has no assigned capital")

    client_result = users.update_one(
        {
            "user_id": user["user_id"],
            "$expr": {
                "$lte": [
                    {"$add": [{"$ifNull": ["$real_exposure_used", 0]}, exposure]},
                    capital,
                ]
            },
        },
        {"$inc": {"real_exposure_used": exposure}, "$set": {"updated_at": now_utc()}},
    )
    if client_result.modified_count != 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Real trade exposure exceeds this client's assigned capital")

    if not broker_adapter.is_live:
        return False

    master_limit = float(settings.upstox_master_capital_limit or 0)
    if master_limit <= 0:
        users.update_one({"user_id": user["user_id"]}, {"$inc": {"real_exposure_used": -exposure}})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live trading is locked until UPSTOX_MASTER_CAPITAL_LIMIT is configured",
        )

    master_result = master_broker.update_one(
        {
            "broker_name": "upstox",
            "$expr": {
                "$lte": [
                    {"$add": [{"$ifNull": ["$reserved_real_exposure", 0]}, exposure]},
                    master_limit,
                ]
            },
        },
        {"$inc": {"reserved_real_exposure": exposure}, "$set": {"updated_at": now_utc()}},
    )
    if master_result.modified_count != 1:
        users.update_one({"user_id": user["user_id"]}, {"$inc": {"real_exposure_used": -exposure}})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Shared Upstox master account exposure cap would be exceeded")
    return True


def _release_real_exposure(trade: Dict[str, Any]) -> None:
    if not trade.get("real_exposure_reserved"):
        return
    exposure = float(trade.get("reserved_exposure") or 0)
    if exposure <= 0:
        return
    users.update_one({"user_id": trade["user_id"]}, {"$inc": {"real_exposure_used": -exposure}})
    if trade.get("master_exposure_reserved"):
        master_broker.update_one(
            {"broker_name": "upstox"},
            {"$inc": {"reserved_real_exposure": -exposure}, "$set": {"updated_at": now_utc()}},
        )


def _status_is_terminal(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "complete", "rejected", "cancelled", "cancelled after market order"
    }


def _set_broker_reconciliation_required(trade: Dict[str, Any], required: bool) -> None:
    trade_id = str(trade.get("trade_id") or "")
    if trade_id:
        trades.update_one(
            {"trade_id": trade_id, "is_open": True},
            {"$set": {"broker_status_unverified": required, "updated_at": now_utc()}},
        )
    user_id = str(trade.get("user_id") or "")
    if not user_id:
        return
    if required:
        users.update_one({"user_id": user_id}, {"$set": {"broker_reconciliation_required": True, "updated_at": now_utc()}})
        return
    unresolved = trades.count_documents({
        "user_id": user_id,
        "account_type": AccountType.real.value,
        "is_open": True,
        "broker_status_unverified": True,
    })
    if unresolved == 0:
        users.update_one({"user_id": user_id}, {"$set": {"broker_reconciliation_required": False, "updated_at": now_utc()}})


def _shrink_entry_reservation_to_filled(trade: Dict[str, Any], filled_quantity: int) -> None:
    if not trade.get("real_exposure_reserved"):
        return
    requested = max(1, int(trade.get("requested_quantity") or trade.get("quantity") or filled_quantity or 1))
    filled = min(requested, max(0, int(filled_quantity)))
    target_exposure = float(trade.get("entry_price") or 0) * filled
    old_exposure = float(trade.get("reserved_exposure") or 0)
    release_exposure = max(0.0, old_exposure - target_exposure)
    old_margin = float(trade.get("required_margin") or 0)
    target_margin = old_margin * filled / requested
    release_margin = max(0.0, old_margin - target_margin)
    if release_exposure or release_margin:
        users.update_one(
            {"user_id": trade["user_id"]},
            {"$inc": {"real_exposure_used": -release_exposure, _margin_field(AccountType.real): -release_margin}},
        )
        if trade.get("master_exposure_reserved") and release_exposure:
            master_broker.update_one(
                {"broker_name": "upstox"},
                {"$inc": {"reserved_real_exposure": -release_exposure}, "$set": {"updated_at": now_utc()}},
            )
    trade["reserved_exposure"] = max(0.0, old_exposure - release_exposure)
    trade["required_margin"] = max(0.0, old_margin - release_margin)
    trades.update_one(
        {"trade_id": trade["trade_id"], "is_open": True},
        {"$set": {"reserved_exposure": trade["reserved_exposure"], "required_margin": trade["required_margin"], "updated_at": now_utc()}},
    )


def _reconcile_live_entry(trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    order_id = str(trade.get("broker_order_id") or "")
    if order_id:
        details = broker_adapter.get_order_details(order_id)
    else:
        details = broker_adapter.get_order_history_by_tag(str(trade.get("broker_tag") or ""))
        if details and details.get("order_id"):
            order_id = str(details["order_id"])
    if not details:
        _set_broker_reconciliation_required(trade, True)
        return None
    filled = max(0, int(details.get("filled_quantity") or 0))
    status_text = str(details.get("status") or "").strip().lower()
    update: Dict[str, Any] = {
        "broker_order_id": order_id or trade.get("broker_order_id"),
        "filled_quantity": filled,
        "execution_status": status_text or trade.get("execution_status"),
        "entry_order_terminal": _status_is_terminal(status_text),
        "updated_at": now_utc(),
    }
    average_price = float(details.get("average_price") or 0)
    if average_price > 0:
        update["entry_price"] = average_price
        update["current_price"] = average_price
    if filled <= 0 and _status_is_terminal(status_text):
        update.update({"is_open": False, "status": "REJECTED" if status_text == "rejected" else "CANCELLED"})
    trades.update_one({"trade_id": trade["trade_id"], "is_open": True}, {"$set": update})
    trade.update(update)
    _set_broker_reconciliation_required(trade, False)
    if _status_is_terminal(status_text):
        _shrink_entry_reservation_to_filled(trade, filled)
    if filled <= 0 and _status_is_terminal(status_text):
        users.update_one(
            {"user_id": trade["user_id"]},
            {"$inc": {_margin_field(AccountType.real): -float(trade.get("required_margin", 0) or 0)}},
        )
        _release_real_exposure(trade)
        trades.update_one(
            {"trade_id": trade["trade_id"]},
            {"$set": {"real_exposure_reserved": False, "master_exposure_reserved": False}},
        )
    return details


def _apply_live_close_fill(trade: Dict[str, Any], details: Dict[str, Any]) -> bool:
    """Apply only exchange-confirmed close fills; keep any remainder open."""
    if not _status_is_terminal(details.get("status")):
        return False
    close_requested = int(trade.get("close_requested_quantity") or 0)
    close_filled = min(close_requested, max(0, int(details.get("filled_quantity") or 0)))
    if close_filled <= 0:
        clear_result = trades.update_one(
            {
                "trade_id": trade["trade_id"],
                "is_open": True,
                "close_pending": True,
                "close_broker_order_id": trade.get("close_broker_order_id"),
            },
            {"$set": {"close_broker_order_id": None, "close_pending": False, "updated_at": now_utc()}},
        )
        return getattr(clear_result, "modified_count", 0) == 1

    open_filled = max(0, int(trade.get("filled_quantity") or 0))
    closed_quantity = min(close_filled, open_filled)
    entry_price = float(trade.get("entry_price") or 0)
    exit_price = float(details.get("average_price") or trade.get("close_requested_price") or trade.get("current_price") or entry_price)
    delta_pnl = _calculate_profit_loss(str(trade.get("side", "BUY")), entry_price, exit_price, closed_quantity)
    original_quantity = max(1, int(trade.get("requested_quantity") or trade.get("quantity") or open_filled or 1))
    margin_release = float(trade.get("required_margin", 0) or 0) * closed_quantity / original_quantity
    exposure_release = min(float(trade.get("reserved_exposure", 0) or 0), entry_price * closed_quantity)
    user_update: Dict[str, Any] = {
        "$inc": {
            _profit_field(AccountType.real, delta_pnl): abs(delta_pnl),
            _margin_field(AccountType.real): -margin_release,
            "real_exposure_used": -exposure_release,
        },
        "$set": {"updated_at": now_utc()},
    }
    remaining = max(0, open_filled - closed_quantity)
    cumulative_pnl = float(trade.get("profit_loss", 0) or 0) + delta_pnl
    set_values: Dict[str, Any] = {
        "filled_quantity": remaining,
        "required_margin": max(0.0, float(trade.get("required_margin", 0) or 0) - margin_release),
        "reserved_exposure": max(0.0, float(trade.get("reserved_exposure", 0) or 0) - exposure_release),
        "profit_loss": cumulative_pnl,
        "exit_price": exit_price,
        "current_price": exit_price,
        "close_broker_order_id": None,
        "close_pending": False,
        "updated_at": now_utc(),
    }
    if remaining <= 0:
        set_values.update({"is_open": False, "status": "CLOSED", "closed_at": now_utc(), "real_exposure_reserved": False, "master_exposure_reserved": False})
    else:
        set_values.update({"is_open": True, "status": "OPEN"})
    apply_result = trades.update_one(
        {
            "trade_id": trade["trade_id"],
            "is_open": True,
            "close_pending": True,
            "close_broker_order_id": trade.get("close_broker_order_id"),
            "filled_quantity": open_filled,
        },
        {"$set": set_values},
    )
    if getattr(apply_result, "modified_count", 0) != 1:
        return False
    # The compare-and-set above makes concurrent risk-monitor or client-close
    # reconciliations idempotent before the separate wallet documents are changed.
    users.update_one({"user_id": trade["user_id"]}, user_update)
    if trade.get("master_exposure_reserved"):
        master_broker.update_one(
            {"broker_name": "upstox"},
            {"$inc": {"reserved_real_exposure": -exposure_release}, "$set": {"updated_at": now_utc()}},
        )
    trade.update(set_values)
    _log_trade_event(
        str(trade["trade_id"]),
        "broker_close_fill_reconciled",
        {
            "user_id": trade["user_id"],
            "client_id": trade.get("client_id"),
            "broker_order_id": trade.get("broker_order_id"),
            "close_broker_order_id": trade.get("close_broker_order_id"),
            "broker_tag": trade.get("broker_tag"),
            "close_broker_tag": trade.get("close_broker_tag"),
            "filled_quantity": closed_quantity,
            "remaining_quantity": remaining,
            "profit_loss": delta_pnl,
        },
    )
    if remaining <= 0:
        _apply_risk_lock(get_user_or_404(trade["user_id"]), AccountType.real)
    return remaining <= 0


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


def _account_capital(user: Dict[str, Any], account_type: AccountType) -> float:
    return float(user.get(_capital_field(account_type), 0) or 0)


def _max_loss_amount(user: Dict[str, Any], account_type: AccountType, risk_profile: Optional[Dict[str, Any]] = None) -> float:
    capital = _account_capital(user, account_type)
    if capital <= 0:
        return 0.0
    profile = risk_profile or {}
    configured = float(profile.get("max_loss_limit", user.get("max_loss_limit", 0)) or 0)
    default_limit = round(capital * DEFAULT_MAX_LOSS_PERCENT, 2)
    if configured <= 0 or configured == settings.default_max_loss_limit:
        return default_limit
    return configured if configured < default_limit else default_limit


def _current_total_loss(user: Dict[str, Any], account_type: AccountType) -> float:
    profit_field = "real_profit" if account_type == AccountType.real else "challenge_profit"
    loss_field = "real_loss" if account_type == AccountType.real else "challenge_loss"
    capital = _account_capital(user, account_type)
    realized_net = float(user.get(profit_field, 0) or 0) - float(user.get(loss_field, 0) or 0)
    unrealized_net = 0.0
    for trade in trades.find({"user_id": user["user_id"], "account_type": account_type.value, "is_open": True}):
        entry_price = float(trade.get("entry_price") or 0)
        current_price = float(trade.get("current_price") or entry_price)
        quantity = float(trade.get("filled_quantity", trade.get("quantity", 0)) or 0)
        pnl = _calculate_profit_loss(str(trade.get("side", "BUY")), entry_price, current_price, quantity)
        unrealized_net += pnl
    equity = capital + realized_net + unrealized_net
    return round(max(0.0, capital - equity), 2)


def _validate_stop_loss(user: Dict[str, Any], request: TradeRequest, risk_profile: Dict[str, Any]) -> None:
    if request.stop_loss is None:
        return
    max_loss = _max_loss_amount(user, request.account_type, risk_profile)
    if max_loss <= 0:
        return
    entry_price = _entry_price(request)
    quantity = float(request.quantity)
    sl_loss = -_calculate_profit_loss(request.side.value, entry_price, float(request.stop_loss), quantity)
    if sl_loss <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Stop loss is not valid for this order side")
    remaining_risk = max_loss - _current_total_loss(user, request.account_type)
    if sl_loss > remaining_risk:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Stop loss risk exceeds allowed 20% capital risk. Remaining risk: {max(0.0, remaining_risk):.2f}",
        )


def _risk_breach_reason(user: Dict[str, Any], account_type: AccountType) -> Optional[str]:
    max_loss_limit = _max_loss_amount(user, account_type)
    max_drawdown_limit = float(user.get("max_drawdown_limit", 0) or 0)
    current_loss = _current_total_loss(user, account_type)

    if max_loss_limit > 0 and current_loss >= max_loss_limit:
        return "20% max loss limit hit"
    if max_drawdown_limit > 0 and current_loss >= max_drawdown_limit:
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




def resolve_instrument_token(symbol: str) -> str:
    normalized_symbol = symbol.strip().upper()
    if "|" in normalized_symbol:
        return normalized_symbol

    instrument_map = settings.parsed_instrument_map
    if normalized_symbol in instrument_map:
        return instrument_map[normalized_symbol]
    if normalized_symbol in _INSTRUMENT_CACHE:
        return _INSTRUMENT_CACHE[normalized_symbol]

    if not settings.upstox_access_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upstox access token is not configured")

    url = f"{settings.upstox_base_url.rstrip('/')}/instruments/search"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {settings.upstox_access_token}",
    }
    params = {
        "query": normalized_symbol,
        "exchanges": "NSE",
        "segments": "EQ",
        "page_number": 1,
        "records": 10,
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Instrument lookup failed: {exc}")

    instruments = payload.get("data") or []
    exact_matches = [
        item for item in instruments
        if str(item.get("trading_symbol", "")).upper() == normalized_symbol
        and item.get("segment") == "NSE_EQ"
        and item.get("instrument_key")
    ]
    fallback_matches = [
        item for item in instruments
        if item.get("segment") == "NSE_EQ" and item.get("instrument_key")
    ]
    selected = exact_matches[0] if exact_matches else (fallback_matches[0] if fallback_matches else None)
    if not selected:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Instrument not found for symbol {normalized_symbol}")

    instrument_key = str(selected["instrument_key"])
    _INSTRUMENT_CACHE[normalized_symbol] = instrument_key
    return instrument_key


def open_app_trade(user: Dict[str, Any], request: AppTradeRequest) -> Dict[str, Any]:
    account_type = request.account_type or get_user_default_account_type(user)
    # Challenge trades are app-only. Avoid the Upstox instrument-search endpoint;
    # a locally configured token is optional for virtual trades.
    instrument_token = request.instrument_token
    if not instrument_token and account_type == AccountType.challenge:
        instrument_token = settings.parsed_instrument_map.get(request.symbol.strip().upper())
    elif not instrument_token:
        instrument_token = resolve_instrument_token(request.symbol)
    entry_price = request.limit_price if request.order_type.upper() == "LIMIT" and request.limit_price else request.entry_price
    amount = request.amount or ((entry_price or 1) * request.quantity)
    trade_request = TradeRequest(
        user_id=user["user_id"],
        symbol=request.symbol.strip().upper(),
        side=request.side,
        amount=amount,
        quantity=request.quantity,
        account_type=account_type,
        instrument_token=instrument_token,
        order_type=request.order_type,
        product=request.product,
        execute_on_broker=request.execute_on_broker,
        entry_price=entry_price,
        limit_price=request.limit_price,
        trigger_price=request.trigger_price,
        stop_loss=request.stop_loss,
        target_price=request.target_price,
        option_type=request.option_type,
        strike=request.strike,
    )
    return open_trade(trade_request)


def close_app_trade(user: Dict[str, Any], request: AppCloseTradeRequest) -> Dict[str, Any]:
    trade = trades.find_one({"trade_id": request.trade_id})
    if not trade:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")
    if trade.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Trade does not belong to the logged-in user")
    return close_trade(CloseTradeRequest(trade_id=request.trade_id, current_price=request.current_price, triggered_by="app"))


def open_trade(request: TradeRequest) -> Dict[str, Any]:
    user = get_user_or_404(request.user_id)
    if request.account_type == AccountType.real and user.get("broker_reconciliation_required"):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Real trading is locked until an Upstox order status is reconciled")
    if not user.get("trading_enabled", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Trading is disabled for this account")

    wallet = get_wallet(user["user_id"], request.account_type)
    available_balance = _available_balance(wallet)
    required_margin = (
        _challenge_margin(request)
        if request.account_type == AccountType.challenge
        else broker_adapter.estimate_margin(request)
    )
    order_type = _normalize_order_type(request.order_type)
    risk_profile = get_risk_profile(user["user_id"])
    max_loss = _max_loss_amount(user, request.account_type, risk_profile)
    if max_loss > 0 and _current_total_loss(user, request.account_type) >= max_loss:
        _apply_risk_lock(user, request.account_type)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="20% max loss limit hit. Trading is locked.")

    max_position_size = int(risk_profile.get("max_position_size", 0) or 0)
    if max_position_size > 0 and request.quantity > max_position_size:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Max position size exceeded")

    if available_balance < required_margin:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient virtual funded balance")

    _validate_stop_loss(user, request, risk_profile)

    if request.account_type == AccountType.real and user.get("real_status") not in {None, "active", "running"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Real account is not approved")

    timestamp = now_utc()
    trade_id = new_uuid()
    broker_tag = f"fx{trade_id.replace('-', '')}"
    entry_price = _entry_price(request)
    exposure = round(entry_price * float(request.quantity), 2)
    master_exposure_reserved = False
    if request.account_type == AccountType.real:
        master_exposure_reserved = _reserve_real_exposure(user, exposure)

    try:
        # Challenge execution is recorded only in the virtual ledger. Do not call
        # any broker adapter method here, even if a client sends execute_on_broker=true.
        broker_result = (
            {
                "status": TradeExecutionStatus.demo,
                "broker_order_id": None,
                "message": "Challenge trade recorded in the virtual account; no broker order was sent.",
            }
            if request.account_type == AccountType.challenge
            else broker_adapter.place_order(request.model_copy(update={"broker_tag": broker_tag}))
        )
    except Exception:
        if request.account_type == AccountType.real:
            _release_real_exposure({
                "user_id": user["user_id"],
                "real_exposure_reserved": True,
                "master_exposure_reserved": master_exposure_reserved,
                "reserved_exposure": exposure,
            })
        raise
    execution_status = broker_result.get("status", TradeExecutionStatus.pending)
    if request.account_type == AccountType.real and broker_adapter.is_live:
        # A successful HTTP placement only means Upstox accepted the request;
        # actual fill state is confirmed by the risk monitor.
        execution_status = TradeExecutionStatus.pending
    if execution_status == TradeExecutionStatus.failed:
        if request.account_type == AccountType.real:
            _release_real_exposure({
                "user_id": user["user_id"],
                "real_exposure_reserved": True,
                "master_exposure_reserved": master_exposure_reserved,
                "reserved_exposure": exposure,
            })
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=broker_result.get("message") or "Broker order failed")

    is_open = execution_status in {TradeExecutionStatus.success, TradeExecutionStatus.demo, TradeExecutionStatus.pending}
    trade = {
        "trade_id": trade_id,
        "user_id": user["user_id"],
        "external_uid": user.get("external_uid"),
        "client_id": user["client_id"],
        "account_type": request.account_type.value,
        "broker_live": request.account_type == AccountType.real and broker_adapter.is_live,
        "symbol": request.symbol,
        "side": request.side.value,
        "amount": request.amount,
        "quantity": request.quantity,
        "requested_quantity": request.quantity,
        "filled_quantity": 0 if request.account_type == AccountType.real and broker_adapter.is_live else request.quantity,
        "entry_order_terminal": not (request.account_type == AccountType.real and broker_adapter.is_live),
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
        "target_price": request.target_price,
        "max_loss_limit": max_loss,
        "risk_limit_percent": DEFAULT_MAX_LOSS_PERCENT * 100,
        "broker_order_id": broker_result.get("broker_order_id"),
        "broker_tag": broker_tag if request.account_type == AccountType.real else None,
        "execution_status": execution_status.value if hasattr(execution_status, "value") else str(execution_status),
        "status": "OPEN" if is_open else "REJECTED",
        "is_open": is_open,
        "required_margin": required_margin,
        "reserved_exposure": exposure if request.account_type == AccountType.real else 0.0,
        "real_exposure_reserved": request.account_type == AccountType.real,
        "master_exposure_reserved": master_exposure_reserved,
        "close_pending": False,
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
            "broker_tag": trade["broker_tag"],
            "execution_status": trade["execution_status"],
            "required_margin": required_margin,
            "broker_order_id": trade["broker_order_id"],
            "entry_price": entry_price,
            "stop_loss": request.stop_loss,
            "target_price": request.target_price,
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
    required_margin = (
        _challenge_margin(normalized_request)
        if request.account_type == AccountType.challenge
        else broker_adapter.estimate_margin(normalized_request)
    )
    return {
        "required_margin": required_margin,
        "available_balance": _available_balance(wallet),
        "balance": float(wallet["balance"]),
        "quantity": int(request.quantity),
        "order_type": order_type,
        "side": request.side.value,
        "product": request.product or "I",
    }


def _extract_feed_ltp(feed: Any) -> Optional[float]:
    if not isinstance(feed, dict):
        return None
    direct = feed.get("ltpc")
    if isinstance(direct, dict):
        value = direct.get("ltp")
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    full_feed = feed.get("fullFeed")
    if isinstance(full_feed, dict):
        market = full_feed.get("marketFF") or full_feed.get("indexFF")
        if isinstance(market, dict) and isinstance(market.get("ltpc"), dict):
            value = market["ltpc"].get("ltp")
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
    return None


def _live_price_map() -> Dict[str, float]:
    url = f"{settings.option_chain_base_url.rstrip('/')}/option-chain"
    try:
        response = requests.get(url, timeout=4)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return {}
    feeds = payload.get("feeds") if isinstance(payload, dict) else None
    if not isinstance(feeds, dict):
        return {}
    prices: Dict[str, float] = {}
    for instrument_key, feed in feeds.items():
        ltp = _extract_feed_ltp(feed)
        if ltp is not None and ltp > 0:
            prices[str(instrument_key)] = ltp
    return prices


def sync_live_risk_once() -> Dict[str, Any]:
    checked = 0
    updated = 0
    closed = 0
    now = now_utc()
    open_trades = list(trades.find({"account_type": AccountType.real.value, "is_open": True}))

    if broker_adapter.is_live:
        for trade in open_trades:
            if trade.get("broker_live") is not True:
                continue
            close_order_id = str(trade.get("close_broker_order_id") or "")
            if trade.get("close_pending") and not close_order_id:
                recovered = broker_adapter.get_order_history_by_tag(str(trade.get("close_broker_tag") or ""))
                if recovered and recovered.get("order_id"):
                    close_order_id = str(recovered["order_id"])
                    trades.update_one({"trade_id": trade["trade_id"], "is_open": True}, {"$set": {"close_broker_order_id": close_order_id, "updated_at": now_utc()}})
                    trade["close_broker_order_id"] = close_order_id
                else:
                    _set_broker_reconciliation_required(trade, True)
                    continue
            if close_order_id:
                close_details = broker_adapter.get_order_details(close_order_id)
                if close_details:
                    _set_broker_reconciliation_required(trade, False)
                    if _apply_live_close_fill(trade, close_details):
                        closed += 1
                else:
                    _set_broker_reconciliation_required(trade, True)
                continue
            if trade.get("broker_order_id") or trade.get("broker_tag"):
                _reconcile_live_entry(trade)

    open_trades = list(trades.find({"account_type": AccountType.real.value, "is_open": True}))
    prices = _live_price_map()
    if not prices:
        return {"checked": 0, "closed": closed, "updated": updated, "reason": "no_live_prices"}

    for trade in open_trades:
        instrument_key = str(trade.get("instrument_token") or trade.get("symbol") or "")
        current_price = prices.get(instrument_key)
        if current_price is None:
            continue
        checked += 1
        entry_price = float(trade.get("entry_price") or trade.get("amount") or 0)
        quantity = float(trade.get("filled_quantity", trade.get("quantity", 0)) or 0)
        if quantity <= 0:
            continue
        pnl = _calculate_profit_loss(str(trade.get("side", "BUY")), entry_price, current_price, quantity)
        trades.update_one(
            {"trade_id": trade["trade_id"], "is_open": True},
            {"$set": {"current_price": current_price, "unrealized_profit_loss": pnl, "updated_at": now}},
        )
        updated += 1

        if trade.get("close_after_entry_cancel") and trade.get("entry_order_terminal"):
            try:
                close_trade(
                    CloseTradeRequest(
                        trade_id=str(trade["trade_id"]),
                        current_price=current_price,
                        triggered_by=str(trade.get("close_triggered_by") or "app"),
                    )
                )
            except HTTPException:
                _log_trade_event(str(trade["trade_id"]), "close_after_entry_cancel_failed", {"current_price": current_price})
            continue

        close_reason: Optional[str] = None
        side = str(trade.get("side", "BUY")).upper()
        stop_loss = trade.get("manual_stop_loss")
        target = trade.get("target_price")
        if stop_loss is not None:
            stop = float(stop_loss)
            if (side == TradeSide.buy.value and current_price <= stop) or (side == TradeSide.sell.value and current_price >= stop):
                close_reason = "stop_loss_hit"
        if close_reason is None and target is not None:
            target_price = float(target)
            if (side == TradeSide.buy.value and current_price >= target_price) or (side == TradeSide.sell.value and current_price <= target_price):
                close_reason = "target_hit"

        user = get_user_or_404(str(trade["user_id"]))
        max_loss = _max_loss_amount(user, AccountType.real)
        if close_reason is None and max_loss > 0:
            current_loss = _current_total_loss(user, AccountType.real)
            if current_loss >= max_loss:
                _apply_risk_lock(user, AccountType.real)
                close_reason = "20_percent_max_loss_hit"

        if close_reason is None:
            continue

        try:
            close_trade(
                CloseTradeRequest(
                    trade_id=str(trade["trade_id"]),
                    current_price=current_price,
                    triggered_by=close_reason,
                )
            )
            latest_trade = trades.find_one({"trade_id": trade["trade_id"]}) or {}
            if not latest_trade.get("is_open", True):
                closed += 1
        except HTTPException:
            _log_trade_event(str(trade["trade_id"]), "auto_close_failed", {"reason": close_reason, "current_price": current_price})

    return {"checked": checked, "closed": closed, "updated": updated}


def _close_live_trade(trade: Dict[str, Any], request: CloseTradeRequest, user: Dict[str, Any]) -> Dict[str, Any]:
    close_order_id = str(trade.get("close_broker_order_id") or "")
    if not close_order_id and trade.get("close_pending") and trade.get("close_broker_tag"):
        recovered = broker_adapter.get_order_history_by_tag(str(trade["close_broker_tag"]))
        if recovered and recovered.get("order_id"):
            close_order_id = str(recovered["order_id"])
            trades.update_one({"trade_id": trade["trade_id"], "is_open": True}, {"$set": {"close_broker_order_id": close_order_id, "updated_at": now_utc()}})
            trade["close_broker_order_id"] = close_order_id
        else:
            _set_broker_reconciliation_required(trade, True)
            wallet = get_wallet(user["user_id"], AccountType.real)
            return {"trade_id": request.trade_id, "user_id": user["user_id"], "account_type": AccountType.real, "balance": wallet["balance"], "available_balance": wallet.get("available_balance"), "profit_loss": 0.0, "message": "Close order is still being reconciled by its unique tag; no duplicate was sent."}
    if close_order_id:
        details = broker_adapter.get_order_details(close_order_id)
        if details:
            _set_broker_reconciliation_required(trade, False)
        else:
            _set_broker_reconciliation_required(trade, True)
        if details and _status_is_terminal(details.get("status")):
            _apply_live_close_fill(trade, details)
            refreshed = trades.find_one({"trade_id": trade["trade_id"]}) or trade
            wallet = get_wallet(user["user_id"], AccountType.real)
            return {
                "trade_id": request.trade_id,
                "user_id": user["user_id"],
                "account_type": AccountType.real,
                "balance": wallet["balance"],
                "available_balance": wallet.get("available_balance"),
                "profit_loss": float(refreshed.get("profit_loss", 0) or 0),
                "message": "Broker close order reconciled; any remaining filled quantity stays open.",
            }
        return {
            "trade_id": request.trade_id,
            "user_id": user["user_id"],
            "account_type": AccountType.real,
            "balance": get_wallet(user["user_id"], AccountType.real)["balance"],
            "available_balance": get_wallet(user["user_id"], AccountType.real).get("available_balance"),
            "profit_loss": 0.0,
            "message": "Broker close is still pending confirmation.",
        }

    order_id = str(trade.get("broker_order_id") or "")
    entry_details = broker_adapter.get_order_details(order_id)
    if not entry_details:
        _set_broker_reconciliation_required(trade, True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Unable to verify the Upstox entry fill; close was not submitted")
    _set_broker_reconciliation_required(trade, False)

    entry_status = str(entry_details.get("status") or "").strip().lower()
    filled = max(0, int(entry_details.get("filled_quantity") or 0))
    pending = max(0, int(entry_details.get("pending_quantity") or 0))
    if pending > 0 and not _status_is_terminal(entry_status):
        if not trade.get("entry_cancel_requested"):
            if not broker_adapter.cancel_order(order_id):
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not cancel the unfilled Upstox remainder; trade remains open")
            trades.update_one(
                {"trade_id": trade["trade_id"], "is_open": True},
                {"$set": {"entry_cancel_requested": True, "close_after_entry_cancel": True, "close_triggered_by": request.triggered_by, "updated_at": now_utc()}},
            )
        latest = broker_adapter.get_order_details(order_id)
        if not latest:
            _set_broker_reconciliation_required(trade, True)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Entry cancellation is pending confirmation")
        entry_details = latest
        entry_status = str(latest.get("status") or "").strip().lower()
        filled = max(0, int(latest.get("filled_quantity") or 0))
        pending = max(0, int(latest.get("pending_quantity") or 0))
        if pending > 0 and not _status_is_terminal(entry_status):
            return {
                "trade_id": request.trade_id,
                "user_id": user["user_id"],
                "account_type": AccountType.real,
                "balance": get_wallet(user["user_id"], AccountType.real)["balance"],
                "available_balance": get_wallet(user["user_id"], AccountType.real).get("available_balance"),
                "profit_loss": 0.0,
                "message": "Unfilled Upstox quantity is being cancelled; close waits for confirmation.",
            }

    if not _status_is_terminal(entry_status) and pending > 0:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Upstox entry still has an unfilled remainder")
    if filled <= 0:
        users.update_one(
            {"user_id": user["user_id"]},
            {"$inc": {_margin_field(AccountType.real): -float(trade.get("required_margin", 0) or 0)}},
        )
        _release_real_exposure(trade)
        trades.update_one(
            {"trade_id": trade["trade_id"], "is_open": True},
            {"$set": {"is_open": False, "status": "CANCELLED", "filled_quantity": 0, "real_exposure_reserved": False, "master_exposure_reserved": False, "closed_at": now_utc(), "updated_at": now_utc()}},
        )
        wallet = get_wallet(user["user_id"], AccountType.real)
        return {"trade_id": request.trade_id, "user_id": user["user_id"], "account_type": AccountType.real, "balance": wallet["balance"], "available_balance": wallet.get("available_balance"), "profit_loss": 0.0, "message": "Upstox confirmed that no quantity filled; the app trade is cancelled."}

    average_price = float(entry_details.get("average_price") or 0)
    if average_price > 0:
        trade["entry_price"] = average_price
    trade["filled_quantity"] = filled
    if _status_is_terminal(entry_status):
        _shrink_entry_reservation_to_filled(trade, filled)
    trades.update_one(
        {"trade_id": trade["trade_id"], "is_open": True},
        {"$set": {"filled_quantity": filled, "entry_price": trade.get("entry_price"), "entry_order_terminal": _status_is_terminal(entry_status), "updated_at": now_utc()}},
    )

    exit_reference_price = float(request.current_price or trade.get("current_price") or trade.get("entry_price") or 0)
    close_tag = f"fxc{new_uuid().replace('-', '')[:30]}"
    close_fields = {
        "close_broker_tag": close_tag,
        "close_requested_quantity": filled,
        "close_requested_price": exit_reference_price,
        "close_triggered_by": request.triggered_by,
        "close_pending": True,
        "close_after_entry_cancel": False,
        "updated_at": now_utc(),
    }
    claim_result = trades.update_one(
        {
            "trade_id": trade["trade_id"],
            "is_open": True,
            "close_pending": {"$ne": True},
            "close_broker_order_id": None,
        },
        {"$set": close_fields},
    )
    if getattr(claim_result, "modified_count", 0) != 1:
        wallet = get_wallet(user["user_id"], AccountType.real)
        return {"trade_id": request.trade_id, "user_id": user["user_id"], "account_type": AccountType.real, "balance": wallet["balance"], "available_balance": wallet.get("available_balance"), "profit_loss": 0.0, "message": "Another close request already owns this trade's pending close; no duplicate was sent."}
    trade.update(close_fields)
    close_result = broker_adapter.close_order({**trade, "close_quantity": filled, "filled_quantity": filled})
    if close_result.get("status") == TradeExecutionStatus.failed:
        trades.update_one(
            {"trade_id": trade["trade_id"], "is_open": True, "close_broker_tag": close_tag},
            {"$set": {"close_broker_tag": None, "close_pending": False, "updated_at": now_utc()}},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=close_result.get("message") or "Broker close failed")
    close_id = str(close_result.get("broker_order_id") or "")
    if not close_id:
        _set_broker_reconciliation_required(trade, True)
        return {
            "trade_id": request.trade_id,
            "user_id": user["user_id"],
            "account_type": AccountType.real,
            "balance": get_wallet(user["user_id"], AccountType.real)["balance"],
            "available_balance": get_wallet(user["user_id"], AccountType.real).get("available_balance"),
            "profit_loss": 0.0,
            "message": "Close response is uncertain; backend is checking Upstox by its unique close tag and will not duplicate it.",
        }
    trades.update_one(
        {"trade_id": trade["trade_id"], "is_open": True},
        {"$set": {"close_broker_order_id": close_id, "close_requested_quantity": filled, "close_requested_price": exit_reference_price, "close_triggered_by": request.triggered_by, "close_pending": True, "updated_at": now_utc()}},
    )
    trade.update({"close_broker_order_id": close_id, "close_requested_quantity": filled, "close_requested_price": exit_reference_price, "close_triggered_by": request.triggered_by, "close_pending": True})
    close_details = broker_adapter.get_order_details(close_id)
    if close_details:
        _set_broker_reconciliation_required(trade, False)
    else:
        _set_broker_reconciliation_required(trade, True)
    if close_details and _status_is_terminal(close_details.get("status")):
        _apply_live_close_fill(trade, close_details)
        refreshed = trades.find_one({"trade_id": trade["trade_id"]}) or trade
        wallet = get_wallet(user["user_id"], AccountType.real)
        return {
            "trade_id": request.trade_id,
            "user_id": user["user_id"],
            "account_type": AccountType.real,
            "balance": wallet["balance"],
            "available_balance": wallet.get("available_balance"),
            "profit_loss": float(refreshed.get("profit_loss", 0) or 0),
            "message": "Upstox close fill confirmed; remaining quantity, if any, remains open.",
        }
    wallet = get_wallet(user["user_id"], AccountType.real)
    return {
        "trade_id": request.trade_id,
        "user_id": user["user_id"],
        "account_type": AccountType.real,
        "balance": wallet["balance"],
        "available_balance": wallet.get("available_balance"),
        "profit_loss": 0.0,
        "message": "Upstox close order submitted; app will wait for fill confirmation.",
    }


def close_trade(request: CloseTradeRequest) -> Dict[str, Any]:
    trade = trades.find_one({"trade_id": request.trade_id})
    if not trade:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")
    if not trade.get("is_open", True):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Trade already closed")

    user = get_user_or_404(trade["user_id"])
    account_type = AccountType(trade["account_type"])
    if account_type == AccountType.real and broker_adapter.is_live and trade.get("broker_live") is True:
        return _close_live_trade(trade, request, user)
    entry_price = float(trade.get("entry_price") or trade.get("amount") or 0)
    exit_price = float(request.exit_price or request.current_price or trade.get("current_price") or entry_price)
    quantity = float(trade.get("quantity") or 0)
    profit_loss = float(request.profit_loss or _calculate_profit_loss(trade.get("side", "BUY"), entry_price, exit_price, quantity))

    # Closing a challenge trade updates only its virtual ledger entry.
    broker_result = (
        {
            "status": TradeExecutionStatus.demo,
            "broker_order_id": None,
            "message": "Challenge trade closed in the virtual account; no broker order was sent.",
        }
        if account_type == AccountType.challenge or (account_type == AccountType.real and not trade.get("broker_live"))
        else broker_adapter.close_order(trade)
    )
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
                "real_exposure_reserved": False,
                "master_exposure_reserved": False,
            }
        },
    )
    _release_real_exposure(trade)
    _log_trade_event(
        request.trade_id,
        "trade_closed",
        {
            "user_id": trade.get("user_id"),
            "client_id": trade.get("client_id"),
            "triggered_by": request.triggered_by,
            "profit_loss": profit_loss,
            "exit_price": exit_price,
            "broker_order_id": broker_result.get("broker_order_id"),
            "broker_tag": trade.get("broker_tag"),
            "close_broker_tag": trade.get("close_broker_tag"),
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

