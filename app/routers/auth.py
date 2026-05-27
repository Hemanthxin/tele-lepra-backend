from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..core.firebase import get_auth, get_db
from ..core.security import (
    ALL_ROLES,
    ROLE_ADMIN,
    CurrentUser,
    get_current_user,
    require_roles,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class RoleAssignment(BaseModel):
    uid: str
    role: str


class SelfRegister(BaseModel):
    name: str
    role: str = "agent"  # only agent / mo self-register is allowed


class ProfileUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    language: str | None = None
    theme: str | None = None


@router.get("/me")
def me(user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    snap = db.collection("users").document(user.uid).get()
    profile_doc = snap.to_dict() if snap.exists else None
    # Firestore profile.role is the source of truth. If the custom claim
    # drifted (e.g. bootstrap partially succeeded, or the user signed in
    # before claims propagated), re-sync it so subsequent token refreshes
    # carry the correct role.
    profile_role = (profile_doc or {}).get("role")
    canonical_role = profile_role or user.role
    if profile_role and profile_role in ALL_ROLES and profile_role != user.role:
        try:
            get_auth().set_custom_user_claims(user.uid, {"role": profile_role})
        except Exception:
            pass
    return {
        "uid": user.uid,
        "email": user.email,
        "role": canonical_role,
        "profile": profile_doc,
    }


SELF_SIGNUP_ROLES = {"agent", "mo"}


@router.post("/bootstrap")
def bootstrap_profile(
    body: SelfRegister, user: CurrentUser = Depends(get_current_user)
):
    """
    Called once after Firebase Auth signup. Creates the user's profile
    document. Only 'agent' and 'mo' may self-register. Admins are
    provisioned out-of-band (see scripts/make_admin.py). Patients are
    enrolled by agents, not via public signup.
    """
    if body.role not in SELF_SIGNUP_ROLES:
        raise HTTPException(400, f"Self-signup role must be one of {SELF_SIGNUP_ROLES}")
    role = body.role
    db = get_db()
    db.collection("users").document(user.uid).set(
        {
            "uid": user.uid,
            "email": user.email,
            "name": body.name,
            "role": role,
        },
        merge=True,
    )
    get_auth().set_custom_user_claims(user.uid, {"role": role})
    return {"ok": True, "role": role}


@router.patch("/me")
def update_my_profile(
    body: ProfileUpdate, user: CurrentUser = Depends(get_current_user)
):
    """Update name / phone / language / theme on the current user's profile."""
    db = get_db()
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update:
        return {"ok": True, "updated": {}}
    db.collection("users").document(user.uid).set(update, merge=True)
    return {"ok": True, "updated": update}


@router.post("/set-role", dependencies=[Depends(require_roles(ROLE_ADMIN))])
def set_role(body: RoleAssignment):
    if body.role not in ALL_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of {ALL_ROLES}")
    db = get_db()
    db.collection("users").document(body.uid).set(
        {"role": body.role}, merge=True
    )
    get_auth().set_custom_user_claims(body.uid, {"role": body.role})
    return {"ok": True}
