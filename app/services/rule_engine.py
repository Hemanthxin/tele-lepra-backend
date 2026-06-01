"""
Deterministic symptom-driven triage engine.

The agent answers a symptom questionnaire (common questions first, then
follow-ups). The engine maps the answered symptoms onto the seven programme
conditions, scores each, and *infers* which are likely — the agent never picks
a disease. The result is advisory: the agent normally chooses between "Send to
MO" and "Close at community level". The exception is a high-probability finding
for a forced condition (leprosy — the programme focus — or Japanese
encephalitis, an acute emergency): there `allow_close` is False and the only
safe path is Send to MO.

It is intentionally conservative: when in doubt it raises risk rather than
lowering it, so the missed-case rate stays low.
"""
from ..models.schemas import (
    ConditionFinding,
    LeprosyScreening,
    RiskLevel,
    Screening,
    SuspectDisease,
    SUSPECT_DISEASE_LABELS,
    TriageOutcome,
    TriageResult,
)

D = SuspectDisease

# Symptom field -> list of (condition, weight, human reason).
# NOTE: the agent's live "likely condition" preview duplicates these weights in
# frontend/src/pages/agent/steps/ScreenStep.jsx (WEIGHTS / CARDINAL) so it works
# offline. If you change weights or cardinals here, update that file to match —
# this module remains the source of truth at submit time.
_SYMPTOM_MAP: dict[str, list[tuple[SuspectDisease, int, str]]] = {
    # Skin
    "skin_loss_of_sensation": [(D.leprosy, 3, "Loss of sensation over a skin patch (cardinal leprosy sign).")],
    "skin_pale_or_reddish_patch": [(D.leprosy, 1, "Pale / reddish skin patch.")],
    "skin_nodules_or_earlobe": [(D.leprosy, 1, "Nodules / ear-lobe swelling.")],
    "skin_itchy_worse_at_night": [(D.scabies, 2, "Itching worse at night.")],
    "skin_household_others_affected": [(D.scabies, 2, "Other household members itching.")],
    # Numbness / weakness
    "glove_stocking_anesthesia": [(D.leprosy, 3, "Glove-and-stocking anaesthesia (cardinal leprosy sign).")],
    "enlarged_nerves": [(D.leprosy, 3, "Thickened / enlarged peripheral nerves (cardinal leprosy sign).")],
    "eye_closure_or_foot_drop": [(D.leprosy, 1, "Difficulty closing eyes / foot drop.")],
    "painless_wounds": [(D.leprosy, 1, "Painless wounds / burns on hands or feet.")],
    "numbness_or_weakness": [(D.leprosy, 1, "Numbness, tingling, or weakness in hands/feet.")],
    "family_history_leprosy": [(D.leprosy, 1, "Household contact / family history of leprosy.")],
    # Fever
    "fever_chills_rigor": [(D.malaria, 3, "Fever with chills and rigor.")],
    "fever_periodic": [(D.malaria, 2, "Periodic fever pattern.")],
    "fever_altered_consciousness": [(D.japanese_encephalitis, 3, "Fever with altered consciousness / fits (acute emergency).")],
    "fever_neck_stiff_or_headache": [(D.japanese_encephalitis, 2, "Fever with neck stiffness / severe headache.")],
    "fever_night_sweats": [(D.tuberculosis, 1, "Night sweats.")],
    # Cough
    "cough_2_weeks_or_more": [(D.tuberculosis, 3, "Cough for 2 weeks or more.")],
    "cough_blood_in_sputum": [(D.tuberculosis, 3, "Blood in sputum (haemoptysis).")],
    "cough_weight_loss": [(D.tuberculosis, 1, "Cough with weight loss.")],
    # Swelling
    "swelling_limb_or_genitals": [(D.lymphatic_filariasis, 3, "Persistent swelling of limb / genitals.")],
    "swelling_acute_attacks": [(D.lymphatic_filariasis, 2, "Recurrent acute swelling attacks.")],
    # Pain / fatigue
    "recurrent_pain_episodes": [(D.sickle_cell, 2, "Recurrent severe pain episodes.")],
    "anaemia_or_fatigue": [(D.sickle_cell, 1, "Anaemia / chronic fatigue.")],
    "jaundice": [(D.sickle_cell, 1, "Jaundice.")],
    "family_history_sickle_cell": [(D.sickle_cell, 1, "Family history of sickle cell disease.")],
}

# Cardinal symptoms that make a condition HIGH risk on their own.
_CARDINAL_HIGH = {
    D.leprosy: ["skin_loss_of_sensation", "glove_stocking_anesthesia", "enlarged_nerves"],
    D.japanese_encephalitis: ["fever_altered_consciousness"],
    D.tuberculosis: ["cough_2_weeks_or_more", "cough_blood_in_sputum"],
    D.malaria: ["fever_chills_rigor"],
    D.lymphatic_filariasis: ["swelling_limb_or_genitals"],
}

# Conditions where a HIGH finding forces MO (no community close).
_FORCED = {D.leprosy, D.japanese_encephalitis}

_RISK_RANK = {RiskLevel.high: 2, RiskLevel.moderate: 1, RiskLevel.low: 0}


def _label(condition) -> str:
    key = condition.value if hasattr(condition, "value") else str(condition)
    return SUSPECT_DISEASE_LABELS.get(key, key.replace("_", " ").title())


def _truthy(screening: Screening, key: str) -> bool:
    return getattr(screening, key, None) is True


def _infer(screening: Screening) -> list[ConditionFinding]:
    scores: dict[SuspectDisease, int] = {}
    reasons: dict[SuspectDisease, list[str]] = {}

    for key, contribs in _SYMPTOM_MAP.items():
        if not _truthy(screening, key):
            continue
        for condition, weight, reason in contribs:
            scores[condition] = scores.get(condition, 0) + weight
            reasons.setdefault(condition, []).append(reason)

    findings: list[ConditionFinding] = []
    for condition, score in scores.items():
        cardinal = any(_truthy(screening, k) for k in _CARDINAL_HIGH.get(condition, []))
        if cardinal or score >= 5:
            risk = RiskLevel.high
        elif score >= 3:
            risk = RiskLevel.high
        elif score >= 1:
            risk = RiskLevel.moderate
        else:
            risk = RiskLevel.low
        findings.append(ConditionFinding(
            condition=condition, risk=risk, score=score, reasons=reasons[condition],
        ))

    findings.sort(key=lambda f: (_RISK_RANK[f.risk], f.score), reverse=True)
    return findings


def triage(screening: Screening) -> TriageResult:
    """Infer likely conditions from the symptom answers and produce an advisory
    triage result with per-condition findings."""
    findings = _infer(screening)

    # ---------- Forced MO: a high-risk forced condition (leprosy / JE) ----------
    forced_high = [f for f in findings if f.risk == RiskLevel.high and f.condition in _FORCED]
    if forced_high:
        lead = forced_high[0]
        emergency = lead.condition == D.japanese_encephalitis
        return TriageResult(
            outcome=TriageOutcome.escalate,
            confidence=min(0.6 + 0.06 * lead.score, 0.95),
            suspected_condition=_label(lead.condition),
            reasons=lead.reasons,
            suggested_action=(
                f"High probability of {_label(lead.condition)}. "
                + ("This is an acute emergency — " if emergency else "")
                + "Send to the Medical Officer now; do not close at community level."
            ),
            condition_findings=findings,
            allow_close=False,
            recommendation=f"Send to Medical Officer (required) — likely {_label(lead.condition)}.",
        )

    high = [f for f in findings if f.risk == RiskLevel.high]
    moderate = [f for f in findings if f.risk == RiskLevel.moderate]

    # ---------- Any other high-risk condition: recommend MO, agent may choose ----------
    if high:
        lead = high[0]
        return TriageResult(
            outcome=TriageOutcome.escalate,
            confidence=0.7,
            suspected_condition=_label(lead.condition),
            reasons=lead.reasons,
            suggested_action=(
                f"Findings suggest {_label(lead.condition)}. Recommended: send to the "
                "Medical Officer. You may close at community level if you are confident "
                "this can be managed locally."
            ),
            condition_findings=findings,
            allow_close=True,
            recommendation=f"Likely {_label(lead.condition)} — recommend Send to MO.",
            alternative_dx_hint=_label(lead.condition) if lead.condition != D.leprosy else None,
        )

    # ---------- Moderate suspicion: agent's choice ----------
    if moderate:
        lead = moderate[0]
        return TriageResult(
            outcome=TriageOutcome.alternative_dx,
            confidence=0.55,
            suspected_condition=_label(lead.condition),
            reasons=lead.reasons,
            suggested_action=(
                f"Possible {_label(lead.condition)}. You may treat / observe at community "
                "level, or send to the Medical Officer if unsure."
            ),
            condition_findings=findings,
            allow_close=True,
            recommendation=f"Possibly {_label(lead.condition)} — your decision: Send to MO or Close.",
            alternative_dx_hint=_label(lead.condition) if lead.condition != D.leprosy else None,
        )

    # ---------- Nothing significant ----------
    return TriageResult(
        outcome=TriageOutcome.rule_out,
        confidence=0.85,
        suspected_condition="none",
        reasons=["No significant findings across the screened symptoms."],
        suggested_action=(
            "No red flags detected. You may close at community level with home-care advice "
            "and a 4-6 week recall, or send to MO if you have concerns."
        ),
        condition_findings=findings,
        allow_close=True,
        recommendation="No red flags — Close at community level (recall in 4-6 weeks).",
    )


def inferred_conditions(result: TriageResult) -> list[str]:
    """Condition keys the engine considers in play (moderate+ risk)."""
    return [
        f.condition.value for f in result.condition_findings
        if f.risk in (RiskLevel.high, RiskLevel.moderate)
    ]


# ---------- Backward-compatible single-disease entry point ----------
def triage_leprosy(s: LeprosyScreening) -> TriageResult:
    """Legacy shim: map a bare LeprosyScreening onto the symptom model."""
    screening = Screening(
        skin_changes=s.has_skin_patches,
        skin_loss_of_sensation=s.patch_loss_of_sensation,
        skin_patch_count=s.patch_count,
        enlarged_nerves=s.enlarged_nerves,
        glove_stocking_anesthesia=s.glove_stocking_anesthesia,
        numbness_or_weakness=s.weakness_in_hands_or_feet,
        family_history_leprosy=s.family_history,
        duration_months=s.duration_months,
    )
    return triage(screening)
