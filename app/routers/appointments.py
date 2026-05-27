import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..core.config import settings
from ..core.firebase import get_db
from ..core.security import (
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_MO,
    CurrentUser,
    get_current_user,
    require_roles,
)
from ..models.schemas import AppointmentCreate, CaseStatus
from ..services import whatsapp
from ..services.zoom import create_meeting_stub, generate_signature

log = logging.getLogger(__name__)

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _now():
    return datetime.now(timezone.utc)


@router.post(
    "",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_MO, ROLE_ADMIN))],
)
def schedule_appointment(
    body: AppointmentCreate, user: CurrentUser = Depends(get_current_user)
):
    db = get_db()
    case_snap = db.collection("cases").document(body.case_id).get()
    if not case_snap.exists:
        raise HTTPException(404, "Case not found")
    case = case_snap.to_dict()

    meeting = create_meeting_stub(body.case_id)
    ref = db.collection("appointments").document()
    appt = {
        "id": ref.id,
        "case_id": body.case_id,
        "patient_id": case["patient_id"],
        "patient_name": case["patient_name"],
        "mo_uid": body.mo_uid,
        "scheduled_at": body.scheduled_at,
        "duration_minutes": body.duration_minutes,
        "status": "scheduled",
        "zoom_meeting_id": meeting["meeting_number"],
        "zoom_join_url": meeting["join_url"],
        "created_at": _now(),
        "created_by": user.uid,
    }
    ref.set(appt)
    case_snap.reference.set(
        {
            "status": CaseStatus.scheduled.value,
            "assigned_mo_uid": body.mo_uid,
            "scheduled_at": body.scheduled_at,
            "zoom_meeting_id": meeting["meeting_number"],
            "zoom_join_url": meeting["join_url"],
            "updated_at": _now(),
        },
        merge=True,
    )
    pat_doc = db.collection("patients").document(case["patient_id"]).get()
    pat_data = pat_doc.to_dict() if pat_doc.exists else {}
    pat_phone = pat_data.get("phone")
    pat_name = pat_data.get("name") or case.get("patient_name") or "Patient"

    scheduled_str = (
        body.scheduled_at.isoformat()
        if hasattr(body.scheduled_at, "isoformat")
        else str(body.scheduled_at)
    )

    # Send WhatsApp template with the join link. The patient taps the link
    # and joins the tele-consult; WhatsApp itself does not host video — the
    # video runs in the existing Zoom flow.
    wa_result = {"skipped": True, "reason": "no_phone"}
    if pat_phone:
        try:
            wa_result = whatsapp.send_template(
                pat_phone,
                settings.wa_tpl_appointment,
                [pat_name, scheduled_str, meeting["join_url"]],
                language=settings.wa_lang,
            )
        except whatsapp.WhatsAppError as e:
            log.warning("WA dispatch failed for appt %s: %s", ref.id, e)
            wa_result = {"error": str(e)}

    db.collection("notifications").add(
        {
            "case_id": body.case_id,
            "patient_id": case["patient_id"],
            "patient_phone": pat_phone,
            "kind": "appointment_scheduled",
            "payload": {
                "scheduled_at": scheduled_str,
                "zoom_join_url": meeting["join_url"],
                "zoom_meeting_id": meeting["meeting_number"],
                "duration_minutes": body.duration_minutes,
            },
            "whatsapp_result": wa_result,
            "created_at": _now(),
            "sent": bool(wa_result.get("messages")),
        }
    )
    return appt


@router.get("/mine")
def my_appointments(user: CurrentUser = Depends(get_current_user)):
    db = get_db()
    if user.role == "mo":
        docs = db.collection("appointments").where("mo_uid", "==", user.uid).stream()
    elif user.role == "patient":
        pat = list(
            db.collection("patients")
            .where("patient_uid", "==", user.uid)
            .limit(1)
            .stream()
        )
        if not pat:
            return []
        docs = (
            db.collection("appointments")
            .where("patient_id", "==", pat[0].id)
            .stream()
        )
    else:
        docs = db.collection("appointments").limit(50).stream()
    return sorted(
        [d.to_dict() for d in docs],
        key=lambda x: x.get("scheduled_at") or _now(),
    )


@router.get("/{appointment_id}/zoom-signature")
def zoom_signature(
    appointment_id: str, user: CurrentUser = Depends(get_current_user)
):
    """
    Returns a signed Zoom Meeting SDK signature so the front-end can join
    the meeting without ever holding the SDK secret.
    """
    db = get_db()
    snap = db.collection("appointments").document(appointment_id).get()
    if not snap.exists:
        raise HTTPException(404, "Appointment not found")
    appt = snap.to_dict()
    role = 1 if user.role == "mo" and appt["mo_uid"] == user.uid else 0
    sig = generate_signature(appt["zoom_meeting_id"], role)
    return {
        "signature": sig,
        "meeting_number": appt["zoom_meeting_id"],
        "role": role,
        "join_url": appt["zoom_join_url"],
    }
