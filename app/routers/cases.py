import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from google.cloud.firestore import Query

from ..core.config import settings
from ..core.firebase import copy_url_into, get_db, patient_storage_key, upload_public
from ..core.security import (
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_MO,
    CurrentUser,
    get_current_user,
    require_roles,
)
from ..models.schemas import (
    CaseCreate,
    CaseStatus,
    HistoryEntry,
    MOClinicalAssessment,
    MODecision,
    Screening,
)
from ..services import whatsapp
from ..services.ids import generate_code
from ..services.rule_engine import inferred_conditions, triage
from .reports import build_and_upload_decision_pdf, build_and_upload_intake_pdf

log = logging.getLogger(__name__)


def _patient_phone_name(db, patient_id: str):
    """Look up the patient document and return (phone, name)."""
    try:
        snap = db.collection("patients").document(patient_id).get()
        if snap.exists:
            d = snap.to_dict()
            return d.get("phone"), d.get("name") or "Patient"
    except Exception as e:  # pragma: no cover
        log.warning("patient lookup failed for %s: %s", patient_id, e)
    return None, "Patient"


def _user_phone_name(db, uid: str | None):
    """Look up a staff user's profile and return (phone, name)."""
    if not uid:
        return None, None
    try:
        snap = db.collection("users").document(uid).get()
        if snap.exists:
            d = snap.to_dict()
            return d.get("phone"), d.get("name")
    except Exception as e:  # pragma: no cover
        log.warning("user lookup failed for %s: %s", uid, e)
    return None, None


def _wa_send(phone: str | None, template: str, params: list[str]) -> dict:
    """Best-effort WhatsApp dispatch — never raises."""
    if not phone:
        return {"skipped": True, "reason": "no_phone"}
    try:
        return whatsapp.send_template(phone, template, params, language=settings.wa_lang)
    except whatsapp.WhatsAppError as e:
        log.warning("WA dispatch failed (%s): %s", template, e)
        return {"error": str(e)}

router = APIRouter(prefix="/cases", tags=["cases"])


def _now():
    return datetime.now(timezone.utc)


def _get_case_or_404(db, case_id: str):
    snap = db.collection("cases").document(case_id).get()
    if not snap.exists:
        raise HTTPException(404, "Case not found")
    return snap


@router.post(
    "",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_ADMIN))],
)
def create_case(body: CaseCreate, user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    pat = db.collection("patients").document(body.patient_id).get()
    if not pat.exists:
        raise HTTPException(404, "Patient not found")
    pdata = pat.to_dict()
    ref = db.collection("cases").document(generate_code(db, "cases"))
    data = {
        "id": ref.id,
        "patient_id": body.patient_id,
        "patient_name": pdata["name"],
        "condition": body.condition,
        "status": CaseStatus.intake.value,
        "created_at": _now(),
        "updated_at": _now(),
        "created_by": user.uid,
        # Denormalised programme + household context for cheap MO-queue grouping.
        "phc": pdata.get("phc"),
        "household_number": pdata.get("household_number"),
    }
    ref.set(data)
    return data


@router.post(
    "/{case_id}/history",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_ADMIN))],
)
def add_history(case_id: str, body: HistoryEntry):
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    snap.reference.set(
        {"history": body.model_dump(), "updated_at": _now()}, merge=True
    )
    return {"ok": True}


@router.post(
    "/{case_id}/screen",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_ADMIN))],
)
def submit_screening(case_id: str, body: Screening):
    """Store the symptom screening and route the case to the Medical Officer.

    The agent only collects data — there is no agent-side decision. The triage
    result is stored purely as a leprosy risk summary for the MO's reference.
    """
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    result = triage(body)

    # The agent only collects data — every screened case goes to the Medical
    # Officer queue. The triage result is stored purely as a risk summary.
    suspected = inferred_conditions(result)
    screening_doc = body.model_dump()
    screening_doc["suspected_diseases"] = suspected
    update = {
        "screening": screening_doc,
        "suspected_diseases": suspected,
        "triage": result.model_dump(),
        "triage_outcome": result.outcome.value,
        "status": CaseStatus.awaiting_mo.value,
        "updated_at": _now(),
    }
    snap.reference.set(update, merge=True)

    # Gather artefacts into the patient's Storage folder (best-effort — never
    # blocks the screening if Storage is unavailable). Produces the agent report
    # PDF and copies the intake images / lab reports into the folder.
    case_data = snap.to_dict() or {}
    patient_id = case_data.get("patient_id")
    if patient_id:
        try:
            pat = db.collection("patients").document(patient_id).get()
            pname = (pat.to_dict() or {}).get("name") if pat.exists else None
            full_case = {**case_data, **update, "id": case_id, "patient_id": patient_id}

            info = build_and_upload_intake_pdf(full_case)
            if info:
                snap.reference.set({"agent_report_url": info[0]}, merge=True)

            imgs = [
                copy_url_into(u, patient_storage_key(patient_id, pname, f"images/image-{i + 1}.jpg")) or u
                for i, u in enumerate(screening_doc.get("image_urls") or [])
            ]
            labs = [
                copy_url_into(u, patient_storage_key(patient_id, pname, f"labs/lab-{i + 1}.jpg")) or u
                for i, u in enumerate(screening_doc.get("lab_urls") or [])
            ]
            if imgs or labs:
                snap.reference.set(
                    {"patient_folder_images": imgs, "patient_folder_labs": labs}, merge=True,
                )
        except Exception as e:  # pragma: no cover
            log.warning("Patient-folder artefact gathering failed for %s: %s", case_id, e)

    return result.model_dump()


def _patient_name_for(db, patient_id: str | None) -> str | None:
    if not patient_id:
        return None
    doc = db.collection("patients").document(patient_id).get()
    return (doc.to_dict() or {}).get("name") if doc.exists else None


@router.post(
    "/{case_id}/documents",
    dependencies=[Depends(require_roles(ROLE_MO, ROLE_ADMIN))],
)
async def upload_post_consult_document(case_id: str, file: UploadFile = File(...)):
    """MO uploads a post-consultation document (PDF/image) into the patient folder."""
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    case_data = snap.to_dict() or {}
    patient_id = case_data.get("patient_id")
    pname = _patient_name_for(db, patient_id)

    raw_name = file.filename or "document.pdf"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip("-") or "document.pdf"
    ts = _now().strftime("%Y%m%d-%H%M%S")
    key = patient_storage_key(patient_id or case_id, pname, f"consultation/{ts}-{safe}")
    content = await file.read()
    url = upload_public(content, key, file.content_type or "application/octet-stream")

    doc = {"name": raw_name, "url": url, "uploaded_at": _now()}
    existing = case_data.get("post_consult_docs") or []
    snap.reference.set(
        {"post_consult_docs": existing + [doc], "updated_at": _now()}, merge=True,
    )
    return doc


@router.post(
    "/{case_id}/recording",
    dependencies=[Depends(require_roles(ROLE_MO, ROLE_ADMIN))],
)
async def upload_consultation_recording(case_id: str, file: UploadFile = File(...)):
    """Store a tele-consultation screen recording in the patient folder."""
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    case_data = snap.to_dict() or {}
    patient_id = case_data.get("patient_id")
    pname = _patient_name_for(db, patient_id)

    ext = (file.filename or "recording.webm").rsplit(".", 1)[-1].lower()
    if ext not in ("webm", "mp4", "ogg"):
        ext = "webm"
    ts = _now().strftime("%Y%m%d-%H%M%S")
    fname = f"consultation-{ts}.{ext}"
    key = patient_storage_key(patient_id or case_id, pname, f"recordings/{fname}")
    content = await file.read()
    url = upload_public(content, key, file.content_type or "video/webm")

    rec = {"filename": fname, "url": url, "uploaded_at": _now()}
    existing = case_data.get("recordings") or []
    snap.reference.set(
        {"recordings": existing + [rec], "updated_at": _now()}, merge=True,
    )
    return rec


@router.get(
    "/queue",
    dependencies=[Depends(require_roles(ROLE_MO, ROLE_ADMIN))],
)
def mo_queue(limit: int = 50):
    """Cases awaiting MO review or already scheduled."""
    db = get_db()
    statuses = [
        CaseStatus.awaiting_mo.value,
        CaseStatus.scheduled.value,
        CaseStatus.in_consult.value,
    ]
    # Avoid composite-index requirement: filter only, then sort in Python.
    docs = db.collection("cases").where("status", "in", statuses).stream()
    items = [d.to_dict() for d in docs]
    items.sort(key=lambda x: x.get("updated_at") or _now(), reverse=True)
    return items[:limit]


@router.get("/mine")
def my_cases(user: CurrentUser = Depends(get_current_user)):
    """
    Agent: returns cases the agent created.
    MO: returns cases assigned to the MO.
    """
    db = get_db()
    if user.role == ROLE_AGENT:
        docs = db.collection("cases").where("created_by", "==", user.uid).stream()
    elif user.role == ROLE_MO:
        docs = (
            db.collection("cases")
            .where("assigned_mo_uid", "==", user.uid)
            .stream()
        )
    else:
        docs = db.collection("cases").limit(50).stream()
    return [d.to_dict() for d in docs]


@router.get("/{case_id}")
def get_case(case_id: str, user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    data = snap.to_dict()
    pat_doc = db.collection("patients").document(data["patient_id"]).get()
    pat = pat_doc.to_dict() if pat_doc.exists else {}
    # Flatten patient fields under c.patient_<field> for the MO/agent UI which
    # already renders them inline. Doesn't overwrite same-named keys on the case.
    for key, val in (pat or {}).items():
        if key in ("id", "patient_uid", "synthetic_email", "created_at", "created_by"):
            continue
        target = f"patient_{key}" if key != "name" else "patient_name"
        data.setdefault(target, val)
    return data


@router.post(
    "/{case_id}/clinical-assessment",
    dependencies=[Depends(require_roles(ROLE_MO, ROLE_ADMIN))],
)
def save_clinical_assessment(
    case_id: str,
    body: MOClinicalAssessment,
    user: CurrentUser = Depends(get_current_user),
):
    """Save the MO's post-consultation clinical assessment (PDF1 Teleconsultation block).

    Must be saved at least once before /decision will accept a submission.
    """
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    snap.reference.set(
        {
            "clinical_assessment": body.model_dump(),
            "clinical_assessment_by": user.uid,
            "clinical_assessment_at": _now(),
            "updated_at": _now(),
        },
        merge=True,
    )
    return {"ok": True}


@router.post(
    "/{case_id}/decision",
    dependencies=[Depends(require_roles(ROLE_MO))],
)
def mo_decision(
    case_id: str,
    body: MODecision,
    user: CurrentUser = Depends(get_current_user),
):
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    status_by_decision = {
        "close_remote": CaseStatus.closed_remote.value,
        "alt_dx": CaseStatus.closed_alt_dx.value,
        "refer": CaseStatus.referred.value,
    }
    if body.decision not in status_by_decision:
        raise HTTPException(400, "decision must be close_remote, alt_dx or refer")
    case_data = snap.to_dict() or {}
    if not case_data.get("clinical_assessment"):
        raise HTTPException(
            400,
            "Clinical assessment must be saved before submitting a decision.",
        )
    new_status = status_by_decision[body.decision]
    now = _now()
    update = {
        "status": new_status,
        "mo_decision": body.decision,
        "mo_notes": body.notes,
        "prescription": body.prescription,
        "referral_note": body.referral_note,
        "closed_by": user.uid,
        "closed_at": now,
        "updated_at": now,
    }
    snap.reference.set(update, merge=True)

    # The decision message goes to the patient AND the agent who raised the
    # case — NOT the MO (who made the decision).
    patient_id = case_data.get("patient_id")
    pat_phone, pat_name = _patient_phone_name(db, patient_id) if patient_id else (None, "Patient")
    agent_phone, agent_name = _user_phone_name(db, case_data.get("created_by"))

    if body.decision == "refer":
        outcome_label = "Referred for further care"
        next_step = body.referral_note or "Please visit the referral centre as advised."
    elif body.decision == "alt_dx":
        outcome_label = "Reviewed — alternative diagnosis, treated at community level"
        next_step = body.prescription or "Follow the treatment advised. Recall in 2 weeks if no improvement."
    else:
        outcome_label = "Reviewed — closed at community level"
        next_step = body.prescription or "Follow the prescription provided. Reach out if symptoms worsen."

    # Build + upload the decision PDF from the UPDATED case (merge the decision
    # fields onto the pre-update snapshot). Done independently of WhatsApp: the
    # decision and its PDF are persisted on save, whether or not it is sent.
    pdf_info = build_and_upload_decision_pdf({**case_data, **update, "id": case_id})
    if pdf_info:
        snap.reference.set(
            {"report_url": pdf_info[0], "report_filename": pdf_info[1], "report_generated_at": now},
            merge=True,
        )

    def _send_decision(phone: str | None, name: str | None) -> dict:
        if not phone:
            return {"skipped": True, "reason": "no_phone"}
        res: dict = {}
        if pdf_info:
            try:
                res = whatsapp.send_template(
                    phone, settings.wa_tpl_decision_with_report,
                    [name or "there", outcome_label, next_step],
                    language=settings.wa_lang,
                    document_url=pdf_info[0], document_filename=pdf_info[1],
                )
            except whatsapp.WhatsAppError as e:
                log.warning("WA with-report dispatch failed (%s): %s — falling back",
                            settings.wa_tpl_decision_with_report, e)
                res = {}
        if not res.get("messages"):
            res = _wa_send(phone, settings.wa_tpl_decision, [name or "there", outcome_label, next_step])
        return res

    for kind, phone, name in (
        ("patient", pat_phone, pat_name),
        ("agent", agent_phone, agent_name or "Agent"),
    ):
        res = _send_decision(phone, name)
        db.collection("notifications").add(
            {
                "case_id": case_id,
                "patient_id": patient_id,
                "recipient_role": kind,
                "patient_phone": phone,
                "kind": body.decision,
                "payload": body.model_dump(),
                "whatsapp_result": res,
                "report_url": pdf_info[0] if pdf_info else None,
                "created_at": _now(),
                "sent": bool(res.get("messages")),
            }
        )
    return {"ok": True, "status": new_status, "report_url": pdf_info[0] if pdf_info else None}
