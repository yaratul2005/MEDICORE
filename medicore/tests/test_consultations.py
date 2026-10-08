import json
from datetime import date, datetime, timedelta, timezone
import pytest
from sqlmodel import Session, select

from medicore.core.database import engine
from medicore.core.events import event_bus
from medicore.core.models import AuditLog, User
from medicore.modules.appointments.models import Appointment, Doctor, QueueToken
from medicore.modules.consultations.models import (
    ClinicalNote,
    Diagnosis,
    Encounter,
    Order,
    Prescription,
    PrescriptionItem,
    Vitals,
)
from medicore.modules.consultations.service import (
    calculate_bmi,
    calculate_bsa,
    calculate_quantity,
    check_prescription_safety,
    expand_text_shortcuts,
    generate_visit_summary_pdf,
    get_vitals_warnings,
    search_drugs,
    search_icd10,
)
from medicore.modules.patients.models import Patient


def test_vitals_math_and_clinical_warnings():
    """Verify BMI and BSA math formulas and out-of-range clinical warnings"""
    # 70 kg, 175 cm -> BMI = 70 / (1.75^2) = 22.9
    bmi = calculate_bmi(70.0, 175.0)
    assert bmi == 22.9

    # Mosteller BSA = sqrt(175 * 70 / 3600) = sqrt(3.40277) = 1.84
    bsa = calculate_bsa(70.0, 175.0)
    assert bsa == 1.84

    # None handling
    assert calculate_bmi(None, 175.0) is None
    assert calculate_bsa(70.0, 0.0) is None

    # Out of range warnings
    warnings_norm = get_vitals_warnings(systolic=118, diastolic=76, pulse=72, temp_c=36.8, spo2=98)
    assert len(warnings_norm) == 0

    warnings_crit = get_vitals_warnings(systolic=150, diastolic=96, pulse=112, temp_c=38.6, spo2=90, resp_rate=26)
    labels = [w["label"] for w in warnings_crit]
    assert "Stage 2 Hypertension" in labels
    assert "Tachycardia" in labels
    assert "High Fever" in labels
    assert "Critical Hypoxia" in labels
    assert "Tachypnea" in labels


def test_soap_text_shortcuts_expansion():
    """Verify clinical text shortcuts expand cleanly"""
    raw_text = "Patient evaluated today. .htn .norm .fu2w"
    expanded = expand_text_shortcuts(raw_text)
    assert "Essential (primary) hypertension" in expanded
    assert "Comprehensive physical examination unremarkable" in expanded
    assert "Follow-up consultation advised in 2 weeks" in expanded


def test_quantity_calculation_from_frequency():
    """Verify automated prescription quantity calculation"""
    assert calculate_quantity("OD", 10) == 10
    assert calculate_quantity("BID", 7) == 14
    assert calculate_quantity("TID", 5) == 15
    assert calculate_quantity("QDS", 7) == 28


def test_icd10_and_drug_search():
    """Verify searchable reference datasets"""
    icd_results = search_icd10("hypertension")
    assert any(r["code"] == "I10" for r in icd_results)

    drug_results = search_drugs("Amoxil")
    assert any("Amoxicillin" in d["generic_name"] for d in drug_results)

    drug_gen_results = search_drugs("Metformin")
    assert any("Glucophage" in d["brand_name"] for d in drug_gen_results)


def test_pluggable_safety_rules_engine():
    """
    Verify safety rules engine:
    1. Allergy match
    2. Duplicate therapy
    3. Drug-Drug interaction
    """
    # 1. Allergy check: Patient allergic to Penicillin prescribed Amoxil
    alerts_allergy = check_prescription_safety(
        patient_allergies="Penicillin, Peanuts",
        candidate_drug_name="Amoxil",
        candidate_generic="Amoxicillin",
        existing_items=[],
    )
    assert len(alerts_allergy) > 0
    assert alerts_allergy[0]["type"] == "Allergy Match"
    assert alerts_allergy[0]["severity"] == "Major"

    # 2. Duplicate therapy: Candidate Advil (Ibuprofen) with existing Naproxen (both NSAIDs)
    alerts_duplicate = check_prescription_safety(
        patient_allergies=None,
        candidate_drug_name="Advil / Motrin",
        candidate_generic="Ibuprofen",
        existing_items=[{"drug_name": "Aleve", "generic_name": "Naproxen"}],
    )
    assert len(alerts_duplicate) > 0
    assert any(a["type"] in ["Duplicate Therapy", "Duplicate Class Therapy"] for a in alerts_duplicate)

    # 3. Drug-Drug interaction: Warfarin + Aspirin (Major bleeding risk)
    alerts_ddi = check_prescription_safety(
        patient_allergies=None,
        candidate_drug_name="Ecotrin",
        candidate_generic="Aspirin",
        existing_items=[{"drug_name": "Coumadin", "generic_name": "Warfarin"}],
    )
    assert len(alerts_ddi) > 0
    assert alerts_ddi[0]["type"] == "Drug-Drug Interaction"
    assert "Bleeding" in alerts_ddi[0]["message"]


def test_start_consultation_lifecycle(client):
    """
    Test initiating a consultation from an appointment:
    - Sets appointment status to In-Consultation
    - Sets QueueToken to Serving
    - Creates Encounter and emits encounter.started
    """
    with Session(engine) as session:
        doctor = session.exec(select(Doctor)).first()
        patient = session.exec(select(Patient)).first()

        today_str = date.today().strftime("%Y-%m-%d")
        now_utc = datetime.now(timezone.utc)

        # Clean up any appointment at test slot to respect strict DB unique index
        existing_appts = session.exec(
            select(Appointment).where(
                Appointment.doctor_id == doctor.id,
                Appointment.appointment_date == today_str,
                Appointment.start_time == "06:10",
            )
        ).all()
        for ea in existing_appts:
            session.delete(ea)
        session.commit()

        appt = Appointment(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            department=doctor.department,
            appointment_date=today_str,
            start_time="06:10",
            end_time="06:30",
            start_datetime=now_utc,
            end_datetime=now_utc + timedelta(minutes=20),
            type="Consultation",
            status="Checked-in",
            source="Reception",
            checked_in_at=now_utc,
        )
        session.add(appt)
        session.commit()
        session.refresh(appt)

        token = QueueToken(
            token_number="C-999",
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            department=doctor.department,
            appointment_id=appt.id,
            date=today_str,
            status="Waiting",
        )
        session.add(token)
        session.commit()
        appt_id = appt.id
        target_patient_id = patient.id

    events_captured = []
    def handler(ev):
        events_captured.append(ev)
    event_bus.subscribe("encounter.started", handler)

    resp = client.post("/consultations/start", data={"appointment_id": appt_id})
    assert resp.status_code == 200
    assert "HX-Redirect" in resp.headers
    redirect_url = resp.headers["HX-Redirect"]
    assert "/consultations/encounter/" in redirect_url

    enc_id = int(redirect_url.split("/")[-1])

    # Verify DB state
    with Session(engine) as session:
        refreshed_appt = session.get(Appointment, appt_id)
        assert refreshed_appt.status == "In-Consultation"
        assert refreshed_appt.consultation_started_at is not None

        refreshed_token = session.exec(select(QueueToken).where(QueueToken.appointment_id == appt_id)).first()
        assert refreshed_token.status == "Serving"

        enc = session.get(Encounter, enc_id)
        assert enc.status == "In-Progress"
        assert enc.patient_id == target_patient_id

    assert any(ev.payload.get("id") == enc_id for ev in events_captured)
    event_bus.unsubscribe("encounter.started", handler)


def test_soap_note_autosave_signing_and_addenda(client):
    """
    Test SOAP note lifecycle:
    1. Autosave draft
    2. Signing makes note immutable (future edits rejected with 400)
    3. Adding addendum to signed note
    """
    with Session(engine) as session:
        patient = session.exec(select(Patient)).first()
        doctor = session.exec(select(Doctor)).first()
        enc = Encounter(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            status="In-Progress",
            started_at=datetime.now(timezone.utc),
        )
        session.add(enc)
        session.commit()
        session.refresh(enc)
        enc_id = enc.id

    # 1. Autosave draft
    save_resp = client.post(
        f"/consultations/encounter/{enc_id}/note/save",
        data={
            "specialty": "General Medicine",
            "subjective": "Patient reports severe migraine for 2 days.",
            "objective": ".norm",
            "assessment": "Acute migraine without aura.",
            "plan": "Analgesic and hydration.",
        },
    )
    assert save_resp.status_code == 200
    assert "Autosaved at" in save_resp.text

    with Session(engine) as session:
        note = session.exec(select(ClinicalNote).where(ClinicalNote.encounter_id == enc_id)).first()
        assert note is not None
        assert "Comprehensive physical examination unremarkable" in note.objective
        assert not note.is_signed

    # 2. Sign Note
    sign_resp = client.post(f"/consultations/encounter/{enc_id}/note/sign")
    assert sign_resp.status_code == 200
    assert "Digitally signed" in sign_resp.text
    assert "Legal Record Locked" in sign_resp.text

    with Session(engine) as session:
        signed_note = session.exec(select(ClinicalNote).where(ClinicalNote.encounter_id == enc_id)).first()
        assert signed_note.is_signed
        assert signed_note.signed_by is not None

    # 3. Attempt edit on signed note -> Must be rejected (400)
    edit_resp = client.post(
        f"/consultations/encounter/{enc_id}/note/save",
        data={"subjective": "Attempted malicious modification"},
    )
    assert edit_resp.status_code == 400
    assert "immutable" in edit_resp.text.lower()

    # 4. Add addendum
    addendum_resp = client.post(
        f"/consultations/encounter/{enc_id}/note/addendum",
        data={"addendum_text": "Follow-up telephone check confirmed headache resolution."},
    )
    assert addendum_resp.status_code == 200
    assert "Follow-up telephone check" in addendum_resp.text


def test_prescription_safety_warning_and_override_enforcement(client):
    """
    Test E-Prescription safety checks:
    - Attempting to add contraindicated drug without override reason is blocked with 409 Conflict.
    - Supplying clinical override reason permits prescription, logs to audit trail, and emits event.
    """
    with Session(engine) as session:
        # Patient with Penicillin allergy
        patient = session.exec(select(Patient).where(Patient.mrn == "MC-TEST-ALLERGY")).first()
        if not patient:
            patient = Patient(
                mrn="MC-TEST-ALLERGY",
                first_name="Arthur",
                last_name="Pendelton",
                date_of_birth="1980-05-15",
                gender="Male",
                phone="555-0199",
                allergies="Penicillin",
            )
            session.add(patient)
            session.commit()
            session.refresh(patient)
        else:
            patient.allergies = "Penicillin"
            session.add(patient)
            session.commit()

        doctor = session.exec(select(Doctor)).first()
        enc = Encounter(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            status="In-Progress",
            started_at=datetime.now(timezone.utc),
        )
        session.add(enc)
        session.commit()
        session.refresh(enc)
        enc_id = enc.id

    # 1. Attempt to prescribe Amoxicillin (Penicillin class) without override reason -> 409 Conflict
    blocked_resp = client.post(
        f"/consultations/encounter/{enc_id}/prescriptions/add",
        data={
            "drug_name": "Amoxil",
            "generic_name": "Amoxicillin",
            "dosage": "500 mg",
            "frequency": "TID",
            "route": "Oral",
            "duration_days": 7,
        },
    )
    assert blocked_resp.status_code == 409
    assert "Clinical Safety Decision Warning" in blocked_resp.text
    assert "Allergy Match" in blocked_resp.text

    # 2. Prescribe with mandatory clinical override justification
    override_events = []
    def override_sub(ev):
        override_events.append(ev)
    event_bus.subscribe("prescription.safety_overridden", override_sub)

    allowed_resp = client.post(
        f"/consultations/encounter/{enc_id}/prescriptions/add",
        data={
            "drug_name": "Amoxil",
            "generic_name": "Amoxicillin",
            "dosage": "500 mg",
            "frequency": "TID",
            "route": "Oral",
            "duration_days": 7,
            "override_reason": "Patient desensitization protocol completed; supervised administration in clinic.",
        },
    )
    assert allowed_resp.status_code == 200
    assert "Amoxil" in allowed_resp.text
    assert "Safety Override on File" in allowed_resp.text

    # Verify AuditLog recorded
    with Session(engine) as session:
        audit = session.exec(
            select(AuditLog)
            .where(
                AuditLog.module == "consultations",
                AuditLog.action == "SAFETY_OVERRIDE",
            )
        ).first()
        assert audit is not None
        assert "desensitization protocol" in audit.changes.get("override_reason", "")

    assert len(override_events) > 0
    event_bus.unsubscribe("prescription.safety_overridden", override_sub)


def test_order_placement_and_event_emission(client):
    """Verify placing diagnostic order creates record and emits order.placed event"""
    with Session(engine) as session:
        patient = session.exec(select(Patient)).first()
        doctor = session.exec(select(Doctor)).first()
        enc = Encounter(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            status="In-Progress",
            started_at=datetime.now(timezone.utc),
        )
        session.add(enc)
        session.commit()
        session.refresh(enc)
        enc_id = enc.id

    order_events = []
    def order_sub(ev):
        order_events.append(ev)
    event_bus.subscribe("order.placed", order_sub)

    resp = client.post(
        f"/consultations/encounter/{enc_id}/orders",
        data={
            "type": "Lab",
            "test_name": "Complete Blood Count (CBC)",
            "priority": "Urgent",
        },
    )
    assert resp.status_code == 200
    assert "Complete Blood Count (CBC)" in resp.text
    assert "Urgent" in resp.text

    assert any(ev.payload.get("test_name") == "Complete Blood Count (CBC)" for ev in order_events)
    event_bus.unsubscribe("order.placed", order_sub)


def test_encounter_completion_and_billing_event(client):
    """
    Verify completing an encounter:
    - Sets encounter status to Completed
    - Sets linked appointment to Completed
    - Emits encounter.completed event
    """
    with Session(engine) as session:
        doctor = session.exec(select(Doctor)).first()
        patient = session.exec(select(Patient)).first()
        today_str = date.today().strftime("%Y-%m-%d")
        now_utc = datetime.now(timezone.utc)

        # Clean up any appointment at test slot
        existing_appts = session.exec(
            select(Appointment).where(
                Appointment.doctor_id == doctor.id,
                Appointment.appointment_date == today_str,
                Appointment.start_time == "06:40",
            )
        ).all()
        for ea in existing_appts:
            session.delete(ea)
        session.commit()

        appt = Appointment(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            department=doctor.department,
            appointment_date=today_str,
            start_time="06:40",
            end_time="07:00",
            start_datetime=now_utc,
            end_datetime=now_utc + timedelta(minutes=20),
            type="Consultation",
            status="In-Consultation",
            source="Reception",
            checked_in_at=now_utc,
            consultation_started_at=now_utc,
        )
        session.add(appt)
        session.commit()
        session.refresh(appt)

        enc = Encounter(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            appointment_id=appt.id,
            status="In-Progress",
            started_at=now_utc,
        )
        session.add(enc)
        session.commit()
        session.refresh(enc)
        enc_id = enc.id
        appt_id = appt.id

    comp_events = []
    def comp_sub(ev):
        comp_events.append(ev)
    event_bus.subscribe("encounter.completed", comp_sub)

    resp = client.post(f"/consultations/encounter/{enc_id}/complete")
    assert resp.status_code == 200
    assert "HX-Redirect" in resp.headers

    with Session(engine) as session:
        enc_check = session.get(Encounter, enc_id)
        assert enc_check.status == "Completed"
        assert enc_check.completed_at is not None

        appt_check = session.get(Appointment, appt_id)
        assert appt_check.status == "Completed"
        assert appt_check.completed_at is not None

    assert any(ev.payload.get("id") == enc_id for ev in comp_events)
    event_bus.unsubscribe("encounter.completed", comp_sub)


def test_pdf_visit_summary_generation():
    """Verify ReportLab PDF generation outputs valid PDF bytes"""
    pdf_bytes = generate_visit_summary_pdf(
        hospital_info={"name": "MediCore Test Hospital", "address": "123 Test St", "phone": "555-1234"},
        encounter_data={"id": 1, "date": "2026-10-08 14:00", "chief_complaint": "Migraine", "status": "Completed"},
        patient_data={"name": "Jane Doe", "mrn": "MC-TEST-001", "dob": "1990-01-01", "gender": "Female", "blood_group": "O+"},
        doctor_data={"name": "Dr. Sarah Chen", "department": "Cardiology"},
        vitals_data={"systolic": 120, "diastolic": 80, "pulse": 72, "temp_c": 36.8, "spo2": 99, "bmi": 22.4, "bsa": 1.7},
        diagnoses=[{"icd10_code": "I10", "description": "Essential hypertension", "is_primary": True}],
        clinical_note={"subjective": "Normal checkup", "objective": "Normal", "assessment": "Stable", "plan": "Maintain lifestyle"},
        prescriptions=[{"drug_name": "Amlodipine", "dosage": "5 mg", "frequency": "OD", "route": "Oral", "duration_days": 30, "quantity": 30, "instructions": "Morning"}],
        orders=[{"type": "Lab", "test_name": "Lipid Panel", "priority": "Routine", "status": "Placed"}],
        follow_up={"date": "2026-11-08", "notes": "Check BP"},
    )
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1000


def test_patient_360_clinical_tabs_wired(client):
    """Verify Patient 360 endpoints return real visits, vitals, and prescriptions"""
    with Session(engine) as session:
        # Find a patient that has an encounter
        enc = session.exec(select(Encounter)).first()
        assert enc is not None
        p_id = enc.patient_id

    # Visits tab endpoint
    resp_visits = client.get(f"/consultations/patient/{p_id}/visits")
    assert resp_visits.status_code == 200
    assert "Clinical Visits & Encounters" in resp_visits.text

    # Vitals tab endpoint
    resp_vitals = client.get(f"/consultations/patient/{p_id}/vitals")
    assert resp_vitals.status_code == 200
    assert "Patient Vitals History & Trends" in resp_vitals.text

    # Prescriptions tab endpoint
    resp_rx = client.get(f"/consultations/patient/{p_id}/prescriptions")
    assert resp_rx.status_code == 200
    assert "Medications & Prescriptions" in resp_rx.text
