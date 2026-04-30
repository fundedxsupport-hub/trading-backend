from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class AccountType(str, Enum):
    challenge = "challenge"
    funded = "funded"


class AccountStatus(str, Enum):
    running = "running"
    passed = "passed"
    active = "active"


class TradeSide(str, Enum):
    buy = "BUY"
    sell = "SELL"


class UserAccount(BaseModel):
    user_id: str
    account_type: AccountType = AccountType.challenge
    status: AccountStatus = AccountStatus.running
    balance: float = 100000.0
    trading_enabled: bool = True
    otp: Optional[str] = None
    otp_expires_at: Optional[datetime] = None


class Trade(BaseModel):
    trade_id: str
    user_id: str
    symbol: str
    side: TradeSide
    amount: float
    quantity: float = 1
    entry_price: Optional[float] = None
    broker_order_id: Optional[str] = None
    is_open: bool = True
    created_at: datetime
    closed_at: Optional[datetime] = None
    profit_loss: Optional[float] = None


class InitUserResponse(UserAccount):
    pass


class TradeRequest(BaseModel):
    user_id: str
    symbol: str = Field(..., min_length=1)
    side: TradeSide = TradeSide.buy
    amount: float = Field(..., gt=0)
    quantity: float = Field(1, gt=0)
    entry_price: Optional[float] = Field(default=None, gt=0)
    execute_on_broker: bool = False


class TradeResponse(BaseModel):
    trade_id: str
    user_id: str
    balance: float
    broker_order_id: Optional[str] = None
    message: str


class CloseTradeRequest(BaseModel):
    trade_id: str
    profit_loss: float = 0


class CloseTradeResponse(BaseModel):
    trade_id: str
    user_id: str
    balance: float
    profit_loss: float
    message: str


class UserIdRequest(BaseModel):
    user_id: str


class AdminActivateResponse(BaseModel):
    user_id: str
    otp: str
    expires_in_seconds: int
    message: str


class VerifyOtpRequest(BaseModel):
    user_id: str
    otp: str = Field(..., min_length=6, max_length=6)


class MessageResponse(BaseModel):
    message: str


class BrokerConnectRequest(BaseModel):
    user_id: str
    broker_name: str = Field(..., min_length=1)
    account_id: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    access_token: str = Field(..., min_length=1)
    base_url: Optional[str] = None


class BrokerConnection(BaseModel):
    user_id: str
    broker_name: str
    account_id: str
    base_url: Optional[str] = None
    connected: bool = True


class BrokerConnectionResponse(BrokerConnection):
    message: str
