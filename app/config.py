import os
from dataclasses import dataclass
from functools import lru_cache

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
    upstox_base_url: str = "https://api.upstox.com/v2"
    upstox_access_token: str = ""
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_product: str = "I"
    upstox_validity: str = "DAY"
    demo_broker_mode: bool = True
    admin_api_key: str = ""

    @property
    def parsed_cors_origins(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings(
        environment=os.getenv("ENVIRONMENT", "development"),
        mongo_url=os.getenv("MONGO_URL", "mongodb://localhost:27017"),
        mongo_db_name=os.getenv("MONGO_DB_NAME", "fundedx_trading"),
        cors_origins=os.getenv("CORS_ORIGINS", "*"),
        otp_ttl_seconds=int(os.getenv("OTP_TTL_SECONDS", "300")),
        upstox_base_url=os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com/v2"),
        upstox_access_token=os.getenv("UPSTOX_ACCESS_TOKEN", ""),
        upstox_api_key=os.getenv("UPSTOX_API_KEY", ""),
        upstox_api_secret=os.getenv("UPSTOX_API_SECRET", ""),
        upstox_product=os.getenv("UPSTOX_PRODUCT", "I"),
        upstox_validity=os.getenv("UPSTOX_VALIDITY", "DAY"),
        demo_broker_mode=os.getenv("DEMO_BROKER_MODE", "true").lower() == "true",
        admin_api_key=os.getenv("ADMIN_API_KEY", ""),
    )
