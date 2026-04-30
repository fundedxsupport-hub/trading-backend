from fastapi import FastAPI

from app.models import (
    AdminActivateResponse,
    BrokerConnectRequest,
    BrokerConnection,
    BrokerConnectionResponse,
    CloseTradeRequest,
    CloseTradeResponse,
    InitUserResponse,
    MessageResponse,
    TradeRequest,
    TradeResponse,
    UserAccount,
    UserIdRequest,
    VerifyOtpRequest,
)
from app.services.broker_service import connect_broker, disconnect_broker, get_broker_status
from app.services.trade_service import close_trade as close_trade_service
from app.services.trade_service import open_trade
from app.services.user_service import (
    activate_with_otp,
    create_activation_otp,
    create_user,
    get_user_or_404,
    mark_challenge_passed,
)

app = FastAPI(
    title="Funded Trading Backend",
    version="1.0.0",
    description="In-memory FastAPI backend for funded trading account flows.",
)


@app.get("/")
def home() -> dict[str, str]:
    return {"message": "Trading Backend Running"}


@app.post("/init-user", response_model=InitUserResponse)
def init_user() -> UserAccount:
    return create_user()


@app.get("/account/{user_id}", response_model=UserAccount)
def get_account(user_id: str) -> UserAccount:
    return get_user_or_404(user_id)


@app.post("/trade", response_model=TradeResponse)
def trade(request: TradeRequest) -> dict[str, object]:
    return open_trade(request)


@app.post("/close-trade", response_model=CloseTradeResponse)
def close_trade(request: CloseTradeRequest) -> dict[str, object]:
    return close_trade_service(request)


@app.post("/request-activation", response_model=MessageResponse)
def request_activation(request: UserIdRequest) -> dict[str, str]:
    return mark_challenge_passed(request.user_id)


@app.post("/admin/activate", response_model=AdminActivateResponse)
def admin_activate(request: UserIdRequest) -> dict[str, object]:
    return create_activation_otp(request.user_id)


@app.post("/verify-otp", response_model=MessageResponse)
def verify_otp(request: VerifyOtpRequest) -> dict[str, str]:
    return activate_with_otp(request.user_id, request.otp)


@app.post("/broker/connect", response_model=BrokerConnectionResponse)
def broker_connect(request: BrokerConnectRequest) -> dict[str, object]:
    return connect_broker(request)


@app.get("/broker/status/{user_id}", response_model=BrokerConnection)
def broker_status(user_id: str) -> BrokerConnection:
    return get_broker_status(user_id)


@app.post("/broker/disconnect", response_model=MessageResponse)
def broker_disconnect(request: UserIdRequest) -> dict[str, str]:
    return disconnect_broker(request.user_id)
