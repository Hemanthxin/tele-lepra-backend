"""
Deterministic leprosy triage rule engine.

Outputs one of three outcomes:
  - rule_out         : community-level close, recall in 4-6 weeks
  - alternative_dx   : likely scabies/fungal/eczema, no MO needed
  - escalate         : queue for MO review

The thresholds below are aligned with the PDF spec (hypopigmented patches
with loss of sensation, nerve enlargement, glove-stocking anesthesia as
cardinal signs). They are intentionally conservative -- when in doubt the
engine escalates rather than rules out, so the missed-case rate stays low.
"""
from ..models.schemas import LeprosyScreening, TriageOutcome, TriageResult


CARDINAL_SIGNS_HINT = (
    "Cardinal signs of leprosy: (1) hypopigmented or reddish patches with "
    "definite loss of sensation, (2) thickened peripheral nerves, "
    "(3) acid-fast bacilli on skin smear."
)


def triage_leprosy(s: LeprosyScreening) -> TriageResult:
    reasons: list[str] = []
    cardinal_score = 0

    if s.has_skin_patches and s.patch_loss_of_sensation:
        cardinal_score += 2
        reasons.append("Skin patch(es) with definite loss of sensation.")
    elif s.has_skin_patches:
        cardinal_score += 1
        reasons.append("Skin patch(es) present, sensation status uncertain.")

    if s.enlarged_nerves:
        cardinal_score += 2
        reasons.append("Thickened/enlarged peripheral nerves reported.")

    if s.glove_stocking_anesthesia:
        cardinal_score += 2
        reasons.append("Glove-and-stocking anesthesia pattern.")

    if s.weakness_in_hands_or_feet:
        cardinal_score += 1
        reasons.append("Weakness in hands or feet.")

    if s.family_history:
        cardinal_score += 1
        reasons.append("Positive family history of leprosy.")

    if s.duration_weeks >= 4:
        reasons.append(f"Chronic duration ({s.duration_weeks} weeks).")

    # ---------- Decision ----------
    # >=3 cardinal points OR any high-specificity sign -> escalate
    high_specificity = (
        (s.has_skin_patches and s.patch_loss_of_sensation)
        or s.glove_stocking_anesthesia
        or s.enlarged_nerves
    )

    if high_specificity or cardinal_score >= 3:
        return TriageResult(
            outcome=TriageOutcome.escalate,
            confidence=min(0.5 + 0.15 * cardinal_score, 0.95),
            suspected_condition="leprosy",
            reasons=reasons or ["High-specificity sign present."],
            suggested_action=(
                "Queue for Medical Officer review. Schedule tele-consult "
                "within 24h. Do not delay if reactional state suspected."
            ),
        )

    # Patch without sensation loss + short duration -> likely alt dx
    if s.has_skin_patches and not s.patch_loss_of_sensation and s.duration_weeks < 4:
        alt = _guess_alternative_dx(s)
        return TriageResult(
            outcome=TriageOutcome.alternative_dx,
            confidence=0.7,
            suspected_condition="non-leprosy dermatosis",
            reasons=reasons
            or ["Skin lesion present without sensory loss, short duration."],
            suggested_action=(
                f"Treat as {alt}. Topical regimen as per protocol. "
                "Recall in 2 weeks; escalate if no improvement."
            ),
            alternative_dx_hint=alt,
        )

    # No cardinal signs at all -> rule out
    if cardinal_score == 0:
        return TriageResult(
            outcome=TriageOutcome.rule_out,
            confidence=0.85,
            suspected_condition="none",
            reasons=["No leprosy cardinal signs detected."],
            suggested_action=(
                "Reassure patient. Home care advice. Schedule recall in 4-6 "
                "weeks. Auto-escalate if new symptoms appear."
            ),
        )

    # Borderline: 1-2 weak points -> escalate to be safe
    return TriageResult(
        outcome=TriageOutcome.escalate,
        confidence=0.55,
        suspected_condition="leprosy (uncertain)",
        reasons=reasons + ["Findings ambiguous; safer to review."],
        suggested_action="Request MO review with additional photos.",
    )


def _guess_alternative_dx(s: LeprosyScreening) -> str:
    notes = (s.notes or "").lower()
    if "itch" in notes or "night" in notes:
        return "scabies"
    if "ring" in notes or "scaly" in notes or "fungal" in notes:
        return "fungal infection (tinea)"
    if "dry" in notes or "eczema" in notes or "atopic" in notes:
        return "eczema"
    return "non-specific dermatosis"
