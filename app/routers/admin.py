import random
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from ..core.firebase import get_auth, get_db
from ..core.security import ROLE_ADMIN, ROLE_AGENT, ROLE_MO, require_roles

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_roles(ROLE_ADMIN))],
)


@router.get("/users")
def list_users():
    db = get_db()
    return [d.to_dict() for d in db.collection("users").stream()]


@router.get("/metrics")
def metrics():
    db = get_db()
    cases = [d.to_dict() for d in db.collection("cases").stream()]
    total = len(cases) or 1
    by_outcome = {"rule_out": 0, "alternative_dx": 0, "escalate": 0, "pending": 0}
    referred = 0
    closed_remote = 0
    for c in cases:
        o = c.get("triage_outcome") or "pending"
        by_outcome[o] = by_outcome.get(o, 0) + 1
        if c.get("status") == "referred":
            referred += 1
        if c.get("status") == "closed_remote":
            closed_remote += 1

    patients = [d.to_dict() for d in db.collection("patients").stream()]
    by_phc: dict[str, int] = {}
    for p in patients:
        phc = (p.get("phc") or "").strip() or "Unknown"
        by_phc[phc] = by_phc.get(phc, 0) + 1

    return {
        "total_cases": len(cases),
        "by_triage_outcome": by_outcome,
        "referral_rate_pct": round(100 * referred / total, 1),
        "remote_closure_rate_pct": round(100 * closed_remote / total, 1),
        "by_phc": by_phc,
        "total_patients": len(patients),
    }


@router.get("/audit-sample")
def audit_sample(pct: float = 5.0):
    """Returns a random 5% (default) sample of rule-out cases for review."""
    db = get_db()
    ruled_out = [
        d.to_dict()
        for d in db.collection("cases")
        .where("triage_outcome", "==", "rule_out")
        .stream()
    ]
    k = max(1, int(len(ruled_out) * pct / 100))
    return random.sample(ruled_out, min(k, len(ruled_out)))


@router.get("/mos")
def list_mos():
    """List MOs so agents can pick one when scheduling."""
    db = get_db()
    return [
        d.to_dict()
        for d in db.collection("users").where("role", "==", "mo").stream()
    ]
