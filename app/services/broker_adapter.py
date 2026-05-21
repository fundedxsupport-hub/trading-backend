import uuid
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import get_settings
from app.models import TradeExecutionStatus, TradeRequest


class BrokerAdapter:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.upstox_access_token)

    def estimate_margin(self, request: TradeRequest) -> float:
        order_type = (request.order_type or "MARKET").upper()
        fallback_price = request.limit_price if order_type == "LIMIT" and request.limit_price else (
            request.entry_price or request.amount
        )
        fallback_price = float(fallback_price or 0)
        buy_margin = round(fallback_price * float(request.quantity), 2)
        fallback_margin = buy_margin if request.side.value == "BUY" else round(max(buy_margin * 5, buy_margin), 2)

        if request.account_type.value != "real" or self.settings.demo_broker_mode or not self.is_configured:
            return fallback_margin

        url = f"{self.settings.upstox_base_url.rstrip('/')}/charges/margin"
        payload = {
            "instruments": [
                {
                    "instrument_token": request.instrument_token or request.symbol,
                    "quantity": int(request.quantity),
                    "transaction_type": request.side.value,
                    "product": request.product or self.settings.upstox_product,
                    "order_type": order_type,
                    "price": fallback_price if order_type == "LIMIT" else 0,
                }
            ]
        }
        headers = {
            "Authorization": f"Bearer {self.settings.upstox_access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            request_obj = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urlopen(request_obj, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            items = data.get("data") or []
            if isinstance(items, list) and items:
                item = items[0] or {}
                for key in ("required_margin", "total", "span_margin", "margin"):
                    value = item.get(key)
                    if value is not None:
                        parsed = float(value)
                        if parsed > 0:
                            return round(parsed, 2)
            return fallback_margin
        except Exception:
            return fallback_margin

    def place_order(self, request: TradeRequest) -> dict[str, Any]:
        if not request.execute_on_broker or request.account_type.value != "real":
            return {"status": TradeExecutionStatus.pending, "broker_order_id": None, "message": "Broker execution not requested"}

        if self.settings.demo_broker_mode or not self.is_configured:
            return {
                "status": TradeExecutionStatus.demo,
                "broker_order_id": f"DEMO-{uuid.uuid4()}",
                "message": "Demo broker order generated. Set DEMO_BROKER_MODE=false and UPSTOX_ACCESS_TOKEN for live execution.",
            }

        url = f"{self.settings.upstox_base_url.rstrip('/')}/order/place"
        order_type = (request.order_type or "MARKET").upper()
        payload = {
            "quantity": request.quantity,
            "product": request.product or self.settings.upstox_product,
            "validity": self.settings.upstox_validity,
            "price": request.limit_price if order_type == "LIMIT" and request.limit_price else 0,
            "tag": request.user_id,
            "instrument_token": request.instrument_token or request.symbol,
            "order_type": order_type,
            "transaction_type": request.side.value,
            "disclosed_quantity": 0,
            "trigger_price": request.trigger_price or 0,
            "is_amo": False,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.upstox_access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        try:
            request_obj = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urlopen(request_obj, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            order_id = data.get("data", {}).get("order_id") or data.get("order_id")
            return {"status": TradeExecutionStatus.success, "broker_order_id": order_id, "message": "Trade executed in Upstox"}
        except HTTPError as exc:
            return {"status": TradeExecutionStatus.failed, "broker_order_id": None, "message": exc.read().decode("utf-8")}
        except (URLError, TimeoutError) as exc:
            return {"status": TradeExecutionStatus.failed, "broker_order_id": None, "message": str(exc)}

    def close_order(self, trade: dict[str, Any]) -> dict[str, Any]:
        if trade.get("account_type") != "real":
            return {
                "status": TradeExecutionStatus.success,
                "broker_order_id": None,
                "message": "Virtual trade closed without broker execution",
            }

        if self.settings.demo_broker_mode or not self.is_configured:
            return {
                "status": TradeExecutionStatus.demo,
                "broker_order_id": f"DEMO-CLOSE-{uuid.uuid4()}",
                "message": "Demo close order generated",
            }

        close_side = "SELL" if str(trade.get("side", "BUY")).upper() == "BUY" else "BUY"
        url = f"{self.settings.upstox_base_url.rstrip('/')}/order/place"
        payload = {
            "quantity": int(trade.get("quantity") or 0),
            "product": trade.get("product") or self.settings.upstox_product,
            "validity": self.settings.upstox_validity,
            "price": 0,
            "tag": trade.get("user_id"),
            "instrument_token": trade.get("instrument_token") or trade.get("symbol"),
            "order_type": "MARKET",
            "transaction_type": close_side,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.upstox_access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            request_obj = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urlopen(request_obj, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            order_id = data.get("data", {}).get("order_id") or data.get("order_id")
            return {"status": TradeExecutionStatus.success, "broker_order_id": order_id, "message": "Trade closed in Upstox"}
        except HTTPError as exc:
            return {"status": TradeExecutionStatus.failed, "broker_order_id": None, "message": exc.read().decode("utf-8")}
        except (URLError, TimeoutError) as exc:
            return {"status": TradeExecutionStatus.failed, "broker_order_id": None, "message": str(exc)}


broker_adapter = BrokerAdapter()
