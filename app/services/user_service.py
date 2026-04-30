import uuid

from fastapi import HTTPException, status

from app.models import AccountStatus, UserAccount
from app.services.otp_service import OTP_TTL_SECONDS, attach_otp, verify_user_otp
from app.storage import users


def create_user() -> UserAccount:
    user_id = str(uuid.uuid4())
    user = UserAccount(user_id=user_id)
    users[user_id] = user
    return user


def get_user_or_404(user_id: str) -> UserAccount:
    user = users.get(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def mark_challenge_passed(user_id: str) -> dict[str, str]:
    user = get_user_or_404(user_id)
    user.status = AccountStatus.passed
    user.trading_enabled = False
    return {"message": "Challenge passed. Activation requested."}


def create_activation_otp(user_id: str) -> dict[str, object]:
    user = get_user_or_404(user_id)

    if user.status != AccountStatus.passed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must pass challenge before activation",
        )

    otp = attach_otp(user)
    return {
        "user_id": user.user_id,
        "otp": otp,
        "expires_in_seconds": OTP_TTL_SECONDS,
        "message": "Activation OTP generated",
    }


def activate_with_otp(user_id: str, otp: str) -> dict[str, str]:
    user = get_user_or_404(user_id)
    verify_user_otp(user, otp)
    return {"message": "Funded account activated"}
