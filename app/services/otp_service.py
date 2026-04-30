import random
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status

from app.models import AccountStatus, AccountType, UserAccount

OTP_TTL_SECONDS = 300


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def generate_otp() -> str:
    return f"{random.randint(0, 999999):06d}"


def attach_otp(user: UserAccount) -> str:
    otp = generate_otp()
    user.otp = otp
    user.otp_expires_at = now_utc() + timedelta(seconds=OTP_TTL_SECONDS)
    return otp


def verify_user_otp(user: UserAccount, otp: str) -> None:
    if not user.otp or not user.otp_expires_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No active OTP found")

    if now_utc() > user.otp_expires_at:
        user.otp = None
        user.otp_expires_at = None
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP expired")

    if user.otp != otp:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wrong OTP")

    # One-time use: clear OTP immediately after success.
    user.otp = None
    user.otp_expires_at = None
    user.account_type = AccountType.funded
    user.status = AccountStatus.active
    user.trading_enabled = True
