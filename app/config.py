import os
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_name: str = "FundedX Trading API"
    environment: str = "development"
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db_name: str = "fundedx_trading"
    cors_origins: str = "*"
    otp_ttl_seconds: int = 300
    upstox_account_id: str = ""
    upstox_base_url: str = "https://api.upstox.com/v2"
    upstox_access_token: str = ""
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_product: str = "I"
    upstox_validity: str = "DAY"
    demo_broker_mode: bool = True
    admin_api_key: str = ""
    default_challenge_capital: float = 100000.0
    default_max_loss_limit: float = 10000.0
    default_max_drawdown_limit: float = 10000.0
    default_daily_loss_limit: float = 5000.0
    default_max_position_size: int = 0
    upstox_instrument_map: str = "{}"
    upstox_underlying_map: str = "{}"

    @property
    def parsed_cors_origins(self) -> List[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def parsed_instrument_map(self) -> Dict[str, str]:
        try:
            data = json.loads(self.upstox_instrument_map or "{}")
        except json.JSONDecodeError:
            return {}
        return {str(key).upper(): str(value) for key, value in data.items()} if isinstance(data, dict) else {}

    @property
    def parsed_underlying_map(self) -> Dict[str, str]:
        default_map = {
            "NIFTY": "NSE_INDEX|Nifty 50",
            "NIFTY50": "NSE_INDEX|Nifty 50",
            "BANKNIFTY": "NSE_INDEX|Nifty Bank",
            "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
            "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
            "MIDCAP": "NSE_INDEX|NIFTY MID SELECT",
            "SENSEX": "BSE_INDEX|SENSEX",
            "BANKEX": "BSE_INDEX|BANKEX",
        }
        try:
            data = json.loads(self.upstox_underlying_map or "{}")
        except json.JSONDecodeError:
            return default_map
        if isinstance(data, dict):
            default_map.update({str(key).upper(): str(value) for key, value in data.items()})
        return default_map

@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "FundedX Trading API"),
        environment=os.getenv("ENVIRONMENT", "development"),
        mongo_url=os.getenv("MONGO_URL", "mongodb://localhost:27017"),
        mongo_db_name=os.getenv("MONGO_DB_NAME", "fundedx_trading"),
        cors_origins=os.getenv("CORS_ORIGINS", "*"),
        otp_ttl_seconds=int(os.getenv("OTP_TTL_SECONDS", "300")),
        upstox_account_id=os.getenv("UPSTOX_ACCOUNT_ID", ""),
        upstox_base_url=os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com/v2"),
        upstox_access_token=os.getenv("UPSTOX_ACCESS_TOKEN", ""),
        upstox_api_key=os.getenv("UPSTOX_API_KEY", ""),
        upstox_api_secret=os.getenv("UPSTOX_API_SECRET", ""),
        upstox_product=os.getenv("UPSTOX_PRODUCT", "I"),
        upstox_validity=os.getenv("UPSTOX_VALIDITY", "DAY"),
        demo_broker_mode=os.getenv("DEMO_BROKER_MODE", "true").lower() == "true",
        admin_api_key=os.getenv("ADMIN_API_KEY", ""),
        default_challenge_capital=float(os.getenv("DEFAULT_CHALLENGE_CAPITAL", "100000")),
        default_max_loss_limit=float(os.getenv("DEFAULT_MAX_LOSS_LIMIT", "10000")),
        default_max_drawdown_limit=float(os.getenv("DEFAULT_MAX_DRAWDOWN_LIMIT", "10000")),
        default_daily_loss_limit=float(os.getenv("DEFAULT_DAILY_LOSS_LIMIT", "5000")),
        default_max_position_size=int(os.getenv("DEFAULT_MAX_POSITION_SIZE", "0")),
        upstox_instrument_map=os.getenv("UPSTOX_INSTRUMENT_MAP", "{}"),
        upstox_underlying_map=os.getenv("UPSTOX_UNDERLYING_MAP", "{}"),
    )

