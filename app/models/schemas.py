import re
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


_AADHAAR_RE = re.compile(r"^\d{12}$")
_ABHA_RE = re.compile(r"^\d{14}$")  # numeric ABHA / health-id number
_PHONE_RE = re.compile(r"^\+?[\d\s-]{7,15}$")


def _normalize_digits(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    return re.sub(r"[\s-]", "", v)


class Sex(str, Enum):
    male = "male"
    female = "female"
    other = "other"


class TriageOutcome(str, Enum):
    rule_out = "rule_out"
    alternative_dx = "alternative_dx"
    escalate = "escalate"


class CaseStatus(str, Enum):
    intake = "intake"
    triaged = "triaged"
    awaiting_mo = "awaiting_mo"
    scheduled = "scheduled"
    in_consult = "in_consult"
    closed_remote = "closed_remote"
    referred = "referred"
    closed_alt_dx = "closed_alt_dx"
    closed_rule_out = "closed_rule_out"


# ---------- Household / programme context enums ----------
class RelationToHead(str, Enum):
    self_ = "self"
    father_mother = "father_mother"
    husband_wife = "husband_wife"
    brother_sister = "brother_sister"
    son_daughter = "son_daughter"
    grand_son_grand_daughter = "grand_son_grand_daughter"
    others = "others"


# ---------- Patient ----------
class PatientCreate(BaseModel):
    name: str
    age: int = Field(ge=0, le=120)
    sex: Sex
    phone: str = Field(min_length=7, description="Phone number is required")
    location: str
    state: Optional[str] = None
    district: Optional[str] = None
    village: Optional[str] = None
    aadhaar_id: Optional[str] = Field(default=None, description="12-digit Aadhaar number")
    abha_id: Optional[str] = Field(default=None, description="14-digit ABHA health ID")
    referred_by: Optional[str] = None
    consent_given: bool = True

    # Programme context (new — PHC hierarchy)
    phc: Optional[str] = None
    supervisor: Optional[str] = None
    chw: Optional[str] = None

    # Address detail (new)
    house_no: Optional[str] = None
    gram_panchayat: Optional[str] = None

    # Household (new)
    household_number: Optional[str] = None
    head_of_family_name: Optional[str] = None
    head_of_family_phone: Optional[str] = None
    relation_to_head: Optional[RelationToHead] = None

    @field_validator("phone")
    @classmethod
    def _phone_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Phone number is required")
        if not _PHONE_RE.match(v):
            raise ValueError("Phone number looks invalid")
        return v

    @field_validator("head_of_family_phone", mode="before")
    @classmethod
    def _validate_hof_phone(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        v = v.strip()
        if not _PHONE_RE.match(v):
            raise ValueError("Head-of-family phone looks invalid")
        return v

    @field_validator("aadhaar_id", mode="before")
    @classmethod
    def _validate_aadhaar(cls, v):
        v = _normalize_digits(v)
        if v is None:
            return None
        if not _AADHAAR_RE.match(v):
            raise ValueError("Aadhaar must be exactly 12 digits")
        return v

    @field_validator("abha_id", mode="before")
    @classmethod
    def _validate_abha(cls, v):
        v = _normalize_digits(v)
        if v is None:
            return None
        if not _ABHA_RE.match(v):
            raise ValueError("ABHA ID must be exactly 14 digits")
        return v


class Patient(PatientCreate):
    id: str
    created_at: datetime
    created_by: str  # agent uid
    patient_uid: Optional[str] = None  # firebase auth uid if patient has login
    synthetic_email: Optional[str] = None  # internal email for Firebase auth lookup


class SymptomsSelfReport(BaseModel):
    has_skin_patches: bool = False
    patch_count: int = 0
    duration_weeks: int = 0
    numb_or_tingling_in_hands_or_feet: bool = False
    weakness_in_hands_or_feet: bool = False
    family_history: bool = False
    image_urls: List[str] = []
    notes: Optional[str] = None


class SelfEnrollment(PatientCreate):
    chronic_conditions: List[str] = []
    symptoms: Optional[SymptomsSelfReport] = None


# ---------- History ----------
class HistoryEntry(BaseModel):
    chronic_conditions: List[str] = []
    prior_prescriptions_urls: List[str] = []
    prior_labs_urls: List[str] = []
    past_visits_notes: Optional[str] = None


# ---------- Geolocation ----------
class GeoPoint(BaseModel):
    lat: float
    lng: float
    altitude: Optional[float] = None
    accuracy: Optional[float] = None
    captured_at: Optional[datetime] = None


# ---------- Screening (leprosy) ----------
# Canonical 11-symptom checklist keys, drawn from PDF1.
LEPROSY_SYMPTOM_KEYS = [
    "skin_patches",
    "patch_loss_of_sensation",
    "numb_tingling_burning",
    "weakness_in_hands_or_feet",
    "weak_grip",
    "painless_wounds",
    "nerve_tenderness",
    "foot_drop",
    "eye_closure_difficulty",
    "eyebrow_loss_nasal_collapse",
    "nodules_or_earlobe_swelling",
]


class LeprosyScreening(BaseModel):
    # Lesions
    has_skin_patches: bool
    patch_count: int = 0
    patch_loss_of_sensation: bool
    # Nerve signs
    enlarged_nerves: bool
    weakness_in_hands_or_feet: bool
    # Sensory testing
    glove_stocking_anesthesia: bool
    # Other
    duration_weeks: int = 0
    duration_months: int = 0
    family_history: bool = False
    image_urls: List[str] = []
    lab_urls: List[str] = []
    notes: Optional[str] = None

    # PDF1 11-symptom canonical checklist (additive — does not replace Y/N rows).
    symptoms_checklist: List[str] = []

    # Screening event context (PDF2)
    screened_at: Optional[datetime] = None
    geolocation: Optional[GeoPoint] = None


# ---------- Multi-disease screening (SHAKTHI Active Screening form) ----------
class SuspectDisease(str, Enum):
    leprosy = "leprosy"
    lymphatic_filariasis = "lymphatic_filariasis"
    tuberculosis = "tuberculosis"
    scabies = "scabies"
    japanese_encephalitis = "japanese_encephalitis"
    malaria = "malaria"
    sickle_cell = "sickle_cell"


# Human-readable labels (shared by UI/PDF via the API if needed).
SUSPECT_DISEASE_LABELS = {
    "leprosy": "Leprosy",
    "lymphatic_filariasis": "Lymphatic Filariasis",
    "tuberculosis": "Tuberculosis",
    "scabies": "Scabies",
    "japanese_encephalitis": "Japanese Encephalitis",
    "malaria": "Malaria",
    "sickle_cell": "Sickle Cell Disease",
}


# Canonical symptom-question keys. The agent answers the COMMON questions first;
# each "yes" reveals its FOLLOW-UP questions (the UI drives the branching). The
# rule engine maps the answered symptoms to candidate conditions — the agent
# never picks a disease; it is inferred.
SCREENING_COMMON_KEYS = [
    "skin_changes",
    "fever",
    "cough",
    "swelling",
    "numbness_or_weakness",
    "pain_or_fatigue",
]


class Screening(BaseModel):
    """Symptom-driven screening payload.

    A flat set of optional booleans: the six COMMON entry symptoms plus the
    follow-up symptoms that the UI reveals when a common answer is "yes".
    Unanswered questions stay None. The engine infers which of the seven
    conditions are likely; the agent does not select a disease.
    """
    # --- Common entry symptoms (always asked) ---
    skin_changes: Optional[bool] = None              # patch / rash / discoloured area
    fever: Optional[bool] = None
    cough: Optional[bool] = None
    swelling: Optional[bool] = None                  # limb / breast / genitals
    numbness_or_weakness: Optional[bool] = None      # hands / feet
    pain_or_fatigue: Optional[bool] = None           # recurrent pain / tiredness / jaundice

    # --- Skin follow-ups (leprosy / scabies) ---
    skin_loss_of_sensation: Optional[bool] = None
    skin_pale_or_reddish_patch: Optional[bool] = None
    skin_patch_count: int = 0
    skin_itchy_worse_at_night: Optional[bool] = None
    skin_household_others_affected: Optional[bool] = None
    skin_nodules_or_earlobe: Optional[bool] = None

    # --- Fever follow-ups (malaria / JE / TB) ---
    fever_chills_rigor: Optional[bool] = None
    fever_periodic: Optional[bool] = None
    fever_altered_consciousness: Optional[bool] = None
    fever_neck_stiff_or_headache: Optional[bool] = None
    fever_night_sweats: Optional[bool] = None

    # --- Cough follow-ups (TB) ---
    cough_2_weeks_or_more: Optional[bool] = None
    cough_blood_in_sputum: Optional[bool] = None
    cough_weight_loss: Optional[bool] = None

    # --- Swelling follow-ups (filariasis) ---
    swelling_limb_or_genitals: Optional[bool] = None
    swelling_acute_attacks: Optional[bool] = None

    # --- Numbness / weakness follow-ups (leprosy) ---
    glove_stocking_anesthesia: Optional[bool] = None
    enlarged_nerves: Optional[bool] = None
    eye_closure_or_foot_drop: Optional[bool] = None
    painless_wounds: Optional[bool] = None

    # --- Pain / fatigue follow-ups (sickle cell) ---
    recurrent_pain_episodes: Optional[bool] = None
    anaemia_or_fatigue: Optional[bool] = None
    jaundice: Optional[bool] = None
    family_history_sickle_cell: Optional[bool] = None

    # --- General context ---
    family_history_leprosy: Optional[bool] = None
    duration_months: int = 0

    # Canonical 11-symptom leprosy checklist (LEPROSY_SYMPTOM_KEYS). The agent
    # answers Yes/No for each; `symptoms_checklist` is the list of "yes" keys.
    symptoms: Dict[str, bool] = {}
    symptoms_checklist: List[str] = []

    # Inferred by the engine (echoed back on the stored case); never an input.
    suspected_diseases: List[SuspectDisease] = []

    # Screening context + attachments
    screened_at: Optional[datetime] = None
    geolocation: Optional[GeoPoint] = None
    image_urls: List[str] = []
    lab_urls: List[str] = []
    notes: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy(cls, data):
        """Back-compat: map the old flat LeprosyScreening keys (and queued
        offline bundles) onto the new symptom fields so they still validate."""
        if not isinstance(data, dict) or "skin_changes" in data:
            return data
        legacy_map = {
            "has_skin_patches": "skin_changes",
            "patch_loss_of_sensation": "skin_loss_of_sensation",
            "enlarged_nerves": "enlarged_nerves",
            "glove_stocking_anesthesia": "glove_stocking_anesthesia",
            "weakness_in_hands_or_feet": "numbness_or_weakness",
            "family_history": "family_history_leprosy",
            "patch_count": "skin_patch_count",
        }
        if not (legacy_map.keys() & data.keys()):
            return data
        out = {v: data[k] for k, v in legacy_map.items() if k in data}
        for shared in ("screened_at", "geolocation", "image_urls", "lab_urls", "notes", "duration_months"):
            if shared in data:
                out[shared] = data[shared]
        return out


# ---------- Rule engine result ----------
class RiskLevel(str, Enum):
    high = "high"
    moderate = "moderate"
    low = "low"


class ConditionFinding(BaseModel):
    """Per-disease triage finding within a multi-disease screening."""
    condition: SuspectDisease
    risk: RiskLevel
    score: int = 0
    reasons: List[str] = []


class TriageResult(BaseModel):
    outcome: TriageOutcome
    confidence: float = Field(ge=0.0, le=1.0)
    suspected_condition: str
    reasons: List[str]
    suggested_action: str
    alternative_dx_hint: Optional[str] = None

    # Multi-disease additions
    condition_findings: List[ConditionFinding] = []
    # When False, the agent may NOT close at community level (e.g. high leprosy
    # probability) — the only path is Send to MO. When True, the agent chooses
    # between Send to MO and Close.
    allow_close: bool = True
    recommendation: Optional[str] = None


# ---------- MO Clinical Assessment (PDF1 Teleconsultation block) ----------
class LesionCount(str, Enum):
    single = "single"
    two_to_ten = "two_to_ten"
    more_than_ten = "more_than_ten"
    pure_neuritic = "pure_neuritic"
    diffuse = "diffuse"


class CaseClinicalStatus(str, Enum):
    new_untreated = "new_untreated"
    continuation_mdt = "continuation_mdt"
    released = "released"
    defaulter = "defaulter"
    relapse = "relapse"


class WhoClassification(str, Enum):
    multibacillary = "multibacillary"
    paucibacillary = "paucibacillary"


class SensoryLossSite(str, Enum):
    eye = "eye"
    hands = "hands"
    feet = "feet"
    none = "none"


class Complication(str, Enum):
    type_1 = "type_1"
    type_2 = "type_2"
    neuritis = "neuritis"
    nfi = "nfi"
    ulcer = "ulcer"
    eye_involvement = "eye_involvement"
    none = "none"


class NerveName(str, Enum):
    radial = "radial"
    ulnar = "ulnar"
    median = "median"
    lateral_popliteal = "lateral_popliteal"
    posterior_tibial = "posterior_tibial"


class BodySide(str, Enum):
    right = "right"
    left = "left"


class NerveState(str, Enum):
    none = "none"
    tender = "tender"
    enlarged = "enlarged"
    not_examined = "not_examined"


class NerveFinding(BaseModel):
    nerve: NerveName
    side: BodySide
    state: NerveState


class MOClinicalAssessment(BaseModel):
    confirmed_leprosy: bool
    lesion_count: Optional[LesionCount] = None
    nerve_involvement: List[NerveFinding] = []
    clinical_status: Optional[CaseClinicalStatus] = None
    who_classification: Optional[WhoClassification] = None
    sensory_loss: List[SensoryLossSite] = []
    disability_grade: Optional[int] = Field(default=None, ge=0, le=2)
    complications: List[Complication] = []
    treatment_plan: Optional[str] = None


# ---------- Case ----------
class CaseCreate(BaseModel):
    patient_id: str
    condition: str = "leprosy"


class CaseSummary(BaseModel):
    id: str
    patient_id: str
    patient_name: str
    condition: str
    status: CaseStatus
    triage_outcome: Optional[TriageOutcome] = None
    created_at: datetime
    updated_at: datetime
    assigned_mo_uid: Optional[str] = None
    scheduled_at: Optional[datetime] = None


class Case(CaseSummary):
    history: Optional[HistoryEntry] = None
    screening: Optional[LeprosyScreening] = None
    triage: Optional[TriageResult] = None
    clinical_assessment: Optional[MOClinicalAssessment] = None
    mo_notes: Optional[str] = None
    prescription: Optional[str] = None
    referral_note: Optional[str] = None
    zoom_meeting_id: Optional[str] = None
    zoom_join_url: Optional[str] = None


# ---------- Appointment ----------
class AppointmentCreate(BaseModel):
    case_id: str
    mo_uid: str
    scheduled_at: datetime
    duration_minutes: int = 20


class Appointment(AppointmentCreate):
    id: str
    patient_id: str
    patient_name: str
    status: str = "scheduled"  # scheduled | completed | missed | cancelled
    zoom_meeting_id: Optional[str] = None
    zoom_join_url: Optional[str] = None
    created_at: datetime


# ---------- MO actions ----------
class MODecision(BaseModel):
    decision: str  # "close_remote" | "refer"
    prescription: Optional[str] = None
    referral_note: Optional[str] = None
    notes: Optional[str] = None


# ---------- PHC master list (Firestore-backed) ----------
class PhcEntry(BaseModel):
    name: str
    supervisors: List[str] = []
    chws: List[str] = []


class PhcMetaList(BaseModel):
    items: List[PhcEntry]


# ---------- User profile (for admin) ----------
class UserProfile(BaseModel):
    uid: str
    email: Optional[str]
    name: Optional[str]
    role: str
    created_at: Optional[datetime] = None
