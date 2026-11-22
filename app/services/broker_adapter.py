import logging
import uuid
from typing import Any, Dict

import requests

from app.config import get_settings
from app.models import AccountType, TradeExecutionStatus, TradeRequest

logger = logging.getLogger(__name__)


class BrokerAdapter:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.upstox_access_token)

    @property
    def is_live(self) -> bool:
        return self.is_configured and not self.settings.demo_broker_mode

    def status(self) -> Dict[str, Any]:
        return {
            "broker_name": "upstox",
            "connected": self.is_configured,
            "demo_mode": self.settings.demo_broker_mode,
            "base_url": self.settings.upstox_base_url,
            "account_id": self.settings.upstox_account_id or None,
            "has_access_token": self.is_configured,
        }

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.upstox_access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _entry_price(self, request: TradeRequest) -> float:
        order_type = (request.order_type or "MARKET").upper()
        if order_type == "LIMIT" and request.limit_price:
            return float(request.limit_price)
        if request.entry_price:
            return float(request.entry_price)
        return float(request.amount)

    def estimate_margin(self, request: TradeRequest) -> float:
        order_type = (request.order_type or "MARKET").upper()
        fallback_price = self._entry_price(request)
        buy_margin = round(fallback_price * float(request.quantity), 2)
        fallback_margin = buy_margin if request.side.value == "BUY" else round(max(buy_margin * 5, buy_margin), 2)

        # Challenge accounts use virtual funds and must not make broker calls.
        if request.account_type == AccountType.challenge:
            return fallback_margin

        if not self.is_live:
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
        try:
            response = requests.post(url, json=payload, headers=self._headers(), timeout=20)
            response.raise_for_status()
            data = response.json()
            items = data.get("data") or []
            if isinstance(items, list) and items:
                item = items[0] or {}
                for key in ("required_margin", "total", "span_margin", "margin"):
                    value = item.get(key)
                    if value is not None:
                        parsed = float(value)
                        if parsed > 0:
                            return round(parsed, 2)
        except requests.RequestException:
            logger.exception("Upstox margin estimate failed; using fallback margin")
        except (TypeError, ValueError):
            logger.exception("Upstox margin response could not be parsed; using fallback margin")
        return fallback_margin

    def place_order(self, request: TradeRequest) -> Dict[str, Any]:
        if request.account_type == AccountType.challenge:
            return {
                "status": TradeExecutionStatus.demo,
                "broker_order_id": None,
                "message": "Challenge trades are virtual and are not sent to Upstox.",
            }
        if not self.is_live:
            return {
                "status": TradeExecutionStatus.demo,
                "broker_order_id": f"DEMO-{uuid.uuid4()}",
                "message": "Demo master Upstox order generated. Set DEMO_BROKER_MODE=false and UPSTOX_ACCESS_TOKEN for live execution.",
            }

        url = f"{self.settings.upstox_base_url.rstrip('/')}/order/place"
        order_type = (request.order_type or "MARKET").upper()
        payload = {
            "quantity": int(request.quantity),
            "product": request.product or self.settings.upstox_product,
            "validity": self.settings.upstox_validity,
            "price": request.limit_price if order_type == "LIMIT" and request.limit_price else 0,
            "tag": request.broker_tag or request.user_id,
            "instrument_token": request.instrument_token or request.symbol,
            "order_type": order_type,
            "transaction_type": request.side.value,
            "disclosed_quantity": 0,
            "trigger_price": request.trigger_price or 0,
            "is_amo": False,
        }
        try:
            response = requests.post(url, json=payload, headers=self._headers(), timeout=20)
            response.raise_for_status()
            data = response.json()
            order_id = data.get("data", {}).get("order_id") or data.get("order_id")
            return {"status": TradeExecutionStatus.success, "broker_order_id": order_id, "message": "Trade executed in master Upstox account"}
        except requests.HTTPError as exc:
            detail = exc.response.text if exc.response is not None else str(exc)
            logger.exception("Upstox order rejected")
            return {"status": TradeExecutionStatus.failed, "broker_order_id": None, "message": detail}
        except (requests.RequestException, ValueError) as exc:
            # A timeout can occur after Upstox accepted the order. Treat that
            # outcome as unknown and reconcile by the unique tag; never retry
            # blindly or release the reserved exposure.
            logger.exception("Upstox order request outcome is uncertain")
            recovered = self.get_order_history_by_tag(request.broker_tag or "")
            if recovered and recovered.get("order_id"):
                return {
                    "status": TradeExecutionStatus.pending,
                    "broker_order_id": str(recovered["order_id"]),
                    "message": "Order found by its unique tag after an uncertain response.",
                }
            return {"status": TradeExecutionStatus.pending, "broker_order_id": None, "message": "Order outcome is uncertain; backend will reconcile it by tag."}

    def get_order_history_by_tag(self, tag: str) -> Dict[str, Any] | None:
        if not self.is_live or not tag:
            return None
        url = f"{self.settings.upstox_base_url.rstrip('/')}/order/history"
        try:
            response = requests.get(url, params={"tag": tag}, headers=self._headers(), timeout=10)
            response.raise_for_status()
            payload = response.json()
            items = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(items, list) and items:
                return items[-1] if isinstance(items[-1], dict) else None
            return None
        except (requests.RequestException, ValueError):
            logger.exception("Upstox order history lookup by tag failed")
            return None

    def get_order_details(self, order_id: str) -> Dict[str, Any] | None:
        if not self.is_live or not order_id:
            return None
        url = f"{self.settings.upstox_base_url.rstrip('/')}/order/details"
        try:
            response = requests.get(url, params={"order_id": order_id}, headers=self._headers(), timeout=10)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            return data if isinstance(data, dict) else None
        except (requests.RequestException, ValueError):
            logger.exception("Upstox order status lookup failed")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if not self.is_live or not order_id:
            return False
        url = f"{self.settings.upstox_base_url.rstrip('/')}/order/cancel"
        try:
            response = requests.delete(url, params={"order_id": order_id}, headers=self._headers(), timeout=10)
            response.raise_for_status()
            return True
        except requests.RequestException:
            logger.exception("Upstox order cancellation failed")
            return False

    def close_order(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        if str(trade.get("account_type", "")) == AccountType.challenge.value:
            return {
                "status": TradeExecutionStatus.demo,
                "broker_order_id": None,
                "message": "Challenge trades are virtual and are not sent to Upstox.",
            }
        if trade.get("broker_live") is not True:
            return {
                "status": TradeExecutionStatus.demo,
                "broker_order_id": None,
                "message": "This app trade has no live Upstox order to close.",
            }
        if not self.is_live:
            return {
                "status": TradeExecutionStatus.demo,
                "broker_order_id": f"DEMO-CLOSE-{uuid.uuid4()}",
                "message": "Demo close order generated for master Upstox account",
            }

        close_side = "SELL" if str(trade.get("side", "BUY")).upper() == "BUY" else "BUY"
        url = f"{self.settings.upstox_base_url.rstrip('/')}/order/place"
        payload = {
            "quantity": int(trade.get("close_quantity") or trade.get("filled_quantity") or trade.get("quantity") or 0),
            "product": trade.get("product") or self.settings.upstox_product,
            "validity": self.settings.upstox_validity,
            "price": 0,
            "tag": trade.get("close_broker_tag") or trade.get("broker_tag") or trade.get("user_id"),
            "instrument_token": trade.get("instrument_token") or trade.get("symbol"),
            "order_type": "MARKET",
            "transaction_type": close_side,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        try:
            response = requests.post(url, json=payload, headers=self._headers(), timeout=20)
            response.raise_for_status()
            data = response.json()
            order_id = data.get("data", {}).get("order_id") or data.get("order_id")
            return {"status": TradeExecutionStatus.success, "broker_order_id": order_id, "message": "Trade closed in master Upstox account"}
        except requests.HTTPError as exc:
            detail = exc.response.text if exc.response is not None else str(exc)
            logger.exception("Upstox close order rejected")
            return {"status": TradeExecutionStatus.failed, "broker_order_id": None, "message": detail}
        except (requests.RequestException, ValueError) as exc:
            logger.exception("Upstox close order response is uncertain")
            recovered = self.get_order_history_by_tag(str(trade.get("close_broker_tag") or ""))
            if recovered and recovered.get("order_id"):
                return {
                    "status": TradeExecutionStatus.pending,
                    "broker_order_id": str(recovered["order_id"]),
                    "message": "Close order found by its unique tag after an uncertain response.",
                }
            return {"status": TradeExecutionStatus.pending, "broker_order_id": None, "message": "Close order outcome is uncertain; backend will reconcile it by tag."}


broker_adapter = BrokerAdapter()
