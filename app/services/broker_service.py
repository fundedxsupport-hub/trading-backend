from typing import Any, Dict

from fastapi import HTTPException, status

from app.models import BrokerConnectRequest
from app.services.account_service import master_broker_status


def connect_broker(request: BrokerConnectRequest) -> Dict[str, Any]:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Manual broker connection is disabled. Configure the master Upstox account with environment variables.",
    )


def get_broker_status(user_id: str = "") -> Dict[str, Any]:
    return master_broker_status()


def disconnect_broker(user_id: str) -> Dict[str, str]:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Manual broker disconnect is disabled for the master account flow.",
    )
