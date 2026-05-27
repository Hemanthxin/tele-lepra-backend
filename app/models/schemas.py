from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


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


# ---------- Patient ----------
class PatientCreate(BaseModel):
    name: str
    age: int = Field(ge=0, le=120)
    sex: Sex
    phone: str
    location: str
    state: Optional[str] = None
    district: Optional[str] = None
    village: Optional[str] = None
    abha_id: Optional[str] = None
    consent_given: bool = True


class Patient(PatientCreate):
    id: str
    created_at: datetime
    created_by: str  # agent uid
    patient_uid: Optional[str] = None  # firebase auth uid if patient has login


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


# ---------- Screening (leprosy) ----------
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
    family_history: bool = False
    image_urls: List[str] = []
    notes: Optional[str] = None


# ---------- Rule engine result ----------
class TriageResult(BaseModel):
    outcome: TriageOutcome
    confidence: float = Field(ge=0.0, le=1.0)
    suspected_condition: str
    reasons: List[str]
    suggested_action: str
    alternative_dx_hint: Optional[str] = None


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


# ---------- User profile (for admin) ----------
class UserProfile(BaseModel):
    uid: str
    email: Optional[str]
    name: Optional[str]
    role: str
    created_at: Optional[datetime] = None
