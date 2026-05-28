"""Patient self-login by phone / Aadhaar / ABHA + first-time password setup.

Design
------
Patients are enrolled by field agents and do not have a Firebase email
account. To let them sign in directly we synthesize an internal email of
the form ``<patient_id>@telelepra.local`` and store it on the patient
document. Firebase Auth becomes the source of truth for the password,
and the regular ``signInWithEmailAndPassword`` flow works once the
synthetic email is created.

Endpoints
~~~~~~~~~
* ``POST /patient-auth/lookup`` — given an identifier (phone / Aadhaar / ABHA),
  return whether a patient record exists and whether they already have a
  password set. No auth required.

* ``POST /patient-auth/init`` — first-time password setup. Creates the
  Firebase user, links ``patient_uid``, sets the role claim, and returns
  the synthetic email so the frontend can sign in. No auth required.

* ``POST /patient-auth/prepare-login`` — for repeat logins. Returns the
  synthetic email so the frontend can call Firebase
  ``signInWithEmailAndPassword`` directly. No auth required.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.firebase import get_auth, get_db, init_firebase

log = logging.getLogger(__name__)

router = APIRouter(prefix="/patient-auth", tags=["patient-auth"])

SYNTHETIC_DOMAIN = "telelepra.local"
_AADHAAR_RE = re.compile(r"^\d{12}$")
_ABHA_RE = re.compile(r"^\d{14}$")


def _normalize(identifier: str) -> str:
    return re.sub(r"[\s-]", "", (identifier or "").strip())


def _identifier_type(identifier: str) -> str:
    """Classify by length after normalization."""
    v = _normalize(identifier)
    if _AADHAAR_RE.match(v):
        return "aadhaar"
    if _ABHA_RE.match(v):
        return "abha"
    # Fallback to phone — accepts 7-15 digits with optional +
    if re.match(r"^\+?\d{7,15}$", v):
        return "phone"
    return "unknown"


def _find_patient(identifier: str) -> tuple[dict | None, object | None]:
    """Find a patient document by phone / aadhaar / abha. Returns (data, doc_ref)."""
    db = get_db()
    v = _normalize(identifier)
    kind = _identifier_type(v)
    if kind == "unknown":
        return None, None

    field_map = {"phone": "phone", "aadhaar": "aadhaar_id", "abha": "abha_id"}
    field = field_map[kind]

    # Try the normalized value first; for phone also try with leading + or
    # the raw form, since agents may have entered the number either way.
    candidates = [v]
    if kind == "phone" and not v.startswith("+"):
        candidates.append("+" + v)
    if kind == "phone" and v.startswith("+"):
        candidates.append(v.lstrip("+"))

    for candidate in candidates:
        snaps = list(
            db.collection("patients").where(field, "==", candidate).limit(1).stream()
        )
        if snaps:
            return snaps[0].to_dict(), snaps[0].reference
    return None, None


def _synthetic_email(patient_id: str) -> str:
    return f"{patient_id}@{SYNTHETIC_DOMAIN}"


# ----- request bodies -----

class IdentifierBody(BaseModel):
    identifier: str = Field(min_length=7)


class InitBody(BaseModel):
    identifier: str = Field(min_length=7)
    password: str = Field(min_length=6, description="Patient-chosen password (≥ 6 chars)")


# ----- endpoints -----

@router.post("/lookup")
def lookup(body: IdentifierBody):
    """Lightweight pre-check used by the patient login form.

    Tells the frontend whether to show "Set password" (first-time) or
    "Enter password" (returning).
    """
    init_firebase()
    kind = _identifier_type(body.identifier)
    if kind == "unknown":
        raise HTTPException(
            400,
            "Enter a 10-digit phone, 12-digit Aadhaar, or 14-digit ABHA number.",
        )

    patient, _ = _find_patient(body.identifier)
    if not patient:
        return {
            "exists": False,
            "has_password": False,
            "identifier_type": kind,
        }
    return {
        "exists": True,
        "has_password": bool(patient.get("patient_uid")),
        "identifier_type": kind,
        "patient_id": patient.get("id"),
        "name": patient.get("name"),
    }


@router.post("/init")
def init_password(body: InitBody):
    """First-time password setup for an enrolled patient.

    Idempotent failure: if the patient already has a ``patient_uid``
    we refuse — they should sign in normally instead.
    """
    init_firebase()
    patient, ref = _find_patient(body.identifier)
    if not patient:
        raise HTTPException(
            404,
            "No patient record matches that number. Please contact your health worker to enrol you first.",
        )
    if patient.get("patient_uid"):
        raise HTTPException(
            409,
            "Account already exists. Sign in with your password instead.",
        )

    patient_id = patient["id"]
    email = patient.get("synthetic_email") or _synthetic_email(patient_id)

    # Create Firebase user. If one already exists (e.g. partial setup from
    # a prior attempt), update the password instead.
    fb = get_auth()
    try:
        user_record = fb.create_user(
            email=email,
            password=body.password,
            display_name=patient.get("name") or "Patient",
            email_verified=True,
        )
    except Exception as e:
        # Fallback: an account with this email already exists; update password.
        if "EMAIL_ALREADY_EXISTS" in str(e) or "EMAIL_EXISTS" in str(e):
            try:
                existing = fb.get_user_by_email(email)
                fb.update_user(existing.uid, password=body.password)
                user_record = existing
            except Exception as e2:
                log.error("Failed to recover existing Firebase user: %s", e2)
                raise HTTPException(500, "Failed to set password") from e2
        else:
            log.error("Firebase create_user failed for %s: %s", email, e)
            raise HTTPException(500, "Failed to create patient account") from e

    fb.set_custom_user_claims(user_record.uid, {"role": "patient"})
    ref.set(
        {
            "patient_uid": user_record.uid,
            "synthetic_email": email,
        },
        merge=True,
    )

    # Mirror into /users so the rest of the app can resolve display info.
    db = get_db()
    db.collection("users").document(user_record.uid).set(
        {
            "uid": user_record.uid,
            "email": email,
            "name": patient.get("name") or "Patient",
            "role": "patient",
        },
        merge=True,
    )

    return {"ok": True, "email": email, "patient_id": patient_id}


@router.post("/prepare-login")
def prepare_login(body: IdentifierBody):
    """Return the synthetic email for a returning patient.

    The frontend then calls Firebase signInWithEmailAndPassword with
    this email + the patient's password.
    """
    init_firebase()
    patient, _ = _find_patient(body.identifier)
    if not patient:
        raise HTTPException(
            404,
            "No patient record matches that number. Please contact your health worker.",
        )
    if not patient.get("patient_uid"):
        # Patient is enrolled but hasn't set a password yet — route them
        # to the init flow.
        return {
            "needs_init": True,
            "patient_id": patient.get("id"),
            "name": patient.get("name"),
        }
    email = patient.get("synthetic_email") or _synthetic_email(patient["id"])
    return {"needs_init": False, "email": email, "name": patient.get("name")}
