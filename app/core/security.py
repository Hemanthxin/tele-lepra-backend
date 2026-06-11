from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from firebase_admin import auth as fb_auth

from .firebase import init_firebase

# Legacy role kept only for comparing against any pre-existing records; the
# patient portal/login has been removed, so it is NOT an assignable role.
ROLE_PATIENT = "patient"
ROLE_AGENT = "agent"
ROLE_MO = "mo"
ROLE_ADMIN = "admin"
ALL_ROLES = {ROLE_AGENT, ROLE_MO, ROLE_ADMIN}


class CurrentUser:
    def __init__(self, uid: str, email: Optional[str], role: str, claims: dict):
        self.uid = uid
        self.email = email
        self.role = role
        self.claims = claims


def _extract_bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )
    return header.split(" ", 1)[1]


def get_current_user(request: Request) -> CurrentUser:
    init_firebase()
    token = _extract_bearer(request)
    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )
    role = decoded.get("role") or "patient"
    return CurrentUser(
        uid=decoded["uid"],
        email=decoded.get("email"),
        role=role,
        claims=decoded,
    )


def require_roles(*roles: str):
    def _checker(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role in {roles}",
            )
        return user

    return _checker
