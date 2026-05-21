import hashlib
import random
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def public_id(prefix: str, digits: int = 6) -> str:
    return f"{prefix}{random.randint(0, (10 ** digits) - 1):0{digits}d}"


def random_code(length: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def normalize_email(email: str) -> str:
    return email.strip().lower()


def clean_dict(document: dict[str, Any] | None) -> dict[str, Any]:
    if not document:
        return {}
    data = dict(document)
    data.pop("_id", None)
    return data


def hash_secret(value: str, salt: str | None = None) -> tuple[str, str]:
    secret_salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256(f"{secret_salt}:{value}".encode("utf-8")).hexdigest()
    return digest, secret_salt


def verify_secret(value: str, digest: str, salt: str) -> bool:
    actual, _ = hash_secret(value, salt)
    return secrets.compare_digest(actual, digest)


def new_uuid() -> str:
    return str(uuid.uuid4())
