import logging
from typing import Any, Dict, Optional, Set

import firebase_admin
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as firebase_auth

from app.config import get_settings

logger = logging.getLogger(__name__)
admin_bearer_scheme = HTTPBearer(auto_error=False)


def _admin_uid_allowlist() -> Set[str]:
    return {
        value.strip()
        for value in get_settings().admin_uids.split(",")
        if value.strip()
    }


def _firebase_app() -> firebase_admin.App:
    try:
        return firebase_admin.get_app()
    except ValueError:
        settings = get_settings()
        if not settings.firebase_project_id.strip():
            raise RuntimeError("FIREBASE_PROJECT_ID is not configured")
        return firebase_admin.initialize_app(
            options={"projectId": settings.firebase_project_id.strip()}
        )


def get_firebase_user_profile(uid: str) -> Optional[Dict[str, Any]]:
    """Read a registration profile using Admin SDK credentials, never client claims."""
    from firebase_admin import firestore

    try:
        snapshot = (
            firestore.client(app=_firebase_app())
            .collection("users")
            .document(uid)
            .get()
        )
    except Exception as exc:
        logger.exception("Firebase profile lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Profile verification service unavailable",
        ) from exc
    return snapshot.to_dict() if snapshot.exists else None


def get_current_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(admin_bearer_scheme),
) -> Dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    allowed_uids = _admin_uid_allowlist()
    if not allowed_uids:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin access is not configured",
        )

    try:
        decoded = firebase_auth.verify_id_token(
            credentials.credentials.strip(),
            app=_firebase_app(),
        )
    except firebase_auth.InvalidIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Firebase token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception:
        logger.exception("Firebase admin token verification failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication service unavailable",
        )

    if decoded.get("uid") not in allowed_uids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access denied",
        )
    return decoded


def verify_firebase_bearer_token(token: str) -> Dict[str, Any]:
    """Validate a Firebase ID token and return its verified claims."""
    try:
        return firebase_auth.verify_id_token(token.strip(), app=_firebase_app())
    except firebase_auth.InvalidIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Firebase token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception:
        logger.exception("Firebase user token verification failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase authentication service unavailable",
        )
