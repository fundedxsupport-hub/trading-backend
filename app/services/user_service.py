from typing import Any, Dict

from app.models import RegisterRequest
from app.services.account_service import create_user as create_account_user
from app.services.account_service import get_user_or_404, request_mpin_otp


def create_user(payload: RegisterRequest) -> Dict[str, str]:
    return create_account_user(payload)


def mark_challenge_passed(user_id: str) -> Dict[str, str]:
    user = get_user_or_404(user_id)
    return {"message": f"Challenge passed for {user['user_id']}. Activation requested."}


def create_activation_otp(user_id: str) -> Dict[str, Any]:
    return request_mpin_otp(user_id)


def activate_with_otp(user_id: str, otp: str) -> Dict[str, str]:
    get_user_or_404(user_id)
    return {"message": "Activation OTP verification is handled by the MPIN/activation workflow"}
