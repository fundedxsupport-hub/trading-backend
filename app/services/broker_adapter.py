import os
import uuid
from typing import Optional

from app.models import TradeRequest


class BrokerAdapter:
    """Placeholder for real broker order placement."""

    def __init__(self) -> None:
        self.base_url = os.getenv("BROKER_BASE_URL", "")
        self.api_key = os.getenv("BROKER_API_KEY", "")
        self.access_token = os.getenv("BROKER_ACCESS_TOKEN", "")

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.access_token)

    def place_order(self, request: TradeRequest) -> Optional[str]:
        """Place a real broker order later.

        Fill BROKER_BASE_URL, BROKER_API_KEY, and BROKER_ACCESS_TOKEN when ready.
        For now, broker execution returns a demo order id.
        """
        if not request.execute_on_broker:
            return None

        if not self.is_configured:
            return f"DEMO-{uuid.uuid4()}"

        # TODO: Replace with real broker API call.
        return f"PENDING-BROKER-{uuid.uuid4()}"


broker_adapter = BrokerAdapter()
