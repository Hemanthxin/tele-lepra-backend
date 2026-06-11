"""
Leprosy risk summary for the Medical Officer.

The field agent only collects data — they answer the canonical 11-symptom
leprosy checklist and submit the case to the MO. There is no agent-side
decision. This module turns those answers into a simple leprosy risk summary
(high / moderate / low) that is stored on the case for the MO's reference; it
does NOT route the case (every screened case goes to the MO).
"""
from ..models.schemas import (
    ConditionFinding,
    LeprosyScreening,
    RiskLevel,
    Screening,
    SuspectDisease,
    TriageOutcome,
    TriageResult,
)

# Weight of each leprosy checklist key toward the leprosy risk score.
_SYMPTOM_WEIGHTS = {
    "patch_loss_of_sensation": 3,   # cardinal sign
    "nerve_tenderness": 2,          # thickened / tender nerves
    "skin_patches": 1,
    "numb_tingling_burning": 1,
    "weakness_in_hands_or_feet": 1,
    "weak_grip": 1,
    "painless_wounds": 1,
    "foot_drop": 1,
    "eye_closure_difficulty": 1,
    "eyebrow_loss_nasal_collapse": 1,
    "nodules_or_earlobe_swelling": 1,
}

# Reasons surfaced for the MO when a key is positive.
_SYMPTOM_REASONS = {
    "patch_loss_of_sensation": "Loss of sensation over a skin patch (cardinal sign).",
    "nerve_tenderness": "Pain / tenderness over peripheral nerves.",
    "skin_patches": "Light-coloured or reddish skin patch(es).",
    "numb_tingling_burning": "Tingling / numbness / burning in hands or feet.",
    "weakness_in_hands_or_feet": "Weakness in hands or feet.",
    "weak_grip": "Weak grip / objects slipping from hands.",
    "painless_wounds": "Painless wounds, burns, or ulcers.",
    "foot_drop": "Foot drop / dragging while walking.",
    "eye_closure_difficulty": "Difficulty closing the eyes / reduced blinking.",
    "eyebrow_loss_nasal_collapse": "Loss of eyebrows / nasal collapse.",
    "nodules_or_earlobe_swelling": "Lumps/nodules or ear-lobe swelling.",
}

_CARDINAL = {"patch_loss_of_sensation"}


def _positive_keys(screening: Screening) -> list[str]:
    keys = set(screening.symptoms_checklist or [])
    for k, v in (screening.symptoms or {}).items():
        if v is True:
            keys.add(k)
    return [k for k in _SYMPTOM_WEIGHTS if k in keys]


def triage(screening: Screening) -> TriageResult:
    """Summarise leprosy risk from the 11-symptom checklist for the MO."""
    positive = _positive_keys(screening)
    score = sum(_SYMPTOM_WEIGHTS.get(k, 0) for k in positive)
    cardinal = any(k in _CARDINAL for k in positive)
    reasons = [_SYMPTOM_REASONS[k] for k in positive if k in _SYMPTOM_REASONS]

    if cardinal or score >= 5:
        risk = RiskLevel.high
    elif score >= 2:
        risk = RiskLevel.moderate
    elif score >= 1:
        risk = RiskLevel.low
    else:
        risk = RiskLevel.low

    if not positive:
        return TriageResult(
            outcome=TriageOutcome.rule_out,
            confidence=0.6,
            suspected_condition="none",
            reasons=["No leprosy symptoms reported."],
            suggested_action="No symptoms flagged. Sent to the Medical Officer for review.",
            condition_findings=[],
            allow_close=False,
            recommendation="Sent to Medical Officer for review.",
        )

    finding = ConditionFinding(
        condition=SuspectDisease.leprosy, risk=risk, score=score, reasons=reasons,
    )
    return TriageResult(
        outcome=TriageOutcome.escalate,
        confidence=min(0.5 + 0.07 * score, 0.95),
        suspected_condition="leprosy",
        reasons=reasons,
        suggested_action=(
            f"{risk.value.title()} leprosy risk from {len(positive)} reported symptom(s). "
            "Sent to the Medical Officer for review and decision."
        ),
        condition_findings=[finding],
        # The agent makes no decision — every case goes to the MO.
        allow_close=False,
        recommendation=f"{risk.value.title()} leprosy risk — Medical Officer to review.",
    )


def inferred_conditions(result: TriageResult) -> list[str]:
    """Condition keys in play (leprosy when any symptom is positive)."""
    return [f.condition.value for f in result.condition_findings]


# ---------- Backward-compatible single-disease entry point ----------
def triage_leprosy(s: LeprosyScreening) -> TriageResult:
    """Legacy shim: map a bare LeprosyScreening onto the checklist model."""
    checklist = list(s.symptoms_checklist or [])
    if s.has_skin_patches:
        checklist.append("skin_patches")
    if s.patch_loss_of_sensation:
        checklist.append("patch_loss_of_sensation")
    if s.enlarged_nerves:
        checklist.append("nerve_tenderness")
    if s.weakness_in_hands_or_feet:
        checklist.append("weakness_in_hands_or_feet")
    return triage(Screening(symptoms_checklist=list(set(checklist))))
