from fastapi import HTTPException, status

from app.models import BrokerConnectRequest, BrokerConnection
from app.services.user_service import get_user_or_404
from app.storage import broker_connections


def connect_broker(request: BrokerConnectRequest) -> dict[str, object]:
    get_user_or_404(request.user_id)

    connection = BrokerConnection(
        user_id=request.user_id,
        broker_name=request.broker_name,
        account_id=request.account_id,
        base_url=request.base_url,
        connected=True,
    )
    broker_connections[request.user_id] = connection

    # API key/access token are intentionally not returned in the response.
    # Replace in-memory storage with encrypted database storage before production.
    return {
        **connection.dict(),
        "message": "Real trading account connected",
    }


def get_broker_status(user_id: str) -> BrokerConnection:
    get_user_or_404(user_id)
    connection = broker_connections.get(user_id)
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Broker account not connected",
        )
    return connection


def disconnect_broker(user_id: str) -> dict[str, str]:
    get_user_or_404(user_id)
    broker_connections.pop(user_id, None)
    return {"message": "Real trading account disconnected"}
