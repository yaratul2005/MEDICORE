import io
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

DATA_DIR = Path(__file__).resolve().parent / "data"

# Cache datasets in memory
_ICD10_CACHE: Optional[List[Dict[str, Any]]] = None
_DRUGS_CACHE: Optional[List[Dict[str, Any]]] = None
_FREQUENCIES_CACHE: Optional[List[Dict[str, Any]]] = None
_INTERACTIONS_CACHE: Optional[Dict[str, Any]] = None


def load_icd10() -> List[Dict[str, Any]]:
    global _ICD10_CACHE
    if _ICD10_CACHE is None:
        path = DATA_DIR / "icd10.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                _ICD10_CACHE = json.load(f)
        else:
            _ICD10_CACHE = []
    return _ICD10_CACHE


def load_drugs() -> List[Dict[str, Any]]:
    global _DRUGS_CACHE
    if _DRUGS_CACHE is None:
        path = DATA_DIR / "drugs.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                _DRUGS_CACHE = json.load(f)
        else:
            _DRUGS_CACHE = []
    return _DRUGS_CACHE


def load_frequencies() -> List[Dict[str, Any]]:
    global _FREQUENCIES_CACHE
    if _FREQUENCIES_CACHE is None:
        path = DATA_DIR / "frequencies.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                _FREQUENCIES_CACHE = json.load(f)
        else:
            _FREQUENCIES_CACHE = []
    return _FREQUENCIES_CACHE


def load_interactions() -> Dict[str, Any]:
    global _INTERACTIONS_CACHE
    if _INTERACTIONS_CACHE is None:
        path = DATA_DIR / "interactions.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                _INTERACTIONS_CACHE = json.load(f)
        else:
            _INTERACTIONS_CACHE = {"drug_drug_interactions": [], "drug_allergy_classes": {}, "duplicate_therapy_classes": {}}
    return _INTERACTIONS_CACHE


def search_icd10(query: str, limit: int = 15) -> List[Dict[str, Any]]:
    data = load_icd10()
    q = query.strip().lower()
    if not q:
        return [item for item in data if item.get("is_common")][:limit]
    results = []
    for item in data:
        if q in item["code"].lower() or q in item["description"].lower() or q in item.get("category", "").lower():
            results.append(item)
            if len(results) >= limit:
                break
    return results


def search_drugs(query: str, limit: int = 15) -> List[Dict[str, Any]]:
    data = load_drugs()
    q = query.strip().lower()
    if not q:
        return data[:limit]
    results = []
    for d in data:
        if (
            q in d["brand_name"].lower()
            or q in d["generic_name"].lower()
            or q in d.get("class", "").lower()
        ):
            results.append(d)
            if len(results) >= limit:
                break
    return results


def calculate_bmi(weight_kg: Optional[float], height_cm: Optional[float]) -> Optional[float]:
    if not weight_kg or not height_cm or height_cm <= 0:
        return None
    h_m = height_cm / 100.0
    return round(weight_kg / (h_m * h_m), 1)


def calculate_bsa(weight_kg: Optional[float], height_cm: Optional[float]) -> Optional[float]:
    """Mosteller formula: BSA (m²) = sqrt((height (cm) * weight (kg)) / 3600)"""
    if not weight_kg or not height_cm or height_cm <= 0 or weight_kg <= 0:
        return None
    return round(math.sqrt((height_cm * weight_kg) / 3600.0), 2)


def get_vitals_warnings(
    systolic: Optional[int] = None,
    diastolic: Optional[int] = None,
    pulse: Optional[int] = None,
    temp_c: Optional[float] = None,
    spo2: Optional[float] = None,
    resp_rate: Optional[int] = None,
) -> List[Dict[str, str]]:
    warnings: List[Dict[str, str]] = []

    if systolic is not None or diastolic is not None:
        sys = systolic or 0
        dia = diastolic or 0
        if sys >= 140 or dia >= 90:
            warnings.append({"param": "BP", "label": "Stage 2 Hypertension", "severity": "rose", "value": f"{sys}/{dia} mmHg"})
        elif sys >= 130 or dia >= 80:
            warnings.append({"param": "BP", "label": "Stage 1 Hypertension", "severity": "amber", "value": f"{sys}/{dia} mmHg"})
        elif sys < 90 or dia < 60:
            warnings.append({"param": "BP", "label": "Hypotension", "severity": "amber", "value": f"{sys}/{dia} mmHg"})

    if pulse is not None:
        if pulse > 100:
            warnings.append({"param": "Pulse", "label": "Tachycardia", "severity": "rose", "value": f"{pulse} bpm"})
        elif pulse < 60:
            warnings.append({"param": "Pulse", "label": "Bradycardia", "severity": "amber", "value": f"{pulse} bpm"})

    if temp_c is not None:
        if temp_c >= 38.3:
            warnings.append({"param": "Temp", "label": "High Fever", "severity": "rose", "value": f"{temp_c} °C"})
        elif temp_c >= 37.8:
            warnings.append({"param": "Temp", "label": "Low-grade Fever", "severity": "amber", "value": f"{temp_c} °C"})
        elif temp_c < 35.5:
            warnings.append({"param": "Temp", "label": "Hypothermia", "severity": "rose", "value": f"{temp_c} °C"})

    if spo2 is not None:
        if spo2 < 92:
            warnings.append({"param": "SpO2", "label": "Critical Hypoxia", "severity": "rose", "value": f"{spo2}%"})
        elif spo2 < 95:
            warnings.append({"param": "SpO2", "label": "Suboptimal Oxygenation", "severity": "amber", "value": f"{spo2}%"})

    if resp_rate is not None:
        if resp_rate > 22:
            warnings.append({"param": "RR", "label": "Tachypnea", "severity": "amber", "value": f"{resp_rate}/min"})
        elif resp_rate < 10:
            warnings.append({"param": "RR", "label": "Bradypnea", "severity": "rose", "value": f"{resp_rate}/min"})

    return warnings


def generate_sparkline(values: List[float], width: int = 100, height: int = 24, stroke_color: str = "#0d9488") -> str:
    """Generates an inline SVG polyline sparkline for numerical trends"""
    if not values:
        return ""
    if len(values) == 1:
        values = [values[0], values[0]]

    min_v = min(values)
    max_v = max(values)
    span = (max_v - min_v) if max_v != min_v else 1.0

    points = []
    step = width / (len(values) - 1)
    for i, v in enumerate(values):
        x = round(i * step, 1)
        # Invert y: high value -> small y
        y = round(height - 4 - ((v - min_v) / span) * (height - 8), 1)
        points.append(f"{x},{y}")

    poly = " ".join(points)
    last_pt = points[-1].split(",")
    return f"""<svg width="{width}" height="{height}" class="overflow-visible inline-block">
      <polyline fill="none" stroke="{stroke_color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" points="{poly}" />
      <circle cx="{last_pt[0]}" cy="{last_pt[1]}" r="2.5" fill="{stroke_color}" />
    </svg>"""


# SOAP templates by clinical specialty
SOAP_TEMPLATES: Dict[str, Dict[str, str]] = {
    "General Medicine": {
        "subjective": "Patient presents for general medical evaluation regarding [chief complaint]. Reports onset [duration] ago. Associated symptoms include [...]. Denies chest pain, shortness of breath, focal weakness, or weight loss.",
        "objective": "Alert, awake, in no acute distress. Vitals verified. HEENT: Normocephalic, atraumatic, mucous membranes moist. Cardiopulmonary: Regular rate and rhythm, normal S1/S2, lungs clear to auscultation bilaterally without wheezes. Abdomen: Soft, non-tender, non-distended. Extremities: No peripheral edema.",
        "assessment": "1. [Primary Diagnosis]\n2. Clinically stable condition with good functional baseline.",
        "plan": "1. Medical management and prescriptions initiated.\n2. Hydration, healthy diet, and lifestyle modification advised.\n3. Return immediately if red-flag symptoms arise; follow-up scheduled.",
    },
    "Cardiology": {
        "subjective": "Presents for cardiovascular follow-up / complaint. Denies acute rest angina, paroxysmal nocturnal dyspnea, orthopnea, or presyncope. Reports moderate exercise tolerance.",
        "objective": "Vitals reviewed. BP optimized. JVP not elevated (<3 cm above sternal angle). Heart: S1/S2 present, no murmurs, rubs, or gallops. Lungs CTA bilaterally. Pulses: 2+ radial and dorsalis pedis bilaterally. Trace pedal edema absent.",
        "assessment": "1. Essential hypertension / Stable coronary artery disease.\n2. Cardiovascular risk parameters within target range.",
        "plan": "1. Continue anti-hypertensive and lipid-lowering regimens.\n2. Order baseline ECG and fasting lipid panel.\n3. DASH diet (low sodium <2g/day) and daily brisk walking.\n4. Follow-up in 4 weeks with home BP log.",
    },
    "Pediatrics": {
        "subjective": "Child accompanied by parent with concerns of [symptom] for [duration]. Appetite and fluid intake reported as [fair/normal]. Wet diapers / urine output maintained. No lethargy or convulsions.",
        "objective": "Active, interactive, smiles during exam. Warm and well-perfused. Ears: TMs pearly grey bilaterally. Throat: No tonsillar exudates. Chest: Clear breath sounds, no intercostal retractions. Abdomen: Soft, non-tender.",
        "assessment": "1. Acute viral upper respiratory illness / Age-appropriate development.\n2. Hydration status reassuring.",
        "plan": "1. Weight-based antipyretic / supportive therapy.\n2. Oral rehydration and continued feeding encouraged.\n3. Parent counselled on pediatric warning signs (stridor, dehydration, high fever).",
    },
    "Orthopedics": {
        "subjective": "Presents with joint / musculoskeletal discomfort involving [region]. Pain rated [x]/10, aggravated by weight-bearing and relieved by rest. Denies numbness, tingling, or bowel/bladder dysfunction.",
        "objective": "Gait: Antalgic / Normal. Inspection: No acute deformity, erythema, or open wounds. Palpation: Localized tenderness over [site]. Range of motion: Full / Mildly restricted. Neurovascular status intact distally.",
        "assessment": "1. Musculoskeletal strain / Osteoarthritis of [joint].\n2. No acute neurological compromise.",
        "plan": "1. Short-course NSAID therapy with gastroprotection.\n2. Rest, ice application, and physical therapy referral.\n3. Plain radiograph requested.",
    },
}

# Text shortcuts for clinical documentation
TEXT_SHORTCUTS: Dict[str, str] = {
    ".htn": "Essential (primary) hypertension. Blood pressure currently assessed. Advised low-sodium DASH diet, regular aerobic exercise, and strict medication compliance.",
    ".dm2": "Type 2 Diabetes Mellitus. Advised balanced low-glycemic dietary intake, routine home blood glucose monitoring, annual dilated eye exam, and daily foot inspections.",
    ".resp": "Bilateral breath sounds clear to auscultation. Normal respiratory effort without accessory muscle use. No wheezes, crackles, or pleural friction rubs.",
    ".norm": "Comprehensive physical examination unremarkable: Alert and oriented x3, heart regular rate and rhythm without murmur, lungs clear to auscultation bilaterally, abdomen soft and non-tender, no peripheral edema.",
    ".fu2w": "Follow-up consultation advised in 2 weeks, or sooner if symptoms persist or deteriorate.",
    ".labs": "Ordered routine clinical diagnostic panel: Complete Blood Count (CBC), Comprehensive Metabolic Panel (CMP), and Urinalysis.",
}


def expand_text_shortcuts(text: str) -> str:
    if not text:
        return text
    expanded = text
    for shortcut, replacement in TEXT_SHORTCUTS.items():
        expanded = expanded.replace(shortcut, replacement)
    return expanded


def calculate_quantity(frequency_code: str, duration_days: int) -> int:
    freqs = {f["code"]: f.get("times_per_day", 1) for f in load_frequencies()}
    multiplier = freqs.get(frequency_code.upper().strip(), 1)
    return max(1, multiplier * max(1, duration_days))


def check_prescription_safety(
    patient_allergies: Optional[str],
    candidate_drug_name: str,
    candidate_generic: Optional[str],
    existing_items: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """
    Pluggable clinical rules engine:
    1. Drug-Allergy matching
    2. Duplicate therapy detection
    3. Drug-Drug interaction checks
    """
    alerts: List[Dict[str, Any]] = []
    interactions_data = load_interactions()

    cand_brand = candidate_drug_name.strip().lower()
    cand_gen = (candidate_generic or candidate_drug_name).strip().lower()

    # 1. Allergy check
    if patient_allergies:
        patient_allergy_lower = patient_allergies.lower()
        allergy_classes = interactions_data.get("drug_allergy_classes", {})

        for allergen_name, drugs_in_class in allergy_classes.items():
            if allergen_name.lower() in patient_allergy_lower:
                # Check if candidate matches any drug in this allergen class
                for d in drugs_in_class:
                    if d.lower() in cand_brand or d.lower() in cand_gen or cand_gen in d.lower():
                        alerts.append({
                            "type": "Allergy Match",
                            "severity": "Major",
                            "drug": candidate_drug_name,
                            "conflict_with": allergen_name,
                            "message": f"Patient has documented allergy to '{allergen_name}'. Prescribing '{candidate_drug_name}' presents high anaphylaxis/hypersensitivity risk.",
                            "mechanism": "Immunological cross-reactivity and hypersensitivity reaction.",
                        })
                        break

        # Also check direct name substring in allergy text
        if cand_gen in patient_allergy_lower or cand_brand in patient_allergy_lower:
            if not any(a["type"] == "Allergy Match" for a in alerts):
                alerts.append({
                    "type": "Allergy Match",
                    "severity": "Major",
                    "drug": candidate_drug_name,
                    "conflict_with": patient_allergies,
                    "message": f"Drug matches patient allergy record: '{patient_allergies}'.",
                    "mechanism": "Documented direct hypersensitivity.",
                })

    # 2. Duplicate therapy check
    duplicate_classes = interactions_data.get("duplicate_therapy_classes", {})
    for item in existing_items:
        existing_name = item.get("drug_name", "").strip().lower()
        existing_gen = item.get("generic_name", item.get("drug_name", "")).strip().lower()

        # Direct duplicate drug
        if cand_gen == existing_gen or cand_brand == existing_name:
            alerts.append({
                "type": "Duplicate Therapy",
                "severity": "Moderate",
                "drug": candidate_drug_name,
                "conflict_with": item.get("drug_name"),
                "message": f"Duplicate active medication: '{candidate_drug_name}' is already in this prescription.",
                "mechanism": "Redundant therapeutic dosing.",
            })

        # Class duplicate
        for class_name, drugs in duplicate_classes.items():
            cand_in_class = any(d.lower() in cand_gen or d.lower() in cand_brand for d in drugs)
            existing_in_class = any(d.lower() in existing_gen or d.lower() in existing_name for d in drugs)
            if cand_in_class and existing_in_class:
                if not any(a["type"] == "Duplicate Therapy" and a["conflict_with"] == item.get("drug_name") for a in alerts):
                    alerts.append({
                        "type": "Duplicate Class Therapy",
                        "severity": "Moderate",
                        "drug": candidate_drug_name,
                        "conflict_with": f"{item.get('drug_name')} ({class_name})",
                        "message": f"Both medications belong to '{class_name}'. Dual therapy increases toxicity without proven efficacy.",
                        "mechanism": f"Class overlap: {class_name}.",
                    })

    # 3. Drug-Drug Interactions
    ddi_list = interactions_data.get("drug_drug_interactions", [])
    for item in existing_items:
        ex_name = item.get("drug_name", "").strip().lower()
        ex_gen = item.get("generic_name", item.get("drug_name", "")).strip().lower()

        for ddi in ddi_list:
            da = ddi["drug_a"].lower()
            db = ddi["drug_b"].lower()

            match1 = (da in cand_gen or da in cand_brand) and (db in ex_gen or db in ex_name)
            match2 = (db in cand_gen or db in cand_brand) and (da in ex_gen or da in ex_name)

            if match1 or match2:
                alerts.append({
                    "type": "Drug-Drug Interaction",
                    "severity": ddi.get("severity", "Major"),
                    "drug": candidate_drug_name,
                    "conflict_with": item.get("drug_name"),
                    "message": f"DDI Alert: {ddi.get('effect', 'Adverse interaction')} between {candidate_drug_name} and {item.get('drug_name')}.",
                    "mechanism": ddi.get("mechanism", "Pharmacodynamic or pharmacokinetic conflict."),
                })

    return alerts


def generate_visit_summary_pdf(
    hospital_info: Dict[str, Any],
    encounter_data: Dict[str, Any],
    patient_data: Dict[str, Any],
    doctor_data: Dict[str, Any],
    vitals_data: Optional[Dict[str, Any]],
    diagnoses: List[Dict[str, Any]],
    clinical_note: Optional[Dict[str, Any]],
    prescriptions: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
    follow_up: Optional[Dict[str, Any]] = None,
) -> bytes:
    """Generates an official clinical PDF visit summary and prescription via ReportLab"""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "HospitalTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        textColor=colors.HexColor("#0f766e"),
    )
    sub_title_style = ParagraphStyle(
        "HospitalSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#475569"),
    )
    section_heading = ParagraphStyle(
        "SectionHeading",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#0f766e"),
        spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#1e293b"),
    )
    bold_style = ParagraphStyle(
        "Bold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#0f172a"),
    )
    alert_style = ParagraphStyle(
        "Alert",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#b91c1c"),
    )
    disclaimer_style = ParagraphStyle(
        "Disclaimer",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#64748b"),
        alignment=1,  # Centered
    )

    story = []

    # 1. Hospital Header
    h_name = hospital_info.get("name", "MediCore Hospital & Medical Center")
    h_address = hospital_info.get("address", "100 Healthcare Boulevard, Metro City")
    h_phone = hospital_info.get("phone", "+1 (555) 234-5678")
    h_emergency = hospital_info.get("emergency_contact", "911 / (555) 999-0000")

    header_table_data = [
        [
            Paragraph(f"<b>{h_name}</b>", title_style),
            Paragraph(f"<b>CLINICAL VISIT SUMMARY & RX</b><br/><font size=7 color='#64748b'>Encounter #{encounter_data.get('id', 'N/A')}</font>", ParagraphStyle("HdrRight", parent=title_style, alignment=2, fontSize=11, leading=14)),
        ],
        [
            Paragraph(f"{h_address} • Tel: {h_phone} • Emergency: {h_emergency}", sub_title_style),
            Paragraph(f"Date: {encounter_data.get('date', datetime.now().strftime('%Y-%m-%d %H:%M'))}", ParagraphStyle("DateRight", parent=sub_title_style, alignment=2)),
        ],
    ]
    header_table = Table(header_table_data, colWidths=[360, 180])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#0d9488"), spaceAfter=8, spaceBefore=0))

    # 2. Patient & Encounter Box
    p_name = patient_data.get("name", "Unknown")
    p_mrn = patient_data.get("mrn", "N/A")
    p_dob = patient_data.get("dob", "N/A")
    p_gender = patient_data.get("gender", "N/A")
    p_blood = patient_data.get("blood_group", "N/A")
    p_allergies = patient_data.get("allergies", "No known drug allergies")

    doc_name = doctor_data.get("name", "Attending Physician")
    doc_dept = doctor_data.get("department", "General Medicine")

    meta_table_data = [
        [
            Paragraph(f"<b>Patient:</b> {p_name}", bold_style),
            Paragraph(f"<b>MRN:</b> <font color='#0d9488'>{p_mrn}</font>", bold_style),
            Paragraph(f"<b>Physician:</b> {doc_name}", bold_style),
        ],
        [
            Paragraph(f"<b>DOB / Gender:</b> {p_dob} ({p_gender})", body_style),
            Paragraph(f"<b>Blood Group:</b> {p_blood}", body_style),
            Paragraph(f"<b>Department:</b> {doc_dept}", body_style),
        ],
        [
            Paragraph(f"<b>Allergies:</b> <font color='#b91c1c'><b>{p_allergies or 'No known drug allergies'}</b></font>", alert_style if p_allergies else body_style),
            Paragraph(f"<b>Chief Complaint:</b> {encounter_data.get('chief_complaint', 'Routine Consultation')}", body_style),
            Paragraph(f"<b>Status:</b> {encounter_data.get('status', 'Completed')}", body_style),
        ],
    ]
    meta_table = Table(meta_table_data, colWidths=[180, 180, 180])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 8))

    # 3. Vitals Box (if available)
    if vitals_data:
        story.append(Paragraph("VITAL SIGNS", section_heading))
        bp_val = f"{vitals_data.get('systolic', '-')}/{vitals_data.get('diastolic', '-')} mmHg"
        pulse_val = f"{vitals_data.get('pulse', '-')} bpm"
        temp_val = f"{vitals_data.get('temp_c', '-')} °C"
        spo2_val = f"{vitals_data.get('spo2', '-')} %"
        bmi_val = f"{vitals_data.get('bmi', '-')} kg/m²"
        bsa_val = f"{vitals_data.get('bsa', '-')} m²"
        wt_val = f"{vitals_data.get('weight_kg', '-')} kg"
        ht_val = f"{vitals_data.get('height_cm', '-')} cm"

        vitals_table_data = [
            [
                Paragraph("<b>Blood Pressure:</b>", body_style), Paragraph(bp_val, bold_style),
                Paragraph("<b>Pulse / HR:</b>", body_style), Paragraph(pulse_val, bold_style),
                Paragraph("<b>Temperature:</b>", body_style), Paragraph(temp_val, bold_style),
                Paragraph("<b>SpO₂:</b>", body_style), Paragraph(spo2_val, bold_style),
            ],
            [
                Paragraph("<b>Height:</b>", body_style), Paragraph(ht_val, body_style),
                Paragraph("<b>Weight:</b>", body_style), Paragraph(wt_val, body_style),
                Paragraph("<b>BMI:</b>", body_style), Paragraph(bmi_val, bold_style),
                Paragraph("<b>BSA:</b>", body_style), Paragraph(bsa_val, body_style),
            ],
        ]
        v_table = Table(vitals_table_data, colWidths=[80, 55, 75, 55, 75, 55, 75, 70])
        v_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0fdfa")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#99f6e4")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#ccfbf1")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(v_table)
        story.append(Spacer(1, 8))

    # 4. Diagnoses (ICD-10)
    story.append(Paragraph("DIAGNOSES (ICD-10)", section_heading))
    if diagnoses:
        dx_rows = [
            [Paragraph("<b>Code</b>", bold_style), Paragraph("<b>Description</b>", bold_style), Paragraph("<b>Type</b>", bold_style)]
        ]
        for d in diagnoses:
            is_prim = d.get("is_primary", False)
            tag = "<font color='#0d9488'><b>Primary</b></font>" if is_prim else "Secondary"
            dx_rows.append([
                Paragraph(f"<b>{d.get('icd10_code', '')}</b>", bold_style),
                Paragraph(d.get("description", ""), body_style),
                Paragraph(tag, body_style),
            ])
        dx_table = Table(dx_rows, colWidths=[70, 390, 80])
        dx_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(dx_table)
    else:
        story.append(Paragraph("<i>No diagnostic codes specified for this encounter.</i>", body_style))
    story.append(Spacer(1, 8))

    # 5. Clinical SOAP Note Summary
    if clinical_note:
        story.append(Paragraph("CLINICAL NOTES (SOAP)", section_heading))
        note_rows = []
        if clinical_note.get("subjective"):
            note_rows.append([Paragraph("<b>S (Subjective):</b>", bold_style), Paragraph(clinical_note["subjective"], body_style)])
        if clinical_note.get("objective"):
            note_rows.append([Paragraph("<b>O (Objective):</b>", bold_style), Paragraph(clinical_note["objective"], body_style)])
        if clinical_note.get("assessment"):
            note_rows.append([Paragraph("<b>A (Assessment):</b>", bold_style), Paragraph(clinical_note["assessment"], body_style)])
        if clinical_note.get("plan"):
            note_rows.append([Paragraph("<b>P (Plan):</b>", bold_style), Paragraph(clinical_note["plan"], body_style)])

        if note_rows:
            soap_table = Table(note_rows, colWidths=[100, 440])
            soap_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(soap_table)

        # Addenda if note is signed
        addenda = clinical_note.get("addenda", [])
        if addenda:
            story.append(Spacer(1, 4))
            story.append(Paragraph("<b>Addenda:</b>", bold_style))
            for add in addenda:
                add_text = f"• [{add.get('timestamp', '')}] <i>{add.get('author', 'Physician')}:</i> {add.get('text', '')}"
                story.append(Paragraph(add_text, body_style))
        story.append(Spacer(1, 8))

    # 6. Prescriptions (Rx)
    story.append(Paragraph("MEDICATIONS PRESCRIBED (Rx)", section_heading))
    if prescriptions:
        rx_rows = [
            [
                Paragraph("<b>Medication</b>", bold_style),
                Paragraph("<b>Dosage</b>", bold_style),
                Paragraph("<b>Frequency</b>", bold_style),
                Paragraph("<b>Route</b>", bold_style),
                Paragraph("<b>Duration</b>", bold_style),
                Paragraph("<b>Qty</b>", bold_style),
                Paragraph("<b>Instructions</b>", bold_style),
            ]
        ]
        for rx in prescriptions:
            med_text = f"<b>{rx.get('drug_name', '')}</b>"
            if rx.get("generic_name") and rx.get("generic_name") != rx.get("drug_name"):
                med_text += f"<br/><font size=7 color='#64748b'>({rx.get('generic_name')})</font>"
            rx_rows.append([
                Paragraph(med_text, body_style),
                Paragraph(rx.get("dosage", ""), body_style),
                Paragraph(rx.get("frequency", ""), body_style),
                Paragraph(rx.get("route", "Oral"), body_style),
                Paragraph(f"{rx.get('duration_days', '')} days", body_style),
                Paragraph(str(rx.get("quantity", "")), bold_style),
                Paragraph(rx.get("instructions", "As directed"), body_style),
            ])
        rx_table = Table(rx_rows, colWidths=[130, 60, 65, 50, 55, 40, 140])
        rx_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0fdfa")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#99f6e4")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#ccfbf1")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(rx_table)
    else:
        story.append(Paragraph("<i>No medications prescribed during this visit.</i>", body_style))
    story.append(Spacer(1, 8))

    # 7. Diagnostic Orders (Lab / Imaging)
    if orders:
        story.append(Paragraph("INVESTIGATIONS & ORDERS REQUESTED", section_heading))
        order_rows = [
            [Paragraph("<b>Type</b>", bold_style), Paragraph("<b>Test Name</b>", bold_style), Paragraph("<b>Priority</b>", bold_style), Paragraph("<b>Status</b>", bold_style)]
        ]
        for o in orders:
            prio = o.get("priority", "Routine")
            prio_color = "#b91c1c" if prio == "Stat" else ("#d97706" if prio == "Urgent" else "#0f172a")
            order_rows.append([
                Paragraph(o.get("type", "Lab"), body_style),
                Paragraph(f"<b>{o.get('test_name', '')}</b>", body_style),
                Paragraph(f"<font color='{prio_color}'><b>{prio}</b></font>", body_style),
                Paragraph(o.get("status", "Placed"), body_style),
            ])
        order_table = Table(order_rows, colWidths=[80, 260, 100, 100])
        order_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f8fafc")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(order_table)
        story.append(Spacer(1, 8))

    # 8. Follow-up & Sign-off footer
    follow_text = f"Follow-up: Recommended on <b>{follow_up.get('date', 'As needed')}</b>. {follow_up.get('notes', '')}" if follow_up else "Follow-up as needed or in 2 weeks."
    signed_block = [
        [
            Paragraph(f"<b>Instructions & Follow-Up:</b><br/>{follow_text}", body_style),
            Paragraph(
                f"<b>Attending Physician Signature:</b><br/><br/>"
                f"<font size=10><b>{doc_name}</b></font><br/>"
                f"<font size=8 color='#64748b'>{doc_dept} • Reg #{doctor_data.get('license', 'MC-LIC-4029')}</font>",
                ParagraphStyle("SigRight", parent=body_style, alignment=2),
            ),
        ]
    ]
    sign_table = Table(signed_block, colWidths=[340, 200])
    sign_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(KeepTogether([
        sign_table,
        Spacer(1, 8),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cbd5e1"), spaceAfter=4, spaceBefore=4),
        Paragraph(
            "MediCore Clinical Information System • Electronic Health Record (EHR) • Valid without manual stamp if cryptographically verified.",
            disclaimer_style,
        ),
    ]))

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
