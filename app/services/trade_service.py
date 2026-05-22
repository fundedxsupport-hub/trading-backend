from typing import Any, Dict, List, Optional

import requests

from fastapi import HTTPException, status

from app.config import get_settings
from app.database import trade_events, trades, users
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
    realized_loss = float(user.get("real_loss" if account_type == AccountType.real else "challenge_loss", 0) or 0)
    open_loss = 0.0
    for trade in trades.find({"user_id": user["user_id"], "account_type": account_type.value, "is_open": True}):
        entry_price = float(trade.get("entry_price") or 0)
        current_price = float(trade.get("current_price") or entry_price)
        quantity = float(trade.get("quantity") or 0)
        pnl = _calculate_profit_loss(str(trade.get("side", "BUY")), entry_price, current_price, quantity)
        if pnl < 0:
            open_loss += abs(pnl)
    return round(realized_loss + open_loss, 2)


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
    wallet = get_wallet(user["user_id"], account_type)
    balance = float(wallet["balance"])
    capital = _account_capital(user, account_type)
    max_loss_limit = _max_loss_amount(user, account_type)
    max_drawdown_limit = float(user.get("max_drawdown_limit", 0) or 0)

    if max_loss_limit > 0 and balance <= capital - max_loss_limit:
        return "20% max loss limit hit"
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
    instrument_token = request.instrument_token or resolve_instrument_token(request.symbol)
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
    if not user.get("trading_enabled", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Trading is disabled for this account")

    wallet = get_wallet(user["user_id"], request.account_type)
    available_balance = _available_balance(wallet)
    required_margin = broker_adapter.estimate_margin(request)
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
        "target_price": request.target_price,
        "max_loss_limit": max_loss,
        "risk_limit_percent": DEFAULT_MAX_LOSS_PERCENT * 100,
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
    prices = _live_price_map()
    if not prices:
        return {"checked": 0, "closed": 0, "updated": 0, "reason": "no_live_prices"}

    checked = 0
    updated = 0
    closed = 0
    now = now_utc()
    open_trades = list(trades.find({"account_type": AccountType.real.value, "is_open": True}))

    for trade in open_trades:
        instrument_key = str(trade.get("instrument_token") or trade.get("symbol") or "")
        current_price = prices.get(instrument_key)
        if current_price is None:
            continue
        checked += 1
        entry_price = float(trade.get("entry_price") or trade.get("amount") or 0)
        quantity = float(trade.get("quantity") or 0)
        pnl = _calculate_profit_loss(str(trade.get("side", "BUY")), entry_price, current_price, quantity)
        trades.update_one(
            {"trade_id": trade["trade_id"], "is_open": True},
            {"$set": {"current_price": current_price, "unrealized_profit_loss": pnl, "updated_at": now}},
        )
        updated += 1

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
            closed += 1
        except HTTPException:
            _log_trade_event(str(trade["trade_id"]), "auto_close_failed", {"reason": close_reason, "current_price": current_price})

    return {"checked": checked, "closed": closed, "updated": updated}


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

