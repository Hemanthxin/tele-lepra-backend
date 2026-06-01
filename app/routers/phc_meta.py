"""PHC / supervisor / CHW master list — Firestore-backed.

The 7 PHCs and the per-PHC supervisor / CHW lists come from the SHAKTHI
field PDF. Admins can edit them via PUT; everyone else (agent, MO, patient)
can read them via GET. On first read the collection is seeded with the
PDF defaults so a fresh deployment has a working dropdown immediately.
"""
from fastapi import APIRouter, Depends

from ..core.firebase import get_db
from ..core.security import ROLE_ADMIN, require_roles
from ..models.schemas import PhcEntry, PhcMetaList

router = APIRouter(prefix="/phc-meta", tags=["phc-meta"])


# Default seed pulled from the SHAKTHI PDF.
_DEFAULT_SEED: list[PhcEntry] = [
    PhcEntry(
        name="Bakawand",
        supervisors=["Bakawand"],
        chws=[
            "01 Bakawand",
            "02 Koudawand Odiyapal",
            "03 Nalpawand",
            "04 Rajnagar",
            "05 Kosmi",
        ],
    ),
    PhcEntry(
        name="Karpawand",
        supervisors=["Karpawand"],
        chws=[
            "01 Karpawand",
            "02 Jaitgiri",
            "03 Paurbel",
            "04 Sanghkarmari",
            "05 Sonpur",
        ],
    ),
    PhcEntry(
        name="Kolawal",
        supervisors=["Kolawal"],
        chws=[
            "01 Kolawal",
            "02 Pathri",
            "03 Satosa",
            "04 Chiurgaon Dhanpur",
        ],
    ),
    PhcEntry(
        name="Mangnaar",
        supervisors=["Mangnaar"],
        chws=[
            "01 Mangnar Khotlapal",
            "02 Sanvra Belputi",
            "03 Kinjoli",
            "04 Mooli",
            "05 Borigaon",
        ],
    ),
    PhcEntry(
        name="Kachnaar",
        supervisors=["Kachnaar"],
        chws=[
            "01 Kachnar Mongrapal",
            "02 Pandanar",
            "03 Bade Umargaon",
            "04 Chote Devda",
            "05 Sargipal",
        ],
    ),
    PhcEntry(
        name="Maalgaon",
        supervisors=["Maalgaon"],
        chws=[
            "01 Maalgaon",
            "02 Chitalur",
            "03 Kohkapal",
            "04 Tarapur Talnar",
            "05 Ulnar",
            "06 Bajawand",
        ],
    ),
    PhcEntry(
        name="Jebel",
        supervisors=["Jebel"],
        chws=[
            "01 Jaibel",
            "02 Dimrapal",
            "03 Garenga",
            "04 Chindgaon",
            "05 Mohlai Chargaon",
        ],
    ),
]

_DOC_ID = "default"


def _doc_ref(db):
    return db.collection("phc_meta").document(_DOC_ID)


def _seed_if_empty(db) -> PhcMetaList:
    ref = _doc_ref(db)
    snap = ref.get()
    if snap.exists:
        data = snap.to_dict() or {}
        items = data.get("items") or []
        if items:
            return PhcMetaList(items=[PhcEntry(**i) for i in items])
    payload = PhcMetaList(items=_DEFAULT_SEED)
    ref.set(payload.model_dump())
    return payload


@router.get("", response_model=PhcMetaList)
def get_phc_meta():
    """All authenticated roles can read the PHC list."""
    db = get_db()
    return _seed_if_empty(db)


@router.put(
    "",
    response_model=PhcMetaList,
    dependencies=[Depends(require_roles(ROLE_ADMIN))],
)
def replace_phc_meta(body: PhcMetaList):
    """Admin-only: replace the entire PHC list."""
    db = get_db()
    _doc_ref(db).set(body.model_dump())
    return body
