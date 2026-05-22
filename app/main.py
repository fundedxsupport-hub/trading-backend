import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pymongo.errors import PyMongoError

from app.config import get_settings
from app.database import check_connection, close_connection, setup_indexes, sync_master_broker
from app.models import (
    AccountType,
    AppCloseTradeRequest,
    AppTradeRequest,
    AdminActivateResponse,
    AdminStatsResponse,
    AdminWalletUpdateRequest,
    ChangeMpinRequest,
    CloseTradeRequest,
    CloseTradeResponse,
    InitUserResponse,
    MarginEstimateRequest,
    MarginEstimateResponse,
    MasterBrokerStatusResponse,
    MessageResponse,
    MpinLoginRequest,
    PlanRequest,
    ReferralRecord,
    RiskProfileRequest,
    RiskProfileResponse,
    RegisterRequest,
    RegisterResponse,
    SupportCreateRequest,
    SupportReplyRequest,
    SupportTicketResponse,
    SyncUserResponse,
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
    get_user_by_access_token,
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
from app.services.market_service import get_option_chain, list_option_contracts
from app.services.trade_service import close_app_trade
from app.services.trade_service import close_trade as close_trade_service
from app.services.trade_service import estimate_margin as estimate_margin_service
from app.services.trade_service import get_trade, list_portfolio, list_trade_history, list_trades, open_app_trade, open_trade, sync_live_risk_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()
bearer_scheme = HTTPBearer(auto_error=False)


async def _risk_monitor_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(sync_live_risk_once)
            if result.get("closed"):
                logger.info("Risk monitor closed trades: %s", result)
        except Exception:
            logger.exception("Risk monitor failed")
        await asyncio.sleep(max(0.5, float(settings.risk_monitor_interval_seconds)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s", settings.app_name)
    risk_task = None
    try:
        setup_indexes()
        sync_master_broker()
        risk_task = asyncio.create_task(_risk_monitor_loop())
        logger.info("Startup complete")
    except PyMongoError:
        logger.exception("MongoDB startup setup failed")
    yield
    if risk_task:
        risk_task.cancel()
        try:
            await risk_task
        except asyncio.CancelledError:
            pass
    logger.info("Shutting down %s", settings.app_name)
    close_connection()


app = FastAPI(
    title=settings.app_name,
    version="2.1.0",
    description="Production-ready MongoDB FastAPI backend for FundedX challenge and master Upstox trading.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.parsed_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)




def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> Dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization bearer token")

    return get_user_by_access_token(credentials.credentials.strip())


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    logger.warning("HTTP error %s on %s: %s", exc.status_code, request.url.path, exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(PyMongoError)
async def mongo_exception_handler(request: Request, exc: PyMongoError) -> JSONResponse:
    logger.exception("MongoDB error on %s", request.url.path)
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": "Database unavailable"})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal server error"})


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
        "mongo": check_connection(),
        "broker": master_broker_status(),
    }


@app.get("/")
def home() -> Dict[str, Any]:
    return {"message": "FundedX Trading Backend Running", "mongo": check_connection()}


@app.post("/register", response_model=RegisterResponse, status_code=201)
def register(request: RegisterRequest) -> Dict[str, str]:
    return create_user(request)


@app.post("/init-user", response_model=InitUserResponse)
def init_user(request: RegisterRequest = Body(...)) -> Dict[str, Any]:
    result = create_user(request)
    return serialize_user(get_user_or_404(result["user_id"]))


@app.post("/sync-user", response_model=SyncUserResponse)
def sync_backend_user(request: RegisterRequest) -> Dict[str, Any]:
    return sync_user(request)


@app.get("/account/{user_id}", response_model=UserAccount)
def get_account(user_id: str) -> Dict[str, Any]:
    return serialize_user(get_user_or_404(user_id))


@app.get("/wallet/{account_type}/{user_id}", response_model=WalletResponse)
def wallet(account_type: AccountType, user_id: str) -> Dict[str, Any]:
    return get_wallet(user_id, account_type)


@app.post("/plan/active", response_model=Dict[str, Any])
def active_plan(request: PlanRequest) -> Dict[str, Any]:
    return set_active_plan(request)


@app.post("/support/tickets", response_model=SupportTicketResponse, status_code=201)
def support_ticket(request: SupportCreateRequest) -> Dict[str, Any]:
    return create_support_ticket(request)


@app.get("/support/tickets/{user_id}", response_model=List[SupportTicketResponse])
def user_support_tickets(user_id: str) -> List[Dict[str, Any]]:
    user = get_user_or_404(user_id)
    return [ticket for ticket in list_support_tickets() if ticket["user_id"] == user["user_id"]]


@app.post("/mpin/request-otp", response_model=AdminActivateResponse)
def mpin_request_otp(request: UserIdRequest) -> Dict[str, Any]:
    return request_mpin_otp(request.user_id)


@app.post("/mpin/change", response_model=MessageResponse)
def mpin_change(request: ChangeMpinRequest) -> Dict[str, str]:
    return change_mpin(request)


@app.post("/mpin/login")
def mpin_login(request: MpinLoginRequest) -> Dict[str, Any]:
    return login_with_mpin(request)



@app.get("/market/option-contracts")
def option_contracts(
    underlying: str,
    expiry_date: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    return list_option_contracts(underlying, expiry_date)


@app.get("/market/option-chain")
def option_chain(
    underlying: str,
    expiry_date: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    return get_option_chain(underlying, expiry_date)

@app.post("/trade", response_model=TradeResponse)
def trade(request: AppTradeRequest, current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    return open_app_trade(current_user, request)


@app.post("/admin/trade/manual", response_model=TradeResponse, include_in_schema=False)
def manual_trade(request: TradeRequest) -> Dict[str, Any]:
    return open_trade(request)


@app.post("/trade/margin", response_model=MarginEstimateResponse)
def trade_margin(request: MarginEstimateRequest) -> Dict[str, Any]:
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
def close_trade(request: AppCloseTradeRequest, current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    return close_app_trade(current_user, request)


@app.post("/admin/close-trade/manual", response_model=CloseTradeResponse, include_in_schema=False)
def manual_close_trade(request: CloseTradeRequest) -> Dict[str, Any]:
    return close_trade_service(request)


@app.get("/portfolio/{account_type}/{user_id}")
def portfolio(account_type: AccountType, user_id: str) -> List[Dict[str, Any]]:
    return list_portfolio(account_type, user_id)


@app.get("/trades/history/{account_type}/{user_id}")
def trade_history(account_type: AccountType, user_id: str) -> List[Dict[str, Any]]:
    return list_trade_history(account_type, user_id)


@app.get("/trade/{trade_id}")
def trade_status(trade_id: str) -> Dict[str, Any]:
    return get_trade(trade_id)


@app.post("/admin/risk/sync-live")
def admin_sync_live_risk() -> Dict[str, Any]:
    return sync_live_risk_once()


@app.get("/broker/status", response_model=MasterBrokerStatusResponse)
def broker_status() -> Dict[str, Any]:
    return master_broker_status()


@app.get("/admin/stats", response_model=AdminStatsResponse)
def stats() -> AdminStatsResponse:
    return admin_stats()


@app.get("/admin/users", response_model=List[UserAccount])
def admin_users() -> List[Dict[str, Any]]:
    return list_users()


@app.get("/admin/support", response_model=List[SupportTicketResponse])
def admin_support() -> List[Dict[str, Any]]:
    return list_support_tickets()


@app.post("/admin/support/{complaint_id}/reply", response_model=SupportTicketResponse)
def admin_support_reply(complaint_id: str, request: SupportReplyRequest) -> Dict[str, Any]:
    return reply_support_ticket(complaint_id, request)


@app.post("/admin/wallet/challenge", response_model=WalletResponse)
def admin_challenge_wallet(request: AdminWalletUpdateRequest) -> Dict[str, Any]:
    return update_wallet(request.user_id, AccountType.challenge, request.amount, request.note)


@app.post("/admin/wallet/real", response_model=WalletResponse)
def admin_real_wallet(request: AdminWalletUpdateRequest) -> Dict[str, Any]:
    return update_wallet(request.user_id, AccountType.real, request.amount, request.note)


@app.get("/admin/trades")
def admin_trades(account_type: Optional[AccountType] = None, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return list_trades(account_type, user_id)


@app.get("/admin/referrals", response_model=List[ReferralRecord])
def admin_referrals() -> List[Dict[str, Any]]:
    return list_referrals()


@app.post("/admin/referrals/{referred_user_id}/payment", response_model=MessageResponse)
def admin_referral_payment(referred_user_id: str, paid: bool = True) -> Dict[str, str]:
    return set_referral_payment(referred_user_id, paid)


@app.get("/admin/risk/{user_id}", response_model=RiskProfileResponse)
def admin_get_risk(user_id: str) -> Dict[str, Any]:
    return get_risk_profile(user_id)


@app.post("/admin/risk", response_model=RiskProfileResponse)
def admin_set_risk(request: RiskProfileRequest) -> Dict[str, Any]:
    return update_risk_profile(request.user_id, request.model_dump())


@app.get("/admin/master-broker/status", response_model=MasterBrokerStatusResponse)
def admin_master_broker_status() -> Dict[str, Any]:
    return master_broker_status()


@app.post("/request-activation", response_model=MessageResponse)
def request_activation(request: UserIdRequest) -> Dict[str, str]:
    return {"message": f"Activation request received for {request.user_id}"}


@app.post("/admin/activate", response_model=AdminActivateResponse)
def admin_activate(request: UserIdRequest) -> Dict[str, Any]:
    return request_mpin_otp(request.user_id)


@app.post("/verify-otp", response_model=MessageResponse)
def verify_otp(request: VerifyOtpRequest) -> Dict[str, str]:
    return {"message": "OTP endpoint reserved for activation workflows"}






