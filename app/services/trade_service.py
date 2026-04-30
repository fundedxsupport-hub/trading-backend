import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.models import CloseTradeRequest, Trade, TradeRequest
from app.services.broker_adapter import broker_adapter
from app.services.user_service import get_user_or_404
from app.storage import trades


def open_trade(request: TradeRequest) -> dict[str, object]:
    user = get_user_or_404(request.user_id)

    if not user.trading_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Trading is disabled for this account",
        )

    if user.balance < request.amount:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Low balance")

    broker_order_id = broker_adapter.place_order(request)

    user.balance -= request.amount
    trade_id = str(uuid.uuid4())
    trades[trade_id] = Trade(
        trade_id=trade_id,
        user_id=request.user_id,
        symbol=request.symbol,
        side=request.side,
        amount=request.amount,
        quantity=request.quantity,
        entry_price=request.entry_price,
        broker_order_id=broker_order_id,
        created_at=datetime.now(timezone.utc),
    )

    return {
        "trade_id": trade_id,
        "user_id": user.user_id,
        "balance": user.balance,
        "broker_order_id": broker_order_id,
        "message": "Trade placed",
    }


def close_trade(request: CloseTradeRequest) -> dict[str, object]:
    trade = trades.get(request.trade_id)
    if not trade:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")

    if not trade.is_open:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Trade already closed")

    user = get_user_or_404(trade.user_id)

    user.balance += trade.amount + request.profit_loss
    trade.is_open = False
    trade.closed_at = datetime.now(timezone.utc)
    trade.profit_loss = request.profit_loss

    return {
        "trade_id": trade.trade_id,
        "user_id": user.user_id,
        "balance": user.balance,
        "profit_loss": request.profit_loss,
        "message": "Trade closed",
    }
