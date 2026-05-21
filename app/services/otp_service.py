from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import HTTPException, status

OTP_TTL_SECONDS = 300


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def attach_otp(user: Dict[str, Any]) -> str:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Legacy in-memory OTP flow is disabled. Use /mpin/request-otp.",
    )


def verify_user_otp(user: Dict[str, Any], otp: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Legacy in-memory OTP flow is disabled. Use /mpin/change or activation endpoints.",
    )
