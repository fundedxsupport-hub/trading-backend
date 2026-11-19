import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import HTTPException, status

from app.config import get_settings
from app.database import otps
from app.utils import now_utc

logger = logging.getLogger(__name__)
OTP_TTL_SECONDS = 300
SEND_COOLDOWN_SECONDS = 60


def _as_utc(value: datetime) -> datetime:
    """Normalize MongoDB datetimes (returned naive by default) to aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_mobile(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) != 10:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid 10-digit Indian mobile number",
        )
    return digits


def _provider_request(path: str) -> Dict[str, Any]:
    api_key = get_settings().twofactor_api_key.strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="2Factor OTP is not configured on the backend",
        )
    url = f"https://2factor.in/API/V1/{quote(api_key, safe='')}/{path}"
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=12) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as error:
        logger.warning("2Factor returned HTTP status %s", error.code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="2Factor OTP provider returned an HTTP error",
        )
    except (TimeoutError, URLError, OSError):
        logger.exception("2Factor OTP provider request failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the 2Factor OTP provider",
        )
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="2Factor returned an invalid response",
        )
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="2Factor returned an invalid response",
        )
    if str(data.get("Status", "")).lower() != "success":
        provider_detail = str(data.get("Details", ""))
        logger.warning("2Factor OTP operation rejected: %s", provider_detail[:160])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="2Factor rejected the OTP request. Check the API key, SMS credits, and account setup.",
        )
    return data


def send_phone_otp(mobile: str) -> Dict[str, str]:
    mobile = _normalize_mobile(mobile)
    now = now_utc()
    cooldown = otps.find_one(
        {"user_id": mobile, "purpose": "registration_phone_cooldown"}
    )
    if (
        cooldown
        and cooldown.get("created_at")
        and now - _as_utc(cooldown["created_at"])
        < timedelta(seconds=SEND_COOLDOWN_SECONDS)
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Wait one minute before requesting another OTP",
        )

    response = _provider_request(f"SMS/%2B91{mobile}/AUTOGEN")
    challenge_id = str(response.get("Details", "")).strip()
    if not challenge_id:
        logger.error("2Factor success response did not contain a session ID")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="2Factor did not return an OTP session ID",
        )

    now = now_utc()
    otps.update_one(
        {"user_id": challenge_id, "purpose": "registration_phone"},
        {
            "$set": {
                "mobile": mobile,
                "challenge_id": challenge_id,
                "purpose": "registration_phone",
                "created_at": now,
                "expires_at": now + timedelta(seconds=OTP_TTL_SECONDS),
            }
        },
        upsert=True,
    )
    otps.update_one(
        {"user_id": mobile, "purpose": "registration_phone_cooldown"},
        {"$set": {"created_at": now}},
        upsert=True,
    )
    return {"challenge_id": challenge_id, "message": "OTP sent"}


def verify_phone_otp(mobile: str, challenge_id: str, code: str) -> Dict[str, str]:
    mobile = _normalize_mobile(mobile)
    challenge_id = challenge_id.strip()
    challenge = otps.find_one(
        {"user_id": challenge_id, "purpose": "registration_phone"}
    )
    now = now_utc()
    if (
        not challenge
        or challenge.get("mobile") != mobile
        or not challenge.get("expires_at")
        or _as_utc(challenge["expires_at"]) <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP session is invalid or expired. Request a new code.",
        )

    response = _provider_request(
        f"SMS/VERIFY/{quote(challenge_id, safe='')}/{quote(code.strip(), safe='')}"
    )
    provider_detail = str(response.get("Details", "")).lower()
    if "match" not in provider_detail and "verified" not in provider_detail:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OTP",
        )

    verification_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(verification_token.encode("utf-8")).hexdigest()
    otps.insert_one(
        {
            "user_id": token_hash,
            "purpose": "registration_phone_receipt",
            "mobile": mobile,
            "created_at": now,
            "expires_at": now + timedelta(seconds=OTP_TTL_SECONDS),
        }
    )
    otps.delete_one(
        {"user_id": challenge_id, "purpose": "registration_phone"}
    )
    return {"verification_token": verification_token, "message": "OTP verified"}


def consume_phone_verification(token: str, mobile: str) -> bool:
    if not token:
        return False
    normalized_mobile = _normalize_mobile(mobile)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    receipt = otps.find_one(
        {
            "user_id": token_hash,
            "purpose": "registration_phone_receipt",
            "mobile": normalized_mobile,
            "expires_at": {"$gt": now_utc()},
        }
    )
    if not receipt:
        return False
    return otps.delete_one(
        {"_id": receipt["_id"], "purpose": "registration_phone_receipt"}
    ).deleted_count == 1
