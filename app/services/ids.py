"""Human-readable sequential identifiers for cases and patients.

We generate codes like ``CASE-00001`` and ``PAT-00001`` and use them directly
as the Firestore document ID, so URLs, PDFs and every ``.document(id)`` lookup
show the readable code. Sequence numbers come from an atomic counter document
in the ``counters`` collection, incremented inside a Firestore transaction so
two concurrent enrolments can never collide on the same number.

Existing records created before this change keep their old random IDs; only
newly-created cases/patients get the new format.
"""
from __future__ import annotations

import re

from google.cloud import firestore

_CODE_RE = re.compile(r"^(CASE|PAT|APPT)-", re.IGNORECASE)

# entity key -> (counter doc name, code prefix, zero-pad width)
_SCHEMES: dict[str, tuple[str, str, int]] = {
    "cases": ("cases_seq", "CASE", 5),
    "patients": ("patients_seq", "PAT", 5),
}


def _next_sequence(db, counter_name: str) -> int:
    """Atomically increment and return the next value for a named counter."""
    ref = db.collection("counters").document(counter_name)

    @firestore.transactional
    def _txn(transaction) -> int:
        snap = ref.get(transaction=transaction)
        current = (snap.to_dict() or {}).get("value", 0) if snap.exists else 0
        nxt = current + 1
        transaction.set(ref, {"value": nxt}, merge=True)
        return nxt

    return _txn(db.transaction())


def generate_code(db, entity: str) -> str:
    """Return the next human-readable code for ``entity`` (e.g. 'CASE-00001').

    `entity` must be a key in :data:`_SCHEMES` ('cases' or 'patients').
    """
    try:
        counter_name, prefix, width = _SCHEMES[entity]
    except KeyError as e:  # pragma: no cover
        raise ValueError(f"Unknown entity for code generation: {entity}") from e
    n = _next_sequence(db, counter_name)
    return f"{prefix}-{n:0{width}d}"


def display_id(raw: str | None) -> str:
    """Short, human-friendly form of an id for labels and filenames.

    New codes (CASE-00001 / PAT-00001) are already short and are shown in full;
    legacy random Firestore ids are truncated to their first 8 characters.
    """
    if not raw:
        return "—"
    return raw if _CODE_RE.match(raw) else raw[:8]
