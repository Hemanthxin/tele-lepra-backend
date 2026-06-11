from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from google.cloud.firestore import Query

from ..core.firebase import get_db
from ..core.security import (
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_MO,
    CurrentUser,
    get_current_user,
    require_roles,
)
from ..models.schemas import Patient, PatientCreate
from ..services.ids import generate_code

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
    ref = db.collection("patients").document(generate_code(db, "patients"))
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
    return data
