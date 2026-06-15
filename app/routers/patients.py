import io
import csv
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
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
    "/export/excel",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_MO, ROLE_ADMIN))],
)
def export_patients_excel(patient_id: str | None = None, q: str | None = None, escalated: bool = False):
    db = get_db()
    coll = db.collection("patients")

    if patient_id:
        doc = coll.document(patient_id).get()
        docs = [doc.to_dict()] if doc.exists else []
    else:
        docs_snaps = coll.stream()
        docs = [d.to_dict() for d in docs_snaps if d.exists]
        if q:
            q_low = q.lower()
            docs = [d for d in docs if q_low in str(d.get("name", "")).lower() or q_low in str(d.get("phone", ""))]

    patient_ids = [d.get("id") for d in docs if d.get("id")]
    latest_case_by_pid = {}
    if patient_ids:
        for i in range(0, len(patient_ids), 30):
            chunk = patient_ids[i : i + 30]
            case_snaps = db.collection("cases").where("patient_id", "in", chunk).stream()
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

    if escalated:
        filtered_docs = []
        for d in docs:
            c = latest_case_by_pid.get(d.get("id"))
            if c and c.get("triage_outcome") == "escalate":
                filtered_docs.append(d)
        docs = filtered_docs

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Patient ID", "Name", "Age", "Sex", "Phone", "Location",
        "Aadhaar ID", "ABHA ID", "Consent Given", "Created At",
        "Latest Case ID", "Latest Case Status", "Triage Outcome"
    ])

    for d in docs:
        c = latest_case_by_pid.get(d.get("id"))
        row = [
            d.get("id"),
            d.get("name"),
            d.get("age"),
            d.get("sex"),
            d.get("phone"),
            d.get("location"),
            d.get("aadhaar_id"),
            d.get("abha_id"),
            d.get("consent_given"),
            d.get("created_at"),
            c.get("id") if c else None,
            c.get("status") if c else None,
            c.get("triage_outcome") if c else None
        ]
        writer.writerow([str(x) if x is not None else "" for x in row])

    output.seek(0)
    filename = f"patient_{patient_id}.csv" if patient_id else ("escalated_patients.csv" if escalated else "patients_export.csv")
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get(
    "",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_MO, ROLE_ADMIN))],
)
def list_patients(q: str | None = None, limit: int = 50):
    db = get_db()
    coll = db.collection("patients")

    if q:
        # Client-side filtering is safer for partial text matches if no full-text search engine is wired.
        # So we fetch a broader set. In a real app, integrate Typesense / Algolia.
        docs = [d.to_dict() for d in coll.order_by("created_at", direction=Query.DESCENDING).limit(500).stream()]
        q_low = q.lower()
        docs = [d for d in docs if q_low in d.get("name", "").lower() or q_low in str(d.get("phone", ""))]
        docs = docs[:limit]
    else:
        docs = [
            d.to_dict()
            for d in coll.order_by("created_at", direction=Query.DESCENDING)
            .limit(limit)
            .stream()
        ]

    # Batch fetch latest case for the fetched patients to provide triage status previews
    patient_ids = [d.get("id") for d in docs if d.get("id")]
    latest_case_by_pid = {}

    if patient_ids:
        # Firestore 'in' queries are limited to 30 items
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

    # Get history — sort in Python to avoid requiring a composite Firestore index.
    cases = [
        c.to_dict()
        for c in db.collection("cases")
        .where("patient_id", "==", patient_id)
        .stream()
    ]
    cases.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    data["cases"] = cases
    return data