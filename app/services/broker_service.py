from fastapi import HTTPException, status

from app.database import broker_connections
from app.models import BrokerConnectRequest
from app.services.account_service import get_user_or_404
from app.utils import clean_dict, now_utc


def connect_broker(request: BrokerConnectRequest) -> dict:
    user = get_user_or_404(request.user_id)
    connection = {
        "user_id": user["user_id"],
        "broker_name": request.broker_name,
        "account_id": request.account_id,
        "base_url": request.base_url,
        "access_token": request.access_token,
        "api_key": request.api_key,
        "connected": True,
        "updated_at": now_utc(),
    }
    broker_connections.update_one({"user_id": user["user_id"]}, {"$set": connection}, upsert=True)
    public_connection = {k: v for k, v in connection.items() if k not in {"access_token", "api_key", "updated_at"}}
    return {**public_connection, "message": "Broker connected"}


def get_broker_status(user_id: str) -> dict:
    user = get_user_or_404(user_id)
    connection = broker_connections.find_one({"user_id": user["user_id"]})
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Broker connection not found")
    public_connection = clean_dict(connection)
    public_connection.pop("access_token", None)
    public_connection.pop("api_key", None)
    public_connection.pop("updated_at", None)
    return public_connection


def disconnect_broker(user_id: str) -> dict[str, str]:
    user = get_user_or_404(user_id)
    broker_connections.update_one({"user_id": user["user_id"]}, {"$set": {"connected": False, "updated_at": now_utc()}})
    return {"message": "Broker disconnected"}
