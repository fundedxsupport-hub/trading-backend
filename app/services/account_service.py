from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.database import master_broker, otps, referrals, risk_profiles, support_tickets, trades, users, wallet_logs
from app.models import (
    AccountType,
    AdminStatsResponse,
    ChangeMpinRequest,
    MpinLoginRequest,
    PlanRequest,
    RegisterRequest,
    SupportCreateRequest,
    SupportReplyRequest,
)
from app.utils import clean_dict, hash_secret, new_uuid, normalize_email, now_utc, public_id, random_code, verify_secret

settings = get_settings()


def _unique_value(collection, field: str, prefix: str, digits: int = 6) -> str:
    while True:
        value = public_id(prefix, digits)
        if collection.count_documents({field: value}, limit=1) == 0:
            return value


def serialize_user(user: Dict[str, Any]) -> Dict[str, Any]:
    data = clean_dict(user)
    challenge_profit = float(data.get("challenge_profit", 0))
    challenge_loss = float(data.get("challenge_loss", 0))
    challenge_capital = float(data.get("challenge_virtual_capital", 0))
    challenge_margin_used = float(data.get("challenge_margin_used", 0))
    real_profit = float(data.get("real_profit", 0))
    real_loss = float(data.get("real_loss", 0))
    real_capital = float(data.get("real_capital", 0))
    real_margin_used = float(data.get("real_margin_used", 0))
    data["challenge_balance"] = challenge_capital + challenge_profit - challenge_loss
    data["real_balance"] = real_capital + real_profit - real_loss
    data["challenge_margin_used"] = challenge_margin_used
    data["real_margin_used"] = real_margin_used
    data["challenge_available_balance"] = max(0.0, data["challenge_balance"] - challenge_margin_used)
    data["real_available_balance"] = max(0.0, data["real_balance"] - real_margin_used)
    data["max_loss_limit"] = float(data.get("max_loss_limit", 0))
    data["max_drawdown_limit"] = float(data.get("max_drawdown_limit", 0))
    data["daily_loss_limit"] = float(data.get("daily_loss_limit", 0))
    data["max_position_size"] = int(data.get("max_position_size", 0) or 0)
    return data


def get_user_or_404(user_id: str) -> Dict[str, Any]:
    user = users.find_one({"user_id": user_id})
    if not user:
        user = users.find_one({"client_id": user_id})
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def list_users() -> List[Dict[str, Any]]:
    return [serialize_user(user) for user in users.find({}, {"mpin_hash": 0, "mpin_salt": 0}).sort("created_at", -1)]


def create_user(payload: RegisterRequest) -> Dict[str, Any]:
    email = normalize_email(payload.email)
    if users.count_documents({"email": email}, limit=1):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")
    if users.count_documents({"mobile": payload.mobile}, limit=1):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mobile already registered")

    user_id = new_uuid()
    requested_client_id = (payload.client_id or "").strip().upper()
    if requested_client_id and users.count_documents({"client_id": requested_client_id}, limit=1):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Client ID already registered")

    client_id = requested_client_id or _unique_value(users, "client_id", "FXI", 6)
    referral_code = _unique_value(users, "referral_code", "REF", 6)
    created_at = now_utc()
    mpin_hash = mpin_salt = None
    if payload.mpin:
        mpin_hash, mpin_salt = hash_secret(payload.mpin)
    access_token = new_uuid()

    user = {
        "user_id": user_id,
        "client_id": client_id,
        "name": payload.name.strip(),
        "email": email,
        "mobile": payload.mobile,
        "external_uid": (payload.external_uid or "").strip() or None,
        "referral_code": referral_code,
        "referred_by": (payload.referral_code or "").strip().upper() or None,
        "active_plan_amount": settings.default_challenge_capital,
        "account_type": AccountType.challenge.value,
        "challenge_status": "running",
        "real_status": "running",
        "challenge_virtual_capital": settings.default_challenge_capital,
        "challenge_profit": 0.0,
        "challenge_loss": 0.0,
        "challenge_margin_used": 0.0,
        "real_capital": 0.0,
        "real_profit": 0.0,
        "real_loss": 0.0,
        "real_margin_used": 0.0,
        "max_loss_limit": settings.default_max_loss_limit,
        "max_drawdown_limit": settings.default_max_drawdown_limit,
        "daily_loss_limit": settings.default_daily_loss_limit,
        "max_position_size": settings.default_max_position_size,
        "trading_enabled": True,
        "access_token": access_token,
        "mpin_hash": mpin_hash,
        "mpin_salt": mpin_salt,
        "created_at": created_at,
        "updated_at": created_at,
    }

    try:
        users.insert_one(user)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email, mobile, or client ID already exists")

    if user["referred_by"]:
        referrer = users.find_one({"referral_code": user["referred_by"]})
        referrals.insert_one(
            {
                "referrer_user_id": referrer["user_id"] if referrer else "",
                "referrer_name": referrer["name"] if referrer else "",
                "referral_code": user["referred_by"],
                "referred_user_id": user_id,
                "referred_name": user["name"],
                "registration_status": "Registered",
                "payment_status": "Not Paid",
                "created_at": created_at,
            }
        )

    return {
        "message": "Registration successful",
        "user_id": user_id,
        "client_id": client_id,
        "referral_code": referral_code,
        "access_token": access_token,
        "account_type": AccountType.challenge,
        "challenge_capital": settings.default_challenge_capital,
    }


def sync_user(payload: RegisterRequest) -> Dict[str, Any]:
    email = normalize_email(payload.email)
    requested_client_id = (payload.client_id or "").strip().upper()
    mobile = payload.mobile.strip()
    query = {
        "$or": [
            {"email": email},
            {"mobile": mobile},
        ]
    }
    if requested_client_id:
        query["$or"].append({"client_id": requested_client_id})
    if payload.external_uid:
        query["$or"].append({"external_uid": payload.external_uid.strip()})

    existing = users.find_one(query)
    if existing:
        access_token = existing.get("access_token") or new_uuid()
        updates = {
            "name": payload.name.strip(),
            "email": email,
            "mobile": mobile,
            "access_token": access_token,
            "account_type": existing.get("account_type") or AccountType.challenge.value,
            "updated_at": now_utc(),
        }
        if requested_client_id:
            updates["client_id"] = requested_client_id
        if payload.external_uid:
            updates["external_uid"] = payload.external_uid.strip()
        users.update_one({"user_id": existing["user_id"]}, {"$set": updates})
        user = serialize_user(get_user_or_404(existing["user_id"]))
        return {
            "message": "User synced",
            "user_id": user["user_id"],
            "client_id": user["client_id"],
            "access_token": access_token,
            "account_type": AccountType.challenge,
        }

    created = create_user(payload)
    return {
        "message": "User synced",
        "user_id": created["user_id"],
        "client_id": created["client_id"],
        "access_token": created["access_token"],
        "account_type": AccountType.challenge,
    }


def set_active_plan(request: PlanRequest) -> Dict[str, Any]:
    user = get_user_or_404(request.user_id)
    users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"active_plan_amount": request.amount, "updated_at": now_utc()}},
    )
    return {"message": "Active plan updated", "user_id": user["user_id"], "active_plan_amount": request.amount}


def update_wallet(user_id: str, account_type: AccountType, amount: float, note: Optional[str], admin: bool = True) -> Dict[str, Any]:
    user = get_user_or_404(user_id)
    field = "challenge_virtual_capital" if account_type == AccountType.challenge else "real_capital"
    timestamp = now_utc()
    users.update_one({"user_id": user["user_id"]}, {"$set": {field: amount, "updated_at": timestamp}})
    wallet_logs.insert_one(
        {
            "user_id": user["user_id"],
            "client_id": user["client_id"],
            "account_type": account_type.value,
            "amount": amount,
            "field": field,
            "note": note,
            "source": "admin" if admin else "system",
            "created_at": timestamp,
        }
    )
    return get_wallet(user["user_id"], account_type)


def get_wallet(user_id: str, account_type: AccountType) -> Dict[str, Any]:
    user = serialize_user(get_user_or_404(user_id))
    if account_type == AccountType.challenge:
        return {
            "user_id": user["user_id"],
            "account_type": account_type,
            "virtual_capital": user["challenge_virtual_capital"],
            "profit": user["challenge_profit"],
            "loss": user["challenge_loss"],
            "balance": user["challenge_balance"],
            "margin_used": user["challenge_margin_used"],
            "available_balance": user["challenge_available_balance"],
        }
    return {
        "user_id": user["user_id"],
        "account_type": account_type,
        "real_capital": user["real_capital"],
        "profit": user["real_profit"],
        "loss": user["real_loss"],
        "balance": user["real_balance"],
        "margin_used": user["real_margin_used"],
        "available_balance": user["real_available_balance"],
    }


def create_support_ticket(payload: SupportCreateRequest) -> Dict[str, Any]:
    user = get_user_or_404(payload.user_id)
    timestamp = now_utc()
    message = {"sender": "client", "message": payload.message.strip(), "timestamp": timestamp}
    ticket = {
        "ticket_id": new_uuid(),
        "complaint_id": _unique_value(support_tickets, "complaint_id", "CMP", 6),
        "user_id": user["user_id"],
        "user_name": user["name"],
        "message": payload.message.strip(),
        "status": "open",
        "timestamp": timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
        "messages": [message],
    }
    support_tickets.insert_one(ticket)
    return clean_dict(ticket)


def reply_support_ticket(complaint_id: str, payload: SupportReplyRequest) -> Dict[str, Any]:
    message = {"sender": "admin", "message": payload.message.strip(), "timestamp": now_utc()}
    result = support_tickets.update_one(
        {"complaint_id": complaint_id},
        {"$push": {"messages": message}, "$set": {"status": "replied", "updated_at": now_utc()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Complaint not found")
    return clean_dict(support_tickets.find_one({"complaint_id": complaint_id}))


def list_support_tickets() -> List[Dict[str, Any]]:
    return [clean_dict(ticket) for ticket in support_tickets.find({}).sort("updated_at", -1)]


def request_mpin_otp(user_id: str) -> Dict[str, Any]:
    user = get_user_or_404(user_id)
    code = random_code(6)
    expires_at = now_utc() + timedelta(seconds=settings.otp_ttl_seconds)
    otps.update_one(
        {"user_id": user["user_id"], "purpose": "mpin_change"},
        {"$set": {"otp": code, "expires_at": expires_at, "created_at": now_utc()}},
        upsert=True,
    )
    return {
        "user_id": user["user_id"],
        "otp": code,
        "expires_in_seconds": settings.otp_ttl_seconds,
        "message": "OTP generated for registered mobile",
    }


def change_mpin(payload: ChangeMpinRequest) -> Dict[str, str]:
    if payload.new_mpin != payload.confirm_mpin:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MPIN confirmation does not match")
    user = get_user_or_404(payload.user_id)
    otp_doc = otps.find_one({"user_id": user["user_id"], "purpose": "mpin_change"})
    if not otp_doc or otp_doc.get("otp") != payload.otp or otp_doc.get("expires_at") < now_utc():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP")
    digest, salt = hash_secret(payload.new_mpin)
    users.update_one({"user_id": user["user_id"]}, {"$set": {"mpin_hash": digest, "mpin_salt": salt, "updated_at": now_utc()}})
    otps.delete_many({"user_id": user["user_id"], "purpose": "mpin_change"})
    return {"message": "MPIN changed successfully"}


def login_with_mpin(payload: MpinLoginRequest) -> Dict[str, Any]:
    user = get_user_or_404(payload.user_id)
    if not user.get("mpin_hash") or not user.get("mpin_salt"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MPIN is not set")
    if not verify_secret(payload.mpin, user["mpin_hash"], user["mpin_salt"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid MPIN")
    access_token = user.get("access_token") or new_uuid()
    users.update_one({"user_id": user["user_id"]}, {"$set": {"access_token": access_token, "updated_at": now_utc()}})
    return {
        "message": "MPIN verified",
        "user_id": user["user_id"],
        "client_id": user["client_id"],
        "access_token": access_token,
        "account_type": AccountType.challenge,
    }


def list_referrals() -> List[Dict[str, Any]]:
    return [
        {
            "referrer_user_id": item.get("referrer_user_id", ""),
            "referrer_name": item.get("referrer_name", ""),
            "referred_user_id": item.get("referred_user_id", ""),
            "referred_name": item.get("referred_name", ""),
            "date_time": item.get("created_at"),
            "registration_status": item.get("registration_status", "Registered"),
            "payment_status": item.get("payment_status", "Not Paid"),
        }
        for item in referrals.find({}).sort("created_at", -1)
    ]


def set_referral_payment(referral_id: str, paid: bool) -> Dict[str, str]:
    result = referrals.update_one({"referred_user_id": referral_id}, {"$set": {"payment_status": "Paid" if paid else "Not Paid"}})
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Referral not found")
    return {"message": "Referral payment status updated"}


def admin_stats() -> AdminStatsResponse:
    return AdminStatsResponse(
        total_users=users.count_documents({}),
        total_referrals=referrals.count_documents({}),
        open_support_tickets=support_tickets.count_documents({"status": {"$ne": "closed"}}),
        real_trades=trades.count_documents({"account_type": "real"}),
        challenge_trades=trades.count_documents({"account_type": "challenge"}),
    )


def get_risk_profile(user_id: str) -> Dict[str, Any]:
    user = serialize_user(get_user_or_404(user_id))
    profile = risk_profiles.find_one({"user_id": user["user_id"]}) or {}
    return {
        "user_id": user["user_id"],
        "client_id": user["client_id"],
        "max_loss_limit": float(profile.get("max_loss_limit", user.get("max_loss_limit", 0))),
        "max_drawdown_limit": float(profile.get("max_drawdown_limit", user.get("max_drawdown_limit", 0))),
        "daily_loss_limit": float(profile.get("daily_loss_limit", user.get("daily_loss_limit", 0))),
        "max_position_size": int(profile.get("max_position_size", user.get("max_position_size", 0)) or 0),
        "trading_enabled": bool(user.get("trading_enabled", True)),
        "blocked_margin": float(user.get("real_margin_used", 0)),
    }


def update_risk_profile(user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    user = get_user_or_404(user_id)
    updates = {"updated_at": now_utc()}
    user_updates = {"updated_at": now_utc()}
    for field in ("max_loss_limit", "max_drawdown_limit", "daily_loss_limit", "max_position_size"):
        if payload.get(field) is not None:
            updates[field] = payload[field]
            user_updates[field] = payload[field]
    if payload.get("trading_enabled") is not None:
        user_updates["trading_enabled"] = bool(payload["trading_enabled"])

    risk_profiles.update_one({"user_id": user["user_id"]}, {"$set": updates}, upsert=True)
    users.update_one({"user_id": user["user_id"]}, {"$set": user_updates})
    return get_risk_profile(user["user_id"])


def master_broker_status() -> Dict[str, Any]:
    doc = master_broker.find_one({"broker_name": "upstox"}) or {}
    return {
        "broker_name": "upstox",
        "connected": bool(doc.get("connected", False) or settings.upstox_access_token),
        "demo_mode": settings.demo_broker_mode,
        "base_url": settings.upstox_base_url,
        "account_id": settings.upstox_account_id or doc.get("account_id") or None,
        "has_access_token": bool(settings.upstox_access_token),
    }


def get_user_by_access_token(access_token: str) -> Dict[str, Any]:
    user = users.find_one({"access_token": access_token})
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
    return user


def get_user_default_account_type(user: Dict[str, Any]) -> AccountType:
    raw_account_type = user.get("account_type") or AccountType.challenge.value
    try:
        return AccountType(raw_account_type)
    except ValueError:
        return AccountType.challenge
