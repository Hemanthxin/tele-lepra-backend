from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from google.cloud.firestore import Query

from ..core.firebase import get_db
from ..core.security import (
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_MO,
    ROLE_PATIENT,
    CurrentUser,
    get_current_user,
    require_roles,
)
from ..models.schemas import Patient, PatientCreate, SelfEnrollment

router = APIRouter(prefix="/patients", tags=["patients"])


@router.post(
    "",
    response_model=Patient,
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_ADMIN))],
)
def create_patient(
    body: PatientCreate, user: CurrentUser = Depends(get_current_user)
):
    if not body.consent_given:
        raise HTTPException(400, "Consent is required to enroll a patient.")
    db = get_db()
    ref = db.collection("patients").document()
    data = body.model_dump()
    data.update(
        {
            "id": ref.id,
            "created_at": datetime.now(timezone.utc),
            "created_by": user.uid,
        }
    )
    ref.set(data)
    return data


@router.get(
    "",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_MO, ROLE_ADMIN))],
)
def list_patients(q: str | None = None, limit: int = 50):
    db = get_db()
    coll = db.collection("patients").order_by(
        "created_at", direction=Query.DESCENDING
    ).limit(limit)
    docs = [d.to_dict() for d in coll.stream()]
    if q:
        ql = q.lower()
        docs = [
            d
            for d in docs
            if ql in (d.get("name", "").lower())
            or ql in (d.get("phone", ""))
        ]

    # Attach latest case status + triage outcome for each patient.
    patient_ids = [d.get("id") for d in docs if d.get("id")]
    latest_case_by_pid: dict[str, dict] = {}
    if patient_ids:
        # Firestore "in" queries are capped at 30 ids per query.
        for i in range(0, len(patient_ids), 30):
            chunk = patient_ids[i : i + 30]
            case_snaps = (
                db.collection("cases").where("patient_id", "in", chunk).stream()
            )
            for cs in case_snaps:
                c = cs.to_dict()
                pid = c.get("patient_id")
                if not pid:
                    continue
                prev = latest_case_by_pid.get(pid)
                ts = c.get("updated_at") or c.get("created_at")
                prev_ts = (
                    (prev or {}).get("updated_at") or (prev or {}).get("created_at")
                    if prev
                    else None
                )
                if prev is None or (ts and prev_ts and ts > prev_ts):
                    latest_case_by_pid[pid] = c

    for d in docs:
        c = latest_case_by_pid.get(d.get("id"))
        if c:
            d["latest_case"] = {
                "id": c.get("id"),
                "condition": c.get("condition"),
                "status": c.get("status"),
                "triage_outcome": c.get("triage_outcome"),
                "reason": (c.get("triage") or {}).get("reasons"),
            }
        else:
            d["latest_case"] = None

    return docs


@router.get("/{patient_id}")
def get_patient(
    patient_id: str, user: CurrentUser = Depends(get_current_user)
):
    db = get_db()
    doc = db.collection("patients").document(patient_id).get()
    if not doc.exists:
        raise HTTPException(404, "Patient not found")
    data = doc.to_dict()
    # Patients can only fetch themselves
    if user.role == "patient" and data.get("patient_uid") != user.uid:
        raise HTTPException(403, "Forbidden")
    return data


@router.get("/me")
def get_my_patient_record(user: CurrentUser = Depends(get_current_user)):
    """Return the patient record linked to the current user, or 404."""
    db = get_db()
    snaps = list(
        db.collection("patients")
        .where("patient_uid", "==", user.uid)
        .limit(1)
        .stream()
    )
    if not snaps:
        raise HTTPException(404, "No patient record for this user")
    return snaps[0].to_dict()


@router.post(
    "/self-enroll",
    dependencies=[Depends(require_roles(ROLE_PATIENT))],
)
def self_enroll(
    body: SelfEnrollment, user: CurrentUser = Depends(get_current_user)
):
    if not body.consent_given:
        raise HTTPException(400, "Consent is required to enrol.")
    db = get_db()
    existing = list(
        db.collection("patients")
        .where("patient_uid", "==", user.uid)
        .limit(1)
        .stream()
    )
    if existing:
        raise HTTPException(409, "Patient record already exists for this user.")
    ref = db.collection("patients").document()
    payload = body.model_dump()
    payload.update(
        {
            "id": ref.id,
            "created_at": datetime.now(timezone.utc),
            "created_by": user.uid,
            "patient_uid": user.uid,
        }
    )
    ref.set(payload)
    return payload


@router.post("/link-self")
def link_patient_to_auth_user(
    phone: str, user: CurrentUser = Depends(get_current_user)
):
    """
    A logged-in patient links their auth uid to an existing record the
    agent created. Match on phone for the MVP.
    """
    db = get_db()
    snaps = list(
        db.collection("patients").where("phone", "==", phone).limit(1).stream()
    )
    if not snaps:
        raise HTTPException(404, "No patient record matches that phone")
    doc = snaps[0]
    doc.reference.set({"patient_uid": user.uid}, merge=True)
    return {"ok": True, "patient_id": doc.id}
