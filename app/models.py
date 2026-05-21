from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class AccountType(str, Enum):
    challenge = "challenge"
    real = "real"


class AccountStatus(str, Enum):
    running = "running"
    passed = "passed"
    active = "active"
    suspended = "suspended"


class TradeSide(str, Enum):
    buy = "BUY"
    sell = "SELL"


class TradeExecutionStatus(str, Enum):
    pending = "pending"
    success = "success"
    failed = "failed"
    demo = "demo"


class MessageResponse(BaseModel):
    message: str


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    email: EmailStr
    mobile: str = Field(..., min_length=8, max_length=20)
    password: Optional[str] = Field(default=None, min_length=6)
    referral_code: Optional[str] = None
    mpin: Optional[str] = Field(default=None, min_length=4, max_length=6)
    client_id: Optional[str] = None
    external_uid: Optional[str] = None

    @field_validator("mobile")
    @classmethod
    def normalize_mobile(cls, value: str) -> str:
        return "".join(ch for ch in value.strip() if ch.isdigit() or ch == "+")


class RegisterResponse(BaseModel):
    message: str
    user_id: str
    client_id: str
    referral_code: str


class UserAccount(BaseModel):
    user_id: str
    client_id: str
    name: str
    email: EmailStr
    mobile: str
    active_plan_amount: Optional[float] = None
    challenge_status: AccountStatus = AccountStatus.running
    real_status: AccountStatus = AccountStatus.running
    challenge_virtual_capital: float = 0
    challenge_profit: float = 0
    challenge_loss: float = 0
    challenge_balance: float = 0
    real_capital: float = 0
    real_profit: float = 0
    real_loss: float = 0
    real_balance: float = 0
    real_margin_used: float = 0
    challenge_margin_used: float = 0
    trading_enabled: bool = True
    max_loss_limit: float = 0
    max_drawdown_limit: float = 0
    daily_loss_limit: float = 0
    max_position_size: int = 0
    created_at: datetime


class InitUserResponse(UserAccount):
    pass


class UserIdRequest(BaseModel):
    user_id: str


class PlanRequest(BaseModel):
    user_id: str
    amount: float = Field(..., gt=0)


class AdminWalletUpdateRequest(BaseModel):
    user_id: str
    amount: float = Field(..., ge=0)
    note: Optional[str] = None


class WalletResponse(BaseModel):
    user_id: str
    account_type: AccountType
    virtual_capital: Optional[float] = None
    real_capital: Optional[float] = None
    profit: float
    loss: float
    balance: float


class SupportCreateRequest(BaseModel):
    user_id: str
    message: str = Field(..., min_length=1, max_length=4000)


class SupportReplyRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class SupportMessage(BaseModel):
    sender: str
    message: str
    timestamp: datetime


class SupportTicketResponse(BaseModel):
    ticket_id: str
    complaint_id: str
    user_id: str
    user_name: str
    message: str
    status: str
    timestamp: datetime
    messages: List[SupportMessage] = Field(default_factory=list)


class AdminActivateResponse(BaseModel):
    user_id: str
    otp: str
    expires_in_seconds: int
    message: str


class VerifyOtpRequest(BaseModel):
    user_id: str
    otp: str = Field(..., min_length=4, max_length=6)


class ChangeMpinRequest(BaseModel):
    user_id: str
    otp: str = Field(..., min_length=4, max_length=6)
    new_mpin: str = Field(..., min_length=4, max_length=6)
    confirm_mpin: str = Field(..., min_length=4, max_length=6)


class MpinLoginRequest(BaseModel):
    user_id: str
    mpin: str = Field(..., min_length=4, max_length=6)


class TradeRequest(BaseModel):
    user_id: str
    symbol: str = Field(..., min_length=1)
    side: TradeSide = TradeSide.buy
    amount: float = Field(..., gt=0)
    quantity: int = Field(1, gt=0)
    entry_price: Optional[float] = Field(default=None, gt=0)
    account_type: AccountType = AccountType.challenge
    instrument_token: Optional[str] = None
    order_type: str = "MARKET"
    product: Optional[str] = None
    execute_on_broker: bool = True
    limit_price: Optional[float] = Field(default=None, gt=0)
    trigger_price: Optional[float] = Field(default=None, ge=0)
    stop_loss: Optional[float] = Field(default=None, gt=0)
    option_type: Optional[str] = None
    strike: Optional[float] = None
    client_id: Optional[str] = None
    external_uid: Optional[str] = None


class TradeResponse(BaseModel):
    trade_id: str
    user_id: str
    account_type: AccountType
    status: TradeExecutionStatus
    balance: float
    available_balance: Optional[float] = None
    broker_order_id: Optional[str] = None
    execution_price: Optional[float] = None
    message: str


class MarginEstimateRequest(BaseModel):
    user_id: str
    symbol: str = Field(..., min_length=1)
    side: TradeSide = TradeSide.buy
    quantity: int = Field(..., gt=0)
    entry_price: Optional[float] = Field(default=None, gt=0)
    account_type: AccountType = AccountType.real
    instrument_token: Optional[str] = None
    order_type: str = "MARKET"
    product: Optional[str] = None
    limit_price: Optional[float] = Field(default=None, gt=0)
    option_type: Optional[str] = None
    strike: Optional[float] = None


class MarginEstimateResponse(BaseModel):
    required_margin: float
    available_balance: float
    balance: float
    quantity: int
    order_type: str
    side: str
    product: str


class CloseTradeRequest(BaseModel):
    trade_id: str
    profit_loss: float = 0
    exit_price: Optional[float] = None
    current_price: Optional[float] = None
    triggered_by: str = "manual"


class CloseTradeResponse(BaseModel):
    trade_id: str
    user_id: str
    account_type: AccountType
    balance: float
    available_balance: Optional[float] = None
    profit_loss: float
    message: str


class BrokerConnectRequest(BaseModel):
    user_id: str
    broker_name: str = Field(..., min_length=1)
    account_id: str = Field(..., min_length=1)
    api_key: Optional[str] = None
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


class ReferralRecord(BaseModel):
    referrer_user_id: str
    referrer_name: str
    referred_user_id: str
    referred_name: str
    date_time: datetime
    registration_status: str
    payment_status: str


class AdminStatsResponse(BaseModel):
    total_users: int
    total_referrals: int
    open_support_tickets: int
    real_trades: int
    challenge_trades: int


class SyncUserResponse(BaseModel):
    message: str
    user_id: str
    client_id: str


class RiskProfileRequest(BaseModel):
    user_id: str
    max_loss_limit: Optional[float] = Field(default=None, ge=0)
    max_drawdown_limit: Optional[float] = Field(default=None, ge=0)
    daily_loss_limit: Optional[float] = Field(default=None, ge=0)
    max_position_size: Optional[int] = Field(default=None, ge=1)
    trading_enabled: Optional[bool] = None


class RiskProfileResponse(BaseModel):
    user_id: str
    client_id: str
    max_loss_limit: float
    max_drawdown_limit: float
    daily_loss_limit: float
    max_position_size: int
    trading_enabled: bool
    blocked_margin: float = 0


class MasterBrokerStatusResponse(BaseModel):
    broker_name: str
    connected: bool
    demo_mode: bool
    base_url: str
    account_id: Optional[str] = None
    has_access_token: bool
