"""PDF report endpoints.

Two streams:
  GET /cases/{id}/intake.pdf    — agent-collected data
  GET /cases/{id}/decision.pdf  — MO clinical assessment + final decision

Both authenticated. Allowed roles: MO + admin + agent who created the case +
patient who owns the case. The PDF is generated on demand (no caching) so it
always reflects the current Firestore state.

`upload_decision_pdf_for_whatsapp(case)` is a helper used by the decision
dispatch path: it generates the decision PDF, uploads it to Firebase Storage
with `make_public()`, and returns the public URL so the WhatsApp template's
document header can reference it.
"""
from __future__ import annotations

import logging
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Response

from ..core.firebase import get_db, upload_public
from ..core.security import (
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_MO,
    ROLE_PATIENT,
    CurrentUser,
    get_current_user,
)
from ..services.ids import display_id
from ..services.pdf_reports import build_decision_pdf, build_intake_pdf

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cases", tags=["reports"])


def _join_patient(db, case: dict) -> dict:
    """Flatten patient fields onto the case dict so the PDF builders have
    every demographic / programme / household field by name."""
    pat_doc = db.collection("patients").document(case["patient_id"]).get()
    if not pat_doc.exists:
        return case
    pat = pat_doc.to_dict() or {}
    for key, val in pat.items():
        if key in ("id", "patient_uid", "synthetic_email", "created_at", "created_by"):
            continue
        target = f"patient_{key}" if key != "name" else "patient_name"
        case.setdefault(target, val)
    return case


def _load_case_or_403(case_id: str, user: CurrentUser) -> dict:
    db = get_db()
    snap = db.collection("cases").document(case_id).get()
    if not snap.exists:
        raise HTTPException(404, "Case not found")
    case = snap.to_dict()
    # Authorization
    if user.role in (ROLE_MO, ROLE_ADMIN):
        pass
    elif user.role == ROLE_AGENT:
        if case.get("created_by") != user.uid:
            raise HTTPException(403, "Forbidden")
    elif user.role == ROLE_PATIENT:
        pat_doc = db.collection("patients").document(case["patient_id"]).get()
        if not pat_doc.exists or pat_doc.to_dict().get("patient_uid") != user.uid:
            raise HTTPException(403, "Forbidden")
    else:
        raise HTTPException(403, "Forbidden")
    return _join_patient(db, case)


def _pdf_response(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/{case_id}/intake.pdf")
def get_intake_pdf(case_id: str, user: CurrentUser = Depends(get_current_user)):
    case = _load_case_or_403(case_id, user)
    pdf = build_intake_pdf(case)
    return _pdf_response(pdf, f"intake-{display_id(case_id)}.pdf")


@router.get("/{case_id}/decision.pdf")
def get_decision_pdf(case_id: str, user: CurrentUser = Depends(get_current_user)):
    case = _load_case_or_403(case_id, user)
    pdf = build_decision_pdf(case)
    return _pdf_response(pdf, f"decision-{display_id(case_id)}.pdf")


# ---------- helpers used by the WhatsApp dispatch path ----------
def _upload_pdf_public(content: bytes, key: str) -> str:
    """Upload a PDF to Firebase Storage and return an openable URL.

    Uses the shared helper, which falls back to a signed URL on buckets with
    uniform bucket-level access.
    """
    return upload_public(content, key, "application/pdf")


def build_and_upload_decision_pdf(case: dict) -> tuple[str, str] | None:
    """Generate the decision PDF for a case and upload it to Firebase Storage.

    Returns (public_url, filename) on success, None on any failure. Failures
    are logged but never raised — the caller (the decision route) must keep
    succeeding even if PDF upload fails.
    """
    case_id = case.get("id") or "unknown"
    short = display_id(case_id)
    try:
        db = get_db()
        case = _join_patient(db, case)
        pdf = build_decision_pdf(case)
    except Exception as e:  # pragma: no cover
        log.warning("Decision PDF build failed for %s: %s", case_id, e)
        return None
    try:
        url = _upload_pdf_public(pdf, f"reports/{case_id}/decision-{short}.pdf")
        return url, f"Lepra-decision-{short}.pdf"
    except Exception as e:  # pragma: no cover
        log.warning("Decision PDF upload failed for %s: %s", case_id, e)
        return None
