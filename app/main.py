from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import check_connection, setup_indexes
from app.models import (
    AccountType,
    AdminActivateResponse,
    AdminStatsResponse,
    AdminWalletUpdateRequest,
    BrokerConnectRequest,
    BrokerConnection,
    BrokerConnectionResponse,
    ChangeMpinRequest,
    CloseTradeRequest,
    CloseTradeResponse,
    MasterBrokerStatusResponse,
    InitUserResponse,
    MarginEstimateRequest,
    MarginEstimateResponse,
    MessageResponse,
    MpinLoginRequest,
    PlanRequest,
    ReferralRecord,
    RiskProfileRequest,
    RiskProfileResponse,
    RegisterRequest,
    RegisterResponse,
    SyncUserResponse,
    SupportCreateRequest,
    SupportReplyRequest,
    SupportTicketResponse,
    TradeRequest,
    TradeResponse,
    UserAccount,
    UserIdRequest,
    VerifyOtpRequest,
    WalletResponse,
)
from app.services.account_service import (
    admin_stats,
    change_mpin,
    create_support_ticket,
    create_user,
    get_risk_profile,
    get_user_or_404,
    get_wallet,
    list_referrals,
    list_support_tickets,
    list_users,
    login_with_mpin,
    master_broker_status,
    reply_support_ticket,
    request_mpin_otp,
    serialize_user,
    set_active_plan,
    set_referral_payment,
    sync_user,
    update_wallet,
    update_risk_profile,
)
from app.services.broker_service import connect_broker, disconnect_broker, get_broker_status
from app.services.trade_service import close_trade as close_trade_service
from app.services.trade_service import estimate_margin as estimate_margin_service
from app.services.trade_service import get_trade, list_portfolio, list_trade_history, list_trades, open_trade

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    description="Production-ready MongoDB FastAPI backend for FundedX challenge and real trading accounts.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.parsed_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    setup_indexes()


@app.get("/")
def home() -> dict[str, object]:
    return {"message": "FundedX Trading Backend Running", "mongo": check_connection()}


@app.post("/register", response_model=RegisterResponse, status_code=201)
def register(request: RegisterRequest) -> dict[str, str]:
    return create_user(request)


@app.post("/init-user", response_model=InitUserResponse)
def init_user(request: RegisterRequest = Body(...)) -> dict:
    result = create_user(request)
    return serialize_user(get_user_or_404(result["user_id"]))


@app.post("/sync-user", response_model=SyncUserResponse)
def sync_backend_user(request: RegisterRequest) -> dict:
    return sync_user(request)


@app.get("/account/{user_id}", response_model=UserAccount)
def get_account(user_id: str) -> dict:
    return serialize_user(get_user_or_404(user_id))


@app.get("/wallet/{account_type}/{user_id}", response_model=WalletResponse)
def wallet(account_type: AccountType, user_id: str) -> dict:
    return get_wallet(user_id, account_type)


@app.post("/plan/active", response_model=dict)
def active_plan(request: PlanRequest) -> dict:
    return set_active_plan(request)


@app.post("/support/tickets", response_model=SupportTicketResponse, status_code=201)
def support_ticket(request: SupportCreateRequest) -> dict:
    return create_support_ticket(request)


@app.get("/support/tickets/{user_id}", response_model=list[SupportTicketResponse])
def user_support_tickets(user_id: str) -> list[dict]:
    return [ticket for ticket in list_support_tickets() if ticket["user_id"] == get_user_or_404(user_id)["user_id"]]


@app.post("/mpin/request-otp", response_model=AdminActivateResponse)
def mpin_request_otp(request: UserIdRequest) -> dict:
    return request_mpin_otp(request.user_id)


@app.post("/mpin/change", response_model=MessageResponse)
def mpin_change(request: ChangeMpinRequest) -> dict[str, str]:
    return change_mpin(request)


@app.post("/mpin/login", response_model=MessageResponse)
def mpin_login(request: MpinLoginRequest) -> dict[str, str]:
    return login_with_mpin(request)


@app.post("/trade", response_model=TradeResponse)
def trade(request: TradeRequest) -> dict:
    return open_trade(request)


@app.post("/trade/margin", response_model=MarginEstimateResponse)
def trade_margin(request: MarginEstimateRequest) -> dict:
    return estimate_margin_service(
        TradeRequest(
            user_id=request.user_id,
            symbol=request.symbol,
            side=request.side,
            amount=request.entry_price or request.limit_price or 1,
            quantity=request.quantity,
            entry_price=request.entry_price,
            account_type=request.account_type,
            instrument_token=request.instrument_token,
            order_type=request.order_type,
            product=request.product,
            limit_price=request.limit_price,
            option_type=request.option_type,
            strike=request.strike,
        )
    )


@app.post("/close-trade", response_model=CloseTradeResponse)
def close_trade(request: CloseTradeRequest) -> dict:
    return close_trade_service(request)


@app.get("/portfolio/{account_type}/{user_id}")
def portfolio(account_type: AccountType, user_id: str) -> list[dict]:
    return list_portfolio(account_type, user_id)


@app.get("/trades/history/{account_type}/{user_id}")
def trade_history(account_type: AccountType, user_id: str) -> list[dict]:
    return list_trade_history(account_type, user_id)


@app.get("/trade/{trade_id}")
def trade_status(trade_id: str) -> dict:
    return get_trade(trade_id)


@app.post("/broker/connect", response_model=BrokerConnectionResponse)
def broker_connect(request: BrokerConnectRequest) -> dict:
    return connect_broker(request)


@app.get("/broker/status/{user_id}", response_model=BrokerConnection)
def broker_status(user_id: str) -> dict:
    return get_broker_status(user_id)


@app.post("/broker/disconnect", response_model=MessageResponse)
def broker_disconnect(request: UserIdRequest) -> dict[str, str]:
    return disconnect_broker(request.user_id)


@app.get("/admin/stats", response_model=AdminStatsResponse)
def stats() -> AdminStatsResponse:
    return admin_stats()


@app.get("/admin/users", response_model=list[UserAccount])
def admin_users() -> list[dict]:
    return list_users()


@app.get("/admin/support", response_model=list[SupportTicketResponse])
def admin_support() -> list[dict]:
    return list_support_tickets()


@app.post("/admin/support/{complaint_id}/reply", response_model=SupportTicketResponse)
def admin_support_reply(complaint_id: str, request: SupportReplyRequest) -> dict:
    return reply_support_ticket(complaint_id, request)


@app.post("/admin/wallet/challenge", response_model=WalletResponse)
def admin_challenge_wallet(request: AdminWalletUpdateRequest) -> dict:
    return update_wallet(request.user_id, AccountType.challenge, request.amount, request.note)


@app.post("/admin/wallet/real", response_model=WalletResponse)
def admin_real_wallet(request: AdminWalletUpdateRequest) -> dict:
    return update_wallet(request.user_id, AccountType.real, request.amount, request.note)


@app.get("/admin/trades")
def admin_trades(account_type: AccountType | None = None, user_id: str | None = None) -> list[dict]:
    return list_trades(account_type, user_id)


@app.get("/admin/referrals", response_model=list[ReferralRecord])
def admin_referrals() -> list[dict]:
    return list_referrals()


@app.post("/admin/referrals/{referred_user_id}/payment", response_model=MessageResponse)
def admin_referral_payment(referred_user_id: str, paid: bool = True) -> dict[str, str]:
    return set_referral_payment(referred_user_id, paid)


@app.get("/admin/risk/{user_id}", response_model=RiskProfileResponse)
def admin_get_risk(user_id: str) -> dict:
    return get_risk_profile(user_id)


@app.post("/admin/risk", response_model=RiskProfileResponse)
def admin_set_risk(request: RiskProfileRequest) -> dict:
    return update_risk_profile(request.user_id, request.model_dump())


@app.get("/admin/master-broker/status", response_model=MasterBrokerStatusResponse)
def admin_master_broker_status() -> dict:
    return master_broker_status()


@app.post("/request-activation", response_model=MessageResponse)
def request_activation(request: UserIdRequest) -> dict[str, str]:
    return {"message": f"Activation request received for {request.user_id}"}


@app.post("/admin/activate", response_model=AdminActivateResponse)
def admin_activate(request: UserIdRequest) -> dict:
    return request_mpin_otp(request.user_id)


@app.post("/verify-otp", response_model=MessageResponse)
def verify_otp(request: VerifyOtpRequest) -> dict[str, str]:
    return {"message": "OTP endpoint reserved for activation workflows"}
