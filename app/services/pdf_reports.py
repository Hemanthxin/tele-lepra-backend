"""PDF report generation for case intake + MO decision.

Two builders:
  - build_intake_pdf(case)   : every field the agent collected
  - build_decision_pdf(case) : MO clinical assessment + final decision

Returns bytes — caller is responsible for streaming to a browser or uploading
to Firebase Storage.

Design notes
------------
Medical-grade layout: a branded header band and a confidentiality footer are
painted on *every* page via a custom canvas (so we also get "Page X of Y").
All timestamps are rendered in India Standard Time (IST, UTC+05:30). Content is
laid out in full-width, lightly-coloured tables with brand-coloured section
bands so it reads cleanly both in colour and on grayscale prints.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .ids import display_id

# India Standard Time — all timestamps in the report are shown in IST.
IST = timezone(timedelta(hours=5, minutes=30))

# Page geometry
PAGE_W, PAGE_H = A4
MARGIN = 15 * mm
CONTENT_W = PAGE_W - 2 * MARGIN  # 180 mm
HEADER_BAND_H = 20 * mm

# Colours mirror the brand palette used in the frontend.
BRAND = colors.HexColor("#0f766e")
BRAND_DARK = colors.HexColor("#0b5b54")
BRAND_SOFT = colors.HexColor("#e6f4f1")
INK = colors.HexColor("#0f172a")
SOFT = colors.HexColor("#475569")
MUTED = colors.HexColor("#94a3b8")
BORDER = colors.HexColor("#e2e8f0")
SURFACE_2 = colors.HexColor("#f1f5f9")
LABEL_BG = colors.HexColor("#f8fafc")

# Semantic colours for status callouts: (background, text, left-accent).
_SEMANTIC = {
    "good": (colors.HexColor("#ecfdf5"), colors.HexColor("#047857"), colors.HexColor("#10b981")),
    "warn": (colors.HexColor("#fffbeb"), colors.HexColor("#b45309"), colors.HexColor("#f59e0b")),
    "crit": (colors.HexColor("#fef2f2"), colors.HexColor("#b91c1c"), colors.HexColor("#ef4444")),
    "info": (BRAND_SOFT, BRAND, BRAND),
}


# ---------- Styles ----------
def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=17,
            leading=21, textColor=INK, spaceBefore=0, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontName="Helvetica", fontSize=9,
            textColor=SOFT, leading=13, spaceAfter=2,
        ),
        "section": ParagraphStyle(
            "section", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=9.5,
            textColor=BRAND, leading=12,
        ),
        "label": ParagraphStyle(
            "label", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=6.5,
            textColor=MUTED, leading=9,
        ),
        "value": ParagraphStyle(
            "value", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=9,
            textColor=INK, leading=12,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontName="Helvetica", fontSize=9,
            textColor=INK, leading=13,
        ),
        "soft": ParagraphStyle(
            "soft", parent=base["Normal"], fontName="Helvetica", fontSize=8,
            textColor=SOFT, leading=11,
        ),
        "callout_label": ParagraphStyle(
            "callout_label", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=6.5,
            textColor=MUTED, leading=9, spaceAfter=2,
        ),
        "callout_value": ParagraphStyle(
            "callout_value", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=12.5,
            textColor=INK, leading=15,
        ),
    }


# ---------- Helpers ----------
def _fmt_dt(v: Any) -> str:
    """Render any timestamp in India Standard Time."""
    if not v:
        return "—"
    try:
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(IST).strftime("%d %b %Y · %I:%M %p IST")
    except Exception:
        return str(v)


def _now_ist_str() -> str:
    return datetime.now(IST).strftime("%d %b %Y · %I:%M %p IST")


def _yn(b: Any) -> str:
    if b is None or b == "":
        return "—"
    return "Yes" if b else "No"


def _val(v: Any, default: str = "—") -> str:
    if v is None or v == "" or v == []:
        return default
    return str(v)


_RELATION_LABELS = {
    "self": "Self",
    "father_mother": "Father / Mother",
    "husband_wife": "Husband / Wife",
    "brother_sister": "Brother / Sister",
    "son_daughter": "Son / Daughter",
    "grand_son_grand_daughter": "Grand Son / Grand Daughter",
    "others": "Others",
}

_SYMPTOM_LABELS = {
    "skin_patches": "Light/reddish skin patches",
    "patch_loss_of_sensation": "Loss of sensation over patches",
    "numb_tingling_burning": "Tingling/numbness/burning hands/feet",
    "weakness_in_hands_or_feet": "Weakness in hands or feet",
    "weak_grip": "Weak grip / objects slipping",
    "painless_wounds": "Painless wounds/burns/ulcers",
    "nerve_tenderness": "Pain/tenderness near joints",
    "foot_drop": "Foot drop / dragging",
    "eye_closure_difficulty": "Difficulty closing eyes",
    "eyebrow_loss_nasal_collapse": "Eyebrow loss / collapsed nose",
    "nodules_or_earlobe_swelling": "Nodules / earlobe swelling",
}

_LESION_LABELS = {
    "single": "Single",
    "two_to_ten": "2-10",
    "more_than_ten": ">10",
    "pure_neuritic": "Pure neuritic",
    "diffuse": "Diffuse lesions",
}

_CLINICAL_STATUS_LABELS = {
    "new_untreated": "New untreated",
    "continuation_mdt": "Continuation of MDT",
    "released": "Released from treatment / control",
    "defaulter": "Defaulter / dropped out",
    "relapse": "Relapse",
}

_WHO_LABELS = {
    "multibacillary": "Multibacillary (MB)",
    "paucibacillary": "Paucibacillary (PB)",
}

_NERVE_LABELS = {
    "radial": "Radial",
    "ulnar": "Ulnar",
    "median": "Median",
    "lateral_popliteal": "Lateral popliteal",
    "posterior_tibial": "Posterior tibial",
}

_NERVE_STATE_LABELS = {
    "none": "None",
    "tender": "Tender",
    "enlarged": "Enlarged",
    "not_examined": "Not examined",
}

_DECISION_LABELS = {
    "close_remote": "Closed remotely (rule-out / treated at community)",
    "refer": "Referred for further care",
}


def _escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _data_table(rows: list[tuple[str, Any]], styles, col_width: float = CONTENT_W) -> Table:
    """Full-width 2-up label/value grid (label-value | label-value) with light
    label tints, thin row separators and an outer hairline box."""
    pairs = []
    for i in range(0, len(rows), 2):
        left = rows[i]
        right = rows[i + 1] if i + 1 < len(rows) else ("", "")
        pairs.append((left, right))
    cells = []
    for (l_label, l_val), (r_label, r_val) in pairs:
        cells.append([
            Paragraph(_escape(l_label.upper()), styles["label"]),
            Paragraph(_escape(str(l_val) if l_val not in (None, "", []) else "—"), styles["value"]),
            Paragraph(_escape(r_label.upper()), styles["label"]),
            Paragraph(_escape(str(r_val) if r_val not in (None, "", []) else "—"), styles["value"]),
        ])
    tbl = Table(
        cells,
        colWidths=[col_width * 0.18, col_width * 0.32, col_width * 0.18, col_width * 0.32],
    )
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (0, -1), LABEL_BG),
        ("BACKGROUND", (2, 0), (2, -1), LABEL_BG),
        ("LINEAFTER", (0, 0), (0, -1), 0.4, BORDER),
        ("LINEAFTER", (2, 0), (2, -1), 0.4, BORDER),
        ("ROWBACKGROUNDS", (1, 0), (1, -1), [colors.white, colors.white]),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return tbl


def _section(title: str, styles) -> Table:
    """A brand-coloured section band with a left accent bar."""
    band = Table([[Paragraph(title.upper(), styles["section"])]], colWidths=[CONTENT_W])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_SOFT),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, BRAND),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return band


def _sec(story: list, title: str, styles) -> None:
    """Append spacing + a section band to the story."""
    story.append(Spacer(1, 9))
    story.append(_section(title, styles))
    story.append(Spacer(1, 5))


def _callout(label: str, value: str, kind: str, styles) -> Table:
    """Full-width colour-coded status card (used for triage / decision outcome)."""
    bg, tx, accent = _SEMANTIC.get(kind, _SEMANTIC["info"])
    val_style = ParagraphStyle(
        "callout_val_dyn", parent=styles["callout_value"], textColor=tx,
    )
    cell = [
        Paragraph(_escape(label.upper()), styles["callout_label"]),
        Paragraph(_escape(value), val_style),
    ]
    t = Table([[cell]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
        ("BOX", (0, 0), (-1, -1), 0.5, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _pills_row(items: list[str], styles, empty: str = "None reported") -> Paragraph:
    if not items:
        return Paragraph(f"<i>{empty}</i>", styles["soft"])
    pretty = " · ".join(_escape(i) for i in items)
    return Paragraph(pretty, styles["body"])


def _mini_label(text: str, styles) -> Paragraph:
    return Paragraph(_escape(text.upper()), styles["label"])


def _title_block(title: str, subtitle: str, styles) -> list:
    return [
        Paragraph(_escape(title), styles["h1"]),
        Paragraph(_escape(subtitle), styles["subtitle"]),
        Spacer(1, 2),
    ]


def _triage_kind(outcome: str) -> str:
    o = (outcome or "").lower()
    if o in ("refer", "escalate", "refer_urgent", "referred"):
        return "crit"
    if o in ("alternative_dx", "alt_dx", "review"):
        return "warn"
    if o in ("close", "rule_out", "ruled_out", "close_remote", "closed"):
        return "good"
    return "info"


def _decision_kind(status: str) -> str:
    s = (status or "").lower()
    if s == "referred":
        return "warn"
    if "closed" in s:
        return "good"
    return "info"


# ---------- Page furniture (header band + footer, every page) ----------
def _paint_header(c: canvas.Canvas, meta: dict) -> None:
    c.saveState()
    top = PAGE_H
    band_top = top
    band_bottom = top - HEADER_BAND_H

    # Brand band + darker accent stripe beneath it.
    c.setFillColor(BRAND)
    c.rect(0, band_bottom, PAGE_W, HEADER_BAND_H, fill=1, stroke=0)
    c.setFillColor(BRAND_DARK)
    c.rect(0, band_bottom - 1.4 * mm, PAGE_W, 1.4 * mm, fill=1, stroke=0)

    mid = band_bottom + HEADER_BAND_H / 2

    # Logo mark: white rounded square with a brand medical cross.
    box = 9.5 * mm
    bx, by = MARGIN, mid - box / 2
    c.setFillColor(colors.white)
    c.roundRect(bx, by, box, box, 2 * mm, fill=1, stroke=0)
    c.setStrokeColor(BRAND)
    c.setLineWidth(1.6)
    cx, cy = bx + box / 2, by + box / 2
    c.line(cx, cy - 2.6 * mm, cx, cy + 2.6 * mm)
    c.line(cx - 2.6 * mm, cy, cx + 2.6 * mm, cy)

    # Org title.
    tx = bx + box + 4 * mm
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(tx, mid + 0.6 * mm, "LEPRA SOCIETY")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.Color(1, 1, 1, alpha=0.85))
    c.drawString(tx, mid - 3.4 * mm, "Tele-Leprosy Programme · Triage Console")

    # Right side: document kind + case number.
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawRightString(PAGE_W - MARGIN, mid + 1.2 * mm, meta["doc_kind"])
    c.setFont("Helvetica", 7)
    c.setFillColor(colors.Color(1, 1, 1, alpha=0.85))
    c.drawRightString(PAGE_W - MARGIN, mid - 3.2 * mm, f"Case #{meta['short_id']}")
    c.restoreState()


def _paint_footer(c: canvas.Canvas, meta: dict, page: int, total: int) -> None:
    c.saveState()
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)

    c.setFont("Helvetica", 6.5)
    c.setFillColor(MUTED)
    c.drawString(MARGIN, 10.5 * mm, f"Generated {meta['generated']}")
    c.drawRightString(PAGE_W - MARGIN, 10.5 * mm, f"Page {page} of {total}")
    c.drawCentredString(
        PAGE_W / 2, 7 * mm,
        "CONFIDENTIAL · For authorised clinical use only · Contains protected health information",
    )
    c.restoreState()


def _make_canvas(meta: dict):
    """Canvas factory that paints the header/footer on every page and resolves
    the true total page count for 'Page X of Y'."""

    class _ReportCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_states = []

        def showPage(self):
            self._saved_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_states)
            for i, state in enumerate(self._saved_states, start=1):
                self.__dict__.update(state)
                _paint_header(self, meta)
                _paint_footer(self, meta, i, total)
                canvas.Canvas.showPage(self)
            canvas.Canvas.save(self)

    return _ReportCanvas


def _new_doc(buf: BytesIO, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=HEADER_BAND_H + 9 * mm, bottomMargin=20 * mm,
        title=title,
    )


_SUSPECT_LABELS = {
    "leprosy": "Leprosy",
    "lymphatic_filariasis": "Lymphatic Filariasis",
    "tuberculosis": "Tuberculosis",
    "scabies": "Scabies",
    "japanese_encephalitis": "Japanese Encephalitis",
    "malaria": "Malaria",
    "sickle_cell": "Sickle Cell Disease",
}

# Screening symptom groups for the intake PDF: (group title, [(label, key)]).
_SYMPTOM_GROUPS = [
    ("Skin", [
        ("Skin patch / rash / discolouration", "skin_changes"),
        ("Pale / reddish patch", "skin_pale_or_reddish_patch"),
        ("Loss of sensation over patch", "skin_loss_of_sensation"),
        ("Patch count", "skin_patch_count"),
        ("Itchy, worse at night", "skin_itchy_worse_at_night"),
        ("Others at home affected", "skin_household_others_affected"),
        ("Nodules / ear-lobe swelling", "skin_nodules_or_earlobe"),
    ]),
    ("Nerve / weakness", [
        ("Numbness / tingling / weakness", "numbness_or_weakness"),
        ("Glove-and-stocking anaesthesia", "glove_stocking_anesthesia"),
        ("Thickened / enlarged nerves", "enlarged_nerves"),
        ("Eye-closure difficulty / foot drop", "eye_closure_or_foot_drop"),
        ("Painless wounds / ulcers", "painless_wounds"),
    ]),
    ("Fever", [
        ("Fever (last 2 weeks)", "fever"),
        ("With chills / rigor", "fever_chills_rigor"),
        ("Periodic pattern", "fever_periodic"),
        ("With altered consciousness / fits", "fever_altered_consciousness"),
        ("With neck stiffness / headache", "fever_neck_stiff_or_headache"),
        ("With night sweats", "fever_night_sweats"),
    ]),
    ("Cough", [
        ("Cough", "cough"),
        ("Lasting 2 weeks or more", "cough_2_weeks_or_more"),
        ("Blood in sputum", "cough_blood_in_sputum"),
        ("With weight loss", "cough_weight_loss"),
    ]),
    ("Swelling", [
        ("Swelling (limb / breast / genitals)", "swelling"),
        ("Persistent limb / genital swelling", "swelling_limb_or_genitals"),
        ("Recurrent acute attacks", "swelling_acute_attacks"),
    ]),
    ("Pain / fatigue", [
        ("Recurrent pain / fatigue / jaundice", "pain_or_fatigue"),
        ("Recurrent severe pain episodes", "recurrent_pain_episodes"),
        ("Anaemia / fatigue", "anaemia_or_fatigue"),
        ("Jaundice", "jaundice"),
        ("Family history of sickle cell", "family_history_sickle_cell"),
    ]),
    ("General", [
        ("Family history of leprosy", "family_history_leprosy"),
        ("Duration (months)", "duration_months"),
    ]),
]

_NUMERIC_SYMPTOM_KEYS = {"skin_patch_count", "duration_months"}


def _symptom_value(screen: dict, key: str) -> str:
    val = screen.get(key)
    if key in _NUMERIC_SYMPTOM_KEYS:
        return str(val) if val else "—"
    return _yn(val)


# ========== INTAKE PDF ==========
def build_intake_pdf(case: dict) -> bytes:
    """Patient intake report — everything the agent captured."""
    styles = _styles()
    buf = BytesIO()
    case_id = case.get("id", "")
    short_id = display_id(case_id)
    patient_name = case.get("patient_name") or "Patient"
    meta = {
        "short_id": short_id,
        "doc_kind": "CONFIDENTIAL · PATIENT INTAKE",
        "generated": _now_ist_str(),
    }
    doc = _new_doc(buf, f"Patient intake report · {short_id}")

    story: list = []
    story += _title_block(
        "Patient Intake Report",
        f"Case #{short_id} · {patient_name} · screened {_fmt_dt(case.get('created_at'))} · "
        f"prepared for medical-officer review",
        styles,
    )

    # Programme context
    _sec(story, "Programme context", styles)
    story.append(_data_table([
        ("PHC / CHC", case.get("patient_phc")),
        ("Referred by", case.get("patient_referred_by")),
    ], styles))

    # Demographics
    _sec(story, "Demographics", styles)
    age = case.get("patient_age")
    story.append(_data_table([
        ("Full name", patient_name),
        ("Age", f"{age} years" if age else None),
        ("Sex", str(case.get("patient_sex") or "").capitalize() or None),
        ("Phone", case.get("patient_phone")),
    ], styles))

    # Location & identifiers
    _sec(story, "Location & identifiers", styles)
    story.append(_data_table([
        ("House no", case.get("patient_house_no")),
        ("Village", case.get("patient_village")),
        ("Gram Panchayat", case.get("patient_gram_panchayat")),
        ("District", case.get("patient_district")),
        ("State", case.get("patient_state")),
        ("Aadhaar", case.get("patient_aadhaar_id")),
        ("ABHA", case.get("patient_abha_id")),
        ("", ""),
    ], styles))

    # Household
    _sec(story, "Household", styles)
    rel = case.get("patient_relation_to_head")
    story.append(_data_table([
        ("Household number", case.get("patient_household_number")),
        ("Relation to head", _RELATION_LABELS.get(rel, rel) if rel else None),
        ("Head-of-family name", case.get("patient_head_of_family_name")),
        ("Head-of-family phone", case.get("patient_head_of_family_phone")),
    ], styles))

    # History
    hist = case.get("history") or {}
    _sec(story, "Medical history", styles)
    chronic = hist.get("chronic_conditions") or []
    story.append(_mini_label("Chronic conditions", styles))
    story.append(_pills_row(chronic, styles, "None reported"))
    notes = hist.get("past_visits_notes")
    if notes:
        story.append(Spacer(1, 4))
        story.append(_mini_label("Past visits / notes", styles))
        story.append(Paragraph(_escape(notes).replace("\n", "<br/>"), styles["body"]))

    # Screening — auto-detected conditions + symptom answers
    screen = case.get("screening") or {}
    suspected = screen.get("suspected_diseases") or []

    _sec(story, "Suspected condition(s) — auto-detected", styles)
    story.append(_pills_row(
        [_SUSPECT_LABELS.get(d, str(d).replace("_", " ").title()) for d in suspected],
        styles, "None flagged",
    ))

    # Reported symptoms — show each group that has at least one answered item.
    _sec(story, "Reported symptoms", styles)
    for title, items in _SYMPTOM_GROUPS:
        answered = [(label, key) for (label, key) in items if screen.get(key) not in (None, "")]
        if not answered:
            continue
        story.append(_mini_label(title, styles))
        story.append(_data_table(
            [(label, _symptom_value(screen, key)) for (label, key) in answered], styles,
        ))
        story.append(Spacer(1, 4))

    # Screening event (date + GPS)
    if screen.get("screened_at") or screen.get("geolocation"):
        _sec(story, "Screening event", styles)
        geo = screen.get("geolocation") or {}
        gps_text = "—"
        if geo and (geo.get("lat") is not None and geo.get("lng") is not None):
            acc = geo.get("accuracy")
            gps_text = f"{geo['lat']:.5f}, {geo['lng']:.5f}"
            if acc is not None:
                gps_text += f" (±{int(acc)} m)"
        story.append(_data_table([
            ("Date of screening", _fmt_dt(screen.get("screened_at"))),
            ("GPS", gps_text),
        ], styles))

    # Notes
    if screen.get("notes"):
        story.append(Spacer(1, 4))
        story.append(_mini_label("Agent notes", styles))
        story.append(Paragraph(_escape(screen["notes"]).replace("\n", "<br/>"), styles["body"]))

    # Attachments — URLs only.
    imgs = screen.get("image_urls") or []
    labs = (screen.get("lab_urls") or []) + (hist.get("prior_labs_urls") or [])
    rx_urls = hist.get("prior_prescriptions_urls") or []
    if imgs or labs or rx_urls:
        _sec(story, "Attachments", styles)
        if imgs:
            story.append(Paragraph(f"<b>{len(imgs)} screening image(s)</b>", styles["body"]))
            for u in imgs[:20]:
                story.append(Paragraph(f"<link href='{_escape(u)}'>{_escape(u)}</link>", styles["soft"]))
            story.append(Spacer(1, 4))
        if labs:
            story.append(Paragraph(f"<b>{len(labs)} lab report(s)</b>", styles["body"]))
            for u in labs[:20]:
                story.append(Paragraph(f"<link href='{_escape(u)}'>{_escape(u)}</link>", styles["soft"]))
            story.append(Spacer(1, 4))
        if rx_urls:
            story.append(Paragraph(f"<b>{len(rx_urls)} prior prescription(s)</b>", styles["body"]))
            for u in rx_urls[:20]:
                story.append(Paragraph(f"<link href='{_escape(u)}'>{_escape(u)}</link>", styles["soft"]))

    # Triage result (if screening submitted)
    triage = case.get("triage") or {}
    if triage:
        _sec(story, "Automated triage result", styles)
        outcome = str(triage.get("outcome") or "").replace("_", " ").title() or "—"
        story.append(_callout("Triage outcome", outcome, _triage_kind(triage.get("outcome")), styles))
        story.append(Spacer(1, 5))
        conf = triage.get("confidence")
        allow_close = triage.get("allow_close")
        disposition = (
            "Forced — Send to Medical Officer" if allow_close is False
            else "Agent's choice: Send to MO or Close" if allow_close is True
            else None
        )
        story.append(_data_table([
            ("Confidence", f"{int((conf or 0) * 100)}%" if conf is not None else None),
            ("Suspected condition", triage.get("suspected_condition")),
            ("Recommendation", triage.get("recommendation")),
            ("Disposition", disposition),
        ], styles))

        # Per-condition findings
        findings = triage.get("condition_findings") or []
        if findings:
            story.append(Spacer(1, 5))
            story.append(_mini_label("Conditions assessed", styles))
            for f in findings:
                cond = _SUSPECT_LABELS.get(f.get("condition"), str(f.get("condition")))
                risk = str(f.get("risk") or "").title()
                story.append(Paragraph(f"<b>{_escape(cond)}</b> — {_escape(risk)} risk", styles["body"]))
                for r in (f.get("reasons") or []):
                    story.append(Paragraph(f"&nbsp;&nbsp;• {_escape(r)}", styles["soft"]))

        # Agent's recorded decision (if made)
        decision = case.get("agent_decision")
        if decision:
            story.append(Spacer(1, 5))
            label = "Sent to Medical Officer" if decision == "send_mo" else "Closed at community level"
            story.append(_callout(
                "Agent decision", label,
                "warn" if decision == "send_mo" else "good", styles,
            ))
            if case.get("agent_decision_note"):
                story.append(Spacer(1, 3))
                story.append(Paragraph(_escape(case["agent_decision_note"]), styles["soft"]))

        if triage.get("suggested_action"):
            story.append(Spacer(1, 5))
            story.append(_mini_label("Suggested action", styles))
            story.append(Paragraph(_escape(triage["suggested_action"]), styles["body"]))

    doc.build(story, canvasmaker=_make_canvas(meta))
    return buf.getvalue()


# ========== DECISION PDF ==========
def build_decision_pdf(case: dict) -> bytes:
    """Post-consultation report — clinical assessment + final decision."""
    styles = _styles()
    buf = BytesIO()
    case_id = case.get("id", "")
    short_id = display_id(case_id)
    patient_name = case.get("patient_name") or "Patient"
    meta = {
        "short_id": short_id,
        "doc_kind": "CONFIDENTIAL · DECISION RECORD",
        "generated": _now_ist_str(),
    }
    doc = _new_doc(buf, f"Decision report · {short_id}")

    story: list = []
    story += _title_block(
        "Tele-Consultation Decision Report",
        f"Case #{short_id} · {patient_name} · {_fmt_dt(case.get('closed_at') or case.get('updated_at'))}",
        styles,
    )

    # Identity block (compact)
    _sec(story, "Patient", styles)
    age = case.get("patient_age")
    story.append(_data_table([
        ("Name", patient_name),
        ("Age / Sex", f"{age}y · {str(case.get('patient_sex') or '').capitalize()}" if age else None),
        ("PHC / CHC", case.get("patient_phc")),
        ("Phone", case.get("patient_phone")),
        ("Aadhaar", case.get("patient_aadhaar_id")),
        ("ABHA", case.get("patient_abha_id")),
    ], styles))

    # Clinical assessment
    ca = case.get("clinical_assessment") or {}
    _sec(story, "MO clinical assessment", styles)
    if not ca:
        story.append(Paragraph("<i>Clinical assessment not recorded.</i>", styles["soft"]))
    else:
        lesion = ca.get("lesion_count")
        clinical_status = ca.get("clinical_status")
        who = ca.get("who_classification")
        story.append(_data_table([
            ("Confirmed leprosy", _yn(ca.get("confirmed_leprosy"))),
            ("No. of skin lesions", _LESION_LABELS.get(lesion, lesion) if lesion else None),
            ("Status of case", _CLINICAL_STATUS_LABELS.get(clinical_status, clinical_status) if clinical_status else None),
            ("WHO classification", _WHO_LABELS.get(who, who) if who else None),
            ("Disability grade", f"Grade {ca['disability_grade']}" if ca.get("disability_grade") is not None else None),
            ("Sensory loss", ", ".join(s.title() for s in (ca.get("sensory_loss") or [])) or None),
        ], styles))

        # Nerve involvement table
        nerves = ca.get("nerve_involvement") or []
        if nerves:
            story.append(Spacer(1, 6))
            story.append(_mini_label("Nerve involvement", styles))
            story.append(Spacer(1, 3))
            grid: dict[str, dict[str, str]] = {}
            for f in nerves:
                nerve = f.get("nerve")
                side = f.get("side")
                state = f.get("state")
                if nerve and side:
                    grid.setdefault(nerve, {})[side] = state
            rows = [["Nerve", "Right", "Left"]]
            for nerve_key, label in _NERVE_LABELS.items():
                r = grid.get(nerve_key, {})
                rows.append([
                    label,
                    _NERVE_STATE_LABELS.get(r.get("right", "not_examined"), r.get("right", "—")),
                    _NERVE_STATE_LABELS.get(r.get("left", "not_examined"), r.get("left", "—")),
                ])
            nt = Table(rows, colWidths=[CONTENT_W * 0.40, CONTENT_W * 0.30, CONTENT_W * 0.30])
            nt.setStyle(TableStyle([
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("TEXTCOLOR", (0, 1), (-1, -1), INK),
                ("BACKGROUND", (0, 0), (-1, 0), BRAND),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SURFACE_2]),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
                ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(nt)

        comps = ca.get("complications") or []
        if comps:
            story.append(Spacer(1, 6))
            story.append(_mini_label("Complications", styles))
            pretty = ", ".join(c.replace("_", " ").title() for c in comps)
            story.append(Paragraph(pretty, styles["body"]))

        if ca.get("treatment_plan"):
            story.append(Spacer(1, 6))
            story.append(_mini_label("Treatment plan", styles))
            story.append(Paragraph(_escape(ca["treatment_plan"]).replace("\n", "<br/>"), styles["body"]))

    # Decision
    decision = case.get("status") or ""
    if decision == "referred":
        decision_label = "Referred for further care"
    elif decision == "closed_alt_dx":
        decision_label = "Closed — alternative diagnosis, treated at community level"
    elif "closed" in decision:
        decision_label = "Closed remotely (rule-out / treated at community)"
    else:
        decision_label = decision.replace("_", " ").title() if decision else "—"
    _sec(story, "Final decision", styles)
    story.append(_callout("Outcome", decision_label, _decision_kind(decision), styles))
    extra: list[tuple[str, Any]] = []
    if case.get("prescription"):
        extra.append(("Prescription / care plan", case["prescription"]))
    if case.get("referral_note"):
        extra.append(("Referral note", case["referral_note"]))
    if case.get("mo_notes"):
        extra.append(("Internal notes", case["mo_notes"]))
    extra.append(("Closed at", _fmt_dt(case.get("closed_at"))))
    story.append(Spacer(1, 5))
    story.append(_data_table(extra, styles))

    doc.build(story, canvasmaker=_make_canvas(meta))
    return buf.getvalue()
