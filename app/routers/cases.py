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
    AgentDecision,
    CaseCreate,
    CaseStatus,
    HistoryEntry,
    MOClinicalAssessment,
    MODecision,
    Screening,
    SUSPECT_DISEASE_LABELS,
)
from ..services import whatsapp
from ..services.ids import generate_code
from ..services.rule_engine import inferred_conditions, triage
from .reports import build_and_upload_decision_pdf

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
    """Store the multi-disease screening and compute an advisory triage result.

    Side-effects (recall scheduling, patient WhatsApp) are NOT fired here — they
    happen when the agent confirms the decision via /agent-decision. The one
    automatic path is high leprosy probability (triage.allow_close == False),
    where the case goes straight to the MO queue.
    """
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    result = triage(body)

    # Forced MO (high leprosy) -> awaiting_mo. Otherwise park in `triaged` and
    # wait for the agent's Send-to-MO / Close decision.
    new_status = (
        CaseStatus.awaiting_mo.value
        if not result.allow_close
        else CaseStatus.triaged.value
    )

    # The engine infers the candidate conditions — the agent does not pick them.
    suspected = inferred_conditions(result)
    screening_doc = body.model_dump()
    screening_doc["suspected_diseases"] = suspected
    update = {
        "screening": screening_doc,
        "suspected_diseases": suspected,
        "triage": result.model_dump(),
        "triage_outcome": result.outcome.value,
        "allow_close": result.allow_close,
        "status": new_status,
        "updated_at": _now(),
    }
    # Keep the case's headline condition aligned with the leading suspicion.
    if result.suspected_condition and result.suspected_condition != "none":
        update["condition"] = result.suspected_condition
    snap.reference.set(update, merge=True)

    return result.model_dump()


@router.post(
    "/{case_id}/agent-decision",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_ADMIN))],
)
def agent_decision(case_id: str, body: AgentDecision):
    """Agent's advisory decision after triage: send to MO or close at community.

    Closing is rejected if triage marked the case as forced-MO (allow_close
    False) — that case must go to the Medical Officer.
    """
    db = get_db()
    snap = _get_case_or_404(db, case_id)
    case_data = snap.to_dict() or {}

    if body.action == "send_mo":
        snap.reference.set(
            {"status": CaseStatus.awaiting_mo.value, "agent_decision": "send_mo",
             "agent_decision_note": body.note, "updated_at": _now()},
            merge=True,
        )
        return {"ok": True, "status": CaseStatus.awaiting_mo.value}

    if body.action != "close":
        raise HTTPException(400, "action must be 'send_mo' or 'close'")

    if case_data.get("allow_close") is False:
        raise HTTPException(
            409, "This case has a high leprosy probability and must be sent to the Medical Officer."
        )

    # Close at community level. A non-leprosy chosen condition => alt-dx close;
    # otherwise a plain rule-out close.
    chosen = body.chosen_condition.value if body.chosen_condition else None
    is_alt_dx = bool(chosen and chosen != "leprosy")
    new_status = (
        CaseStatus.closed_alt_dx.value if is_alt_dx else CaseStatus.closed_rule_out.value
    )
    snap.reference.set(
        {
            "status": new_status,
            "agent_decision": "close",
            "agent_decision_note": body.note,
            "closed_condition": chosen,
            "closed_at": _now(),
            "updated_at": _now(),
        },
        merge=True,
    )

    # Schedule a recall and notify the patient.
    patient_id = case_data.get("patient_id")
    if patient_id:
        db.collection("recalls").add(
            {
                "case_id": case_id,
                "patient_id": patient_id,
                "due_at": _now().replace(microsecond=0),
                "status": "pending",
                "weeks_offset": 2 if is_alt_dx else 4,
            }
        )
        phone, name = _patient_phone_name(db, patient_id)
        if is_alt_dx:
            cond_label = SUSPECT_DISEASE_LABELS.get(chosen, (chosen or "").replace("_", " ").title())
            outcome_label = f"Reviewed — {cond_label} suspected, treated at community level"
            next_step = body.note or "Please follow the advice given. Recall in 2 weeks."
        else:
            outcome_label = "No urgent signs detected"
            next_step = body.note or "Follow-up review in 4-6 weeks. Stay safe."
        wa_result = _wa_send(phone, settings.wa_tpl_ruleout, [name, outcome_label, next_step])
        db.collection("notifications").add(
            {
                "case_id": case_id,
                "patient_id": patient_id,
                "patient_phone": phone,
                "kind": "agent_close",
                "payload": {"outcome": outcome_label, "next_step": next_step, "condition": chosen},
                "whatsapp_result": wa_result,
                "created_at": _now(),
                "sent": bool(wa_result.get("messages")),
            }
        )

    return {"ok": True, "status": new_status}


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
    pat_doc = db.collection("patients").document(data["patient_id"]).get()
    pat = pat_doc.to_dict() if pat_doc.exists else {}
    if user.role == ROLE_PATIENT:
        if pat.get("patient_uid") != user.uid:
            raise HTTPException(403, "Forbidden")
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

    # WhatsApp the decision to the patient.
    patient_id = case_data.get("patient_id")
    phone, name = _patient_phone_name(db, patient_id) if patient_id else (None, "Patient")

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
    # fields onto the pre-update snapshot) so the patient's copy reflects it.
    # This is done independently of WhatsApp: the decision and its PDF are
    # persisted on save, whether or not the message is ever sent.
    pdf_info = build_and_upload_decision_pdf({**case_data, **update, "id": case_id})
    if pdf_info:
        snap.reference.set(
            {"report_url": pdf_info[0], "report_filename": pdf_info[1], "report_generated_at": now},
            merge=True,
        )
    wa_result: dict = {}
    if phone and pdf_info:
        pdf_url, pdf_filename = pdf_info
        try:
            wa_result = whatsapp.send_template(
                phone,
                settings.wa_tpl_decision_with_report,
                [name, outcome_label, next_step],
                language=settings.wa_lang,
                document_url=pdf_url,
                document_filename=pdf_filename,
            )
        except whatsapp.WhatsAppError as e:
            log.warning("WA with-report dispatch failed (%s): %s — falling back to text template",
                        settings.wa_tpl_decision_with_report, e)
            wa_result = {}  # trigger fallback
    if not wa_result.get("messages"):
        # Plain-text fallback (or no PDF available).
        wa_result = _wa_send(phone, settings.wa_tpl_decision, [name, outcome_label, next_step])

    db.collection("notifications").add(
        {
            "case_id": case_id,
            "patient_id": patient_id,
            "patient_phone": phone,
            "kind": body.decision,
            "payload": body.model_dump(),
            "whatsapp_result": wa_result,
            "report_url": pdf_info[0] if pdf_info else None,
            "created_at": _now(),
            "sent": bool(wa_result.get("messages")),
        }
    )
    return {"ok": True, "status": new_status, "report_url": pdf_info[0] if pdf_info else None}
