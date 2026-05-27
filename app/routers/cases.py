import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from google.cloud.firestore import Query

from ..core.config import settings
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
from ..models.schemas import (
    CaseCreate,
    CaseStatus,
    HistoryEntry,
    LeprosyScreening,
    MODecision,
)
from ..services import whatsapp
from ..services.rule_engine import triage_leprosy

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
    ref = db.collection("cases").document()
    data = {
        "id": ref.id,
        "patient_id": body.patient_id,
        "patient_name": pdata["name"],
        "condition": body.condition,
        "status": CaseStatus.intake.value,
        "created_at": _now(),
        "updated_at": _now(),
        "created_by": user.uid,
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
def submit_screening(case_id: str, body: LeprosyScreening):
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    triage = triage_leprosy(body)

    status_map = {
        "rule_out": CaseStatus.closed_rule_out.value,
        "alternative_dx": CaseStatus.closed_alt_dx.value,
        "escalate": CaseStatus.awaiting_mo.value,
    }
    new_status = status_map[triage.outcome.value]

    snap.reference.set(
        {
            "screening": body.model_dump(),
            "triage": triage.model_dump(),
            "triage_outcome": triage.outcome.value,
            "status": new_status,
            "updated_at": _now(),
        },
        merge=True,
    )

    case_data = snap.to_dict()
    patient_id = case_data.get("patient_id")

    # Recall scheduling: rule-outs get a 4-6 week follow-up reminder.
    if triage.outcome.value == "rule_out":
        db.collection("recalls").add(
            {
                "case_id": case_id,
                "patient_id": patient_id,
                "due_at": _now().replace(microsecond=0),
                "status": "pending",
                "weeks_offset": 4,
            }
        )

    # WhatsApp notification for terminal outcomes. Escalations are messaged
    # later when the MO schedules the tele-consult (handled in appointments).
    if triage.outcome.value in ("rule_out", "alternative_dx") and patient_id:
        phone, name = _patient_phone_name(db, patient_id)
        outcome_label = {
            "rule_out": "No signs of leprosy detected",
            "alternative_dx": "Alternative diagnosis suspected",
        }[triage.outcome.value]
        next_step = {
            "rule_out": "Follow-up review in 4-6 weeks. Stay safe.",
            "alternative_dx": "Please follow the agent's prescription. Recall in 2 weeks.",
        }[triage.outcome.value]
        wa_result = _wa_send(phone, settings.wa_tpl_ruleout, [name, outcome_label, next_step])
        db.collection("notifications").add(
            {
                "case_id": case_id,
                "patient_id": patient_id,
                "patient_phone": phone,
                "kind": triage.outcome.value,
                "payload": {"outcome": outcome_label, "next_step": next_step},
                "whatsapp_result": wa_result,
                "created_at": _now(),
                "sent": bool(wa_result.get("messages")),
            }
        )

    return triage.model_dump()


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
    Patient: returns own cases.
    Agent: returns cases the agent created.
    MO: returns cases assigned to the MO.
    """
    db = get_db()
    if user.role == ROLE_PATIENT:
        pat = list(
            db.collection("patients")
            .where("patient_uid", "==", user.uid)
            .limit(1)
            .stream()
        )
        if not pat:
            return []
        pid = pat[0].id
        docs = db.collection("cases").where("patient_id", "==", pid).stream()
    elif user.role == ROLE_AGENT:
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
    if user.role == ROLE_PATIENT:
        pat = db.collection("patients").document(data["patient_id"]).get()
        if not pat.exists or pat.to_dict().get("patient_uid") != user.uid:
            raise HTTPException(403, "Forbidden")
    return data


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
    if body.decision not in ("close_remote", "refer"):
        raise HTTPException(400, "decision must be close_remote or refer")
    new_status = (
        CaseStatus.closed_remote.value
        if body.decision == "close_remote"
        else CaseStatus.referred.value
    )
    snap.reference.set(
        {
            "status": new_status,
            "mo_notes": body.notes,
            "prescription": body.prescription,
            "referral_note": body.referral_note,
            "closed_by": user.uid,
            "closed_at": _now(),
            "updated_at": _now(),
        },
        merge=True,
    )

    # WhatsApp the decision to the patient.
    case_data = snap.to_dict()
    patient_id = case_data.get("patient_id")
    phone, name = _patient_phone_name(db, patient_id) if patient_id else (None, "Patient")

    if body.decision == "close_remote":
        outcome_label = "Reviewed — closed at community level"
        next_step = body.prescription or "Follow the prescription provided. Reach out if symptoms worsen."
    else:
        outcome_label = "Referred for further care"
        next_step = body.referral_note or "Please visit the referral centre as advised."

    wa_result = _wa_send(phone, settings.wa_tpl_decision, [name, outcome_label, next_step])

    db.collection("notifications").add(
        {
            "case_id": case_id,
            "patient_id": patient_id,
            "patient_phone": phone,
            "kind": body.decision,
            "payload": body.model_dump(),
            "whatsapp_result": wa_result,
            "created_at": _now(),
            "sent": bool(wa_result.get("messages")),
        }
    )
    return {"ok": True, "status": new_status}
