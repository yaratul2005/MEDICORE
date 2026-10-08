from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, Response
from sqlmodel import Session, col, or_, select

from medicore.core.database import get_session
from medicore.core.events import event_bus
from medicore.core.models import AuditLog, User
from medicore.core.security import get_current_user, require_permission, user_has_permission
from medicore.core.settings import settings_registry
from medicore.modules.appointments.models import Appointment, Doctor, QueueToken
from medicore.modules.consultations.models import (
    ClinicalNote,
    Diagnosis,
    Encounter,
    FollowUp,
    Order,
    Prescription,
    PrescriptionItem,
    Referral,
    Vitals,
)
from medicore.modules.consultations.schema import encounter_schema
from medicore.modules.consultations.service import (
    SOAP_TEMPLATES,
    TEXT_SHORTCUTS,
    calculate_bmi,
    calculate_bsa,
    calculate_quantity,
    check_prescription_safety,
    expand_text_shortcuts,
    generate_sparkline,
    generate_visit_summary_pdf,
    get_vitals_warnings,
    load_frequencies,
    search_drugs,
    search_icd10,
)
from medicore.modules.patients.models import Patient
from medicore.ui.templating import get_ui_context, templates

router = APIRouter(prefix="/consultations", tags=["Consultations"])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("", response_class=HTMLResponse)
def doctor_queue_index(
    request: Request,
    user: User = Depends(require_permission("consultations.encounter.read")),
    session: Session = Depends(get_session),
    tab: str = Query("waiting"),
    q: Optional[str] = Query(None),
    doctor_filter: Optional[int] = Query(None),
):
    today_str = date.today().strftime("%Y-%m-%d")

    # 1. Waiting Queue: Appointments checked in today, waiting to be seen
    waiting_stmt = (
        select(Appointment)
        .where(
            Appointment.appointment_date == today_str,
            Appointment.status == "Checked-in",
        )
        .order_by(Appointment.start_time)
    )
    if doctor_filter:
        waiting_stmt = waiting_stmt.where(Appointment.doctor_id == doctor_filter)
    waiting_appointments = session.exec(waiting_stmt).all()

    # 2. In-Progress Encounters
    in_progress_stmt = (
        select(Encounter)
        .where(Encounter.status == "In-Progress")
        .order_by(col(Encounter.started_at).desc())
    )
    if doctor_filter:
        in_progress_stmt = in_progress_stmt.where(Encounter.doctor_id == doctor_filter)
    in_progress_encounters = session.exec(in_progress_stmt).all()

    # 3. Completed Encounters Today
    completed_stmt = (
        select(Encounter)
        .where(
            Encounter.status == "Completed",
            Encounter.completed_at >= datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc),
        )
        .order_by(col(Encounter.completed_at).desc())
    )
    if doctor_filter:
        completed_stmt = completed_stmt.where(Encounter.doctor_id == doctor_filter)
    completed_encounters = session.exec(completed_stmt).all()

    # Doctors list for filter
    doctors = session.exec(select(Doctor).where(Doctor.is_active == True)).all()

    # Total counts
    stats = {
        "waiting_count": len(waiting_appointments),
        "in_progress_count": len(in_progress_encounters),
        "completed_count": len(completed_encounters),
    }

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        tab=tab,
        waiting_appointments=waiting_appointments,
        in_progress_encounters=in_progress_encounters,
        completed_encounters=completed_encounters,
        doctors=doctors,
        selected_doctor_id=doctor_filter,
        stats=stats,
        today_date=today_str,
    )
    return templates.TemplateResponse("modules/consultations/index.html", ctx)


@router.post("/start", response_class=HTMLResponse)
def start_consultation(
    request: Request,
    appointment_id: Optional[int] = Form(None),
    patient_id: Optional[int] = Form(None),
    doctor_id: Optional[int] = Form(None),
    chief_complaint: Optional[str] = Form(None),
    user: User = Depends(require_permission("consultations.encounter.start")),
    session: Session = Depends(get_session),
):
    """
    Starts an encounter from the checked-in queue or patient detail:
    - Finds or creates Encounter
    - Sets appointment status to 'In-Consultation'
    - Sets queue token status to 'Serving'
    - Emits 'encounter.started'
    """
    patient: Optional[Patient] = None
    doctor: Optional[Doctor] = None
    appt: Optional[Appointment] = None

    if appointment_id:
        appt = session.get(Appointment, appointment_id)
        if not appt:
            raise HTTPException(status_code=404, detail="Appointment not found")
        patient = session.get(Patient, appt.patient_id)
        doctor = session.get(Doctor, appt.doctor_id)
    elif patient_id:
        patient = session.get(Patient, patient_id)
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")
        if doctor_id:
            doctor = session.get(Doctor, doctor_id)
        else:
            doctor = session.exec(select(Doctor).where(Doctor.is_active == True)).first()

    if not patient or not doctor:
        raise HTTPException(status_code=400, detail="Patient and Doctor are required to initiate consultation")

    # Check if encounter already active for this appointment
    encounter: Optional[Encounter] = None
    if appt:
        encounter = session.exec(
            select(Encounter).where(
                Encounter.appointment_id == appt.id,
                Encounter.status == "In-Progress",
            )
        ).first()

    if not encounter:
        encounter = Encounter(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            appointment_id=appt.id if appt else None,
            status="In-Progress",
            department=doctor.department,
            chief_complaint=chief_complaint or (appt.notes if appt else "Outpatient Consultation"),
            started_at=utc_now(),
        )
        session.add(encounter)
        session.commit()
        session.refresh(encounter)

        # Create blank clinical note draft for this encounter
        note = ClinicalNote(
            encounter_id=encounter.id,
            specialty=doctor.department or "General Medicine",
            subjective="",
            objective="",
            assessment="",
            plan="",
            is_signed=False,
        )
        session.add(note)

        # Create active prescription container
        rx = Prescription(
            encounter_id=encounter.id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            status="Active",
        )
        session.add(rx)
        session.commit()

    # Update appointment status to In-Consultation
    if appt:
        appt.status = "In-Consultation"
        if not appt.consultation_started_at:
            appt.consultation_started_at = utc_now()
        session.add(appt)

        # Update queue token
        token = session.exec(
            select(QueueToken).where(QueueToken.appointment_id == appt.id)
        ).first()
        if token:
            token.status = "Serving"
            token.called_at = utc_now()
            session.add(token)

        session.commit()

    # Emit domain event
    event_bus.emit(
        "encounter.started",
        {
            "id": encounter.id,
            "patient_id": patient.id,
            "patient_mrn": patient.mrn,
            "patient_name": patient.full_name,
            "doctor_id": doctor.id,
            "doctor_name": doctor.name,
            "appointment_id": appt.id if appt else None,
        },
        user_id=user.id,
        username=user.username,
        ip_address=request.client.host if request.client else None,
    )

    # Return HTMX redirect header
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = f"/consultations/encounter/{encounter.id}"
    return response


@router.get("/encounter/{encounter_id}", response_class=HTMLResponse)
def consultation_workspace(
    encounter_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.encounter.read")),
    session: Session = Depends(get_session),
):
    """
    3-Pane Consultation (OPD/EMR) Screen
    - Pane 1: Patient History / Timeline & Vitals Sparklines
    - Pane 2: Note Editor (SOAP, Specialty Templates, Shortcuts, Signing, Addenda)
    - Pane 3: Diagnoses (ICD-10), E-Prescription, Orders, Follow-up, Referral
    """
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    patient = session.get(Patient, encounter.patient_id)
    doctor = session.get(Doctor, encounter.doctor_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient record not found")

    # 1. Vitals data
    patient_vitals = session.exec(
        select(Vitals).where(Vitals.patient_id == patient.id).order_by(col(Vitals.recorded_at).desc())
    ).all()
    latest_vitals = patient_vitals[0] if patient_vitals else None
    vitals_warnings = get_vitals_warnings(
        systolic=latest_vitals.systolic if latest_vitals else None,
        diastolic=latest_vitals.diastolic if latest_vitals else None,
        pulse=latest_vitals.pulse if latest_vitals else None,
        temp_c=latest_vitals.temp_c if latest_vitals else None,
        spo2=latest_vitals.spo2 if latest_vitals else None,
        resp_rate=latest_vitals.resp_rate if latest_vitals else None,
    ) if latest_vitals else []

    # Historical sparklines
    rev_vitals = list(reversed(patient_vitals[:10]))
    sparklines = {
        "systolic": generate_sparkline([float(v.systolic) for v in rev_vitals if v.systolic is not None], stroke_color="#e11d48"),
        "pulse": generate_sparkline([float(v.pulse) for v in rev_vitals if v.pulse is not None], stroke_color="#0284c7"),
        "spo2": generate_sparkline([float(v.spo2) for v in rev_vitals if v.spo2 is not None], stroke_color="#0d9488"),
        "temp": generate_sparkline([float(v.temp_c) for v in rev_vitals if v.temp_c is not None], stroke_color="#f59e0b"),
    }

    # 2. Past Encounters & Timeline
    past_encounters = session.exec(
        select(Encounter)
        .where(Encounter.patient_id == patient.id, Encounter.id != encounter.id)
        .order_by(col(Encounter.started_at).desc())
        .limit(10)
    ).all()

    # 3. Clinical Note
    clinical_note = session.exec(
        select(ClinicalNote).where(ClinicalNote.encounter_id == encounter.id)
    ).first()
    if not clinical_note:
        clinical_note = ClinicalNote(
            encounter_id=encounter.id,
            specialty=encounter.department or "General Medicine",
            is_signed=False,
        )
        session.add(clinical_note)
        session.commit()
        session.refresh(clinical_note)

    # 4. Diagnoses
    diagnoses = session.exec(
        select(Diagnosis).where(Diagnosis.encounter_id == encounter.id).order_by(col(Diagnosis.is_primary).desc())
    ).all()

    # 5. Prescription & Items
    prescription = session.exec(
        select(Prescription).where(Prescription.encounter_id == encounter.id)
    ).first()
    if not prescription:
        prescription = Prescription(
            encounter_id=encounter.id,
            patient_id=patient.id,
            doctor_id=encounter.doctor_id,
            doctor_name=encounter.doctor_name,
            status="Active",
        )
        session.add(prescription)
        session.commit()
        session.refresh(prescription)

    prescription_items = session.exec(
        select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id)
    ).all()

    # 6. Orders
    orders = session.exec(
        select(Order).where(Order.encounter_id == encounter.id).order_by(col(Order.placed_at).desc())
    ).all()

    # 7. Follow-up & Referral
    follow_up = session.exec(
        select(FollowUp).where(FollowUp.encounter_id == encounter.id)
    ).first()
    referral = session.exec(
        select(Referral).where(Referral.encounter_id == encounter.id)
    ).first()

    # Doctor permission checks for UI buttons
    can_write_notes = user_has_permission(user, "consultations.notes.write", session)
    can_sign_notes = user_has_permission(user, "consultations.notes.sign", session)
    can_prescribe = user_has_permission(user, "consultations.prescription.write", session)
    can_enter_vitals = user_has_permission(user, "consultations.vitals.create", session)
    can_order = user_has_permission(user, "consultations.orders.create", session)
    can_complete = user_has_permission(user, "consultations.encounter.complete", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        patient=patient,
        doctor=doctor,
        latest_vitals=latest_vitals,
        patient_vitals=patient_vitals,
        vitals_warnings=vitals_warnings,
        sparklines=sparklines,
        past_encounters=past_encounters,
        clinical_note=clinical_note,
        diagnoses=diagnoses,
        prescription=prescription,
        prescription_items=prescription_items,
        orders=orders,
        follow_up=follow_up,
        referral=referral,
        templates_map=SOAP_TEMPLATES,
        shortcuts_map=TEXT_SHORTCUTS,
        frequencies=load_frequencies(),
        can_write_notes=can_write_notes,
        can_sign_notes=can_sign_notes,
        can_prescribe=can_prescribe,
        can_enter_vitals=can_enter_vitals,
        can_order=can_order,
        can_complete=can_complete,
    )
    return templates.TemplateResponse("modules/consultations/encounter.html", ctx)


# -------------------------------------------------------------
# VITALS ENDPOINTS
# -------------------------------------------------------------


@router.post("/encounter/{encounter_id}/vitals", response_class=HTMLResponse)
def record_vitals(
    encounter_id: int,
    request: Request,
    height_cm: Optional[float] = Form(None),
    weight_kg: Optional[float] = Form(None),
    systolic: Optional[int] = Form(None),
    diastolic: Optional[int] = Form(None),
    pulse: Optional[int] = Form(None),
    temp_c: Optional[float] = Form(None),
    spo2: Optional[float] = Form(None),
    resp_rate: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
    user: User = Depends(require_permission("consultations.vitals.create")),
    session: Session = Depends(get_session),
):
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    bmi = calculate_bmi(weight_kg, height_cm)
    bsa = calculate_bsa(weight_kg, height_cm)

    v = Vitals(
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        recorded_at=utc_now(),
        recorded_by=user.full_name,
        height_cm=height_cm,
        weight_kg=weight_kg,
        bmi=bmi,
        bsa=bsa,
        systolic=systolic,
        diastolic=diastolic,
        pulse=pulse,
        temp_c=temp_c,
        spo2=spo2,
        resp_rate=resp_rate,
        notes=notes,
    )
    session.add(v)
    session.commit()
    session.refresh(v)

    event_bus.emit(
        "vitals.recorded",
        {
            "id": v.id,
            "encounter_id": encounter.id,
            "patient_id": encounter.patient_id,
            "systolic": systolic,
            "diastolic": diastolic,
            "bmi": bmi,
        },
        user_id=user.id,
        username=user.username,
    )

    # Return refreshed Pane 1 timeline
    patient = session.get(Patient, encounter.patient_id)
    patient_vitals = session.exec(
        select(Vitals).where(Vitals.patient_id == patient.id).order_by(col(Vitals.recorded_at).desc())
    ).all()
    latest_vitals = patient_vitals[0] if patient_vitals else None
    vitals_warnings = get_vitals_warnings(
        systolic=latest_vitals.systolic if latest_vitals else None,
        diastolic=latest_vitals.diastolic if latest_vitals else None,
        pulse=latest_vitals.pulse if latest_vitals else None,
        temp_c=latest_vitals.temp_c if latest_vitals else None,
        spo2=latest_vitals.spo2 if latest_vitals else None,
        resp_rate=latest_vitals.resp_rate if latest_vitals else None,
    ) if latest_vitals else []

    rev_vitals = list(reversed(patient_vitals[:10]))
    sparklines = {
        "systolic": generate_sparkline([float(x.systolic) for x in rev_vitals if x.systolic is not None], stroke_color="#e11d48"),
        "pulse": generate_sparkline([float(x.pulse) for x in rev_vitals if x.pulse is not None], stroke_color="#0284c7"),
        "spo2": generate_sparkline([float(x.spo2) for x in rev_vitals if x.spo2 is not None], stroke_color="#0d9488"),
        "temp": generate_sparkline([float(x.temp_c) for x in rev_vitals if x.temp_c is not None], stroke_color="#f59e0b"),
    }
    past_encounters = session.exec(
        select(Encounter)
        .where(Encounter.patient_id == patient.id, Encounter.id != encounter.id)
        .order_by(col(Encounter.started_at).desc())
        .limit(10)
    ).all()

    can_enter_vitals = user_has_permission(user, "consultations.vitals.create", session)
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        patient=patient,
        latest_vitals=latest_vitals,
        patient_vitals=patient_vitals,
        vitals_warnings=vitals_warnings,
        sparklines=sparklines,
        past_encounters=past_encounters,
        can_enter_vitals=can_enter_vitals,
    )
    return templates.TemplateResponse("modules/consultations/partials/pane_timeline.html", ctx)


# -------------------------------------------------------------
# SOAP NOTE ENDPOINTS
# -------------------------------------------------------------


@router.post("/encounter/{encounter_id}/note/save", response_class=HTMLResponse)
def save_soap_note(
    encounter_id: int,
    request: Request,
    specialty: Optional[str] = Form("General Medicine"),
    subjective: Optional[str] = Form(""),
    objective: Optional[str] = Form(""),
    assessment: Optional[str] = Form(""),
    plan: Optional[str] = Form(""),
    user: User = Depends(require_permission("consultations.notes.write")),
    session: Session = Depends(get_session),
):
    note = session.exec(select(ClinicalNote).where(ClinicalNote.encounter_id == encounter_id)).first()
    if not note:
        note = ClinicalNote(encounter_id=encounter_id, specialty=specialty)
        session.add(note)

    if note.is_signed:
        return HTMLResponse("<span class='text-xs text-rose-500 font-semibold'>Note is signed and immutable</span>", status_code=400)

    # Expand shortcuts like .htn, .resp, .dm2
    note.specialty = specialty or note.specialty
    note.subjective = expand_text_shortcuts(subjective or "")
    note.objective = expand_text_shortcuts(objective or "")
    note.assessment = expand_text_shortcuts(assessment or "")
    note.plan = expand_text_shortcuts(plan or "")

    session.add(note)
    session.commit()

    saved_time = datetime.now().strftime("%H:%M:%S")
    return HTMLResponse(
        f"""<div class='flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400 font-medium animate-fade-in'>
            <i data-lucide='check-circle-2' class='w-3.5 h-3.5'></i>
            <span>Autosaved at {saved_time}</span>
           </div><script>lucide.createIcons();</script>"""
    )


@router.post("/encounter/{encounter_id}/note/sign", response_class=HTMLResponse)
def sign_soap_note(
    encounter_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.notes.sign")),
    session: Session = Depends(get_session),
):
    note = session.exec(select(ClinicalNote).where(ClinicalNote.encounter_id == encounter_id)).first()
    if not note:
        raise HTTPException(status_code=404, detail="Clinical note not found")

    if note.is_signed:
        return HTMLResponse("<span class='text-xs text-amber-500'>Note already signed</span>")

    note.is_signed = True
    note.signed_at = utc_now()
    note.signed_by = user.full_name
    note.signed_by_user_id = user.id

    session.add(note)
    session.commit()
    session.refresh(note)

    event_bus.emit(
        "clinical_note.signed",
        {
            "note_id": note.id,
            "encounter_id": encounter_id,
            "signed_by": user.full_name,
            "signed_at": note.signed_at.isoformat(),
        },
        user_id=user.id,
        username=user.username,
    )

    encounter = session.get(Encounter, encounter_id)

    # Check if encounter has an active prescription with items, and emit prescription.signed for Pharmacy
    rx = session.exec(select(Prescription).where(Prescription.encounter_id == encounter_id)).first()
    if rx:
        rx_items = session.exec(select(PrescriptionItem).where(PrescriptionItem.prescription_id == rx.id)).all()
        if rx_items:
            event_bus.emit(
                "prescription.signed",
                {
                    "prescription_id": rx.id,
                    "encounter_id": encounter_id,
                    "patient_id": encounter.patient_id if encounter else None,
                    "patient_mrn": encounter.patient_mrn if encounter else None,
                    "patient_name": encounter.patient_name if encounter else None,
                    "doctor_id": encounter.doctor_id if encounter else None,
                    "doctor_name": encounter.doctor_name if encounter else None,
                    "safety_overrides": rx.safety_overrides,
                    "items": [
                        {
                            "id": item.id,
                            "drug_name": item.drug_name,
                            "generic_name": item.generic_name,
                            "dose": item.dose,
                            "frequency": item.frequency,
                            "duration": item.duration,
                            "route": item.route,
                            "quantity": item.quantity,
                            "instructions": item.instructions,
                        }
                        for item in rx_items
                    ],
                    "signed_at": note.signed_at.isoformat(),
                },
                user_id=user.id,
                username=user.username,
            )

    can_write_notes = user_has_permission(user, "consultations.notes.write", session)
    can_sign_notes = user_has_permission(user, "consultations.notes.sign", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        clinical_note=note,
        templates_map=SOAP_TEMPLATES,
        shortcuts_map=TEXT_SHORTCUTS,
        can_write_notes=can_write_notes,
        can_sign_notes=can_sign_notes,
    )
    return templates.TemplateResponse("modules/consultations/partials/pane_soap.html", ctx)


@router.post("/encounter/{encounter_id}/note/addendum", response_class=HTMLResponse)
def add_note_addendum(
    encounter_id: int,
    request: Request,
    addendum_text: str = Form(...),
    user: User = Depends(require_permission("consultations.notes.write")),
    session: Session = Depends(get_session),
):
    note = session.exec(select(ClinicalNote).where(ClinicalNote.encounter_id == encounter_id)).first()
    if not note:
        raise HTTPException(status_code=404, detail="Clinical note not found")

    clean_text = addendum_text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Addendum text cannot be empty")

    addenda = list(note.addenda or [])
    addenda.append({
        "author": user.full_name,
        "user_id": user.id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "text": clean_text,
    })
    note.addenda = addenda

    session.add(note)
    session.commit()
    session.refresh(note)

    event_bus.emit(
        "clinical_note.addendum_added",
        {
            "note_id": note.id,
            "encounter_id": encounter_id,
            "author": user.full_name,
            "text": clean_text,
        },
        user_id=user.id,
        username=user.username,
    )

    encounter = session.get(Encounter, encounter_id)
    can_write_notes = user_has_permission(user, "consultations.notes.write", session)
    can_sign_notes = user_has_permission(user, "consultations.notes.sign", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        clinical_note=note,
        templates_map=SOAP_TEMPLATES,
        shortcuts_map=TEXT_SHORTCUTS,
        can_write_notes=can_write_notes,
        can_sign_notes=can_sign_notes,
    )
    return templates.TemplateResponse("modules/consultations/partials/pane_soap.html", ctx)


# -------------------------------------------------------------
# ICD-10 DIAGNOSES ENDPOINTS
# -------------------------------------------------------------


@router.get("/search/icd10", response_class=HTMLResponse)
def icd10_search(
    q: str = Query(""),
    user: User = Depends(require_permission("consultations.encounter.read")),
):
    results = search_icd10(q, limit=12)
    html = ""
    for r in results:
        code = r["code"]
        desc = r["description"]
        cat = r.get("category", "")
        html += f"""
        <div class="px-3 py-2 hover:bg-teal-50 dark:hover:bg-teal-950/40 cursor-pointer flex items-center justify-between text-xs border-b border-subtle transition-colors"
             onclick="selectDiagnosis('{code}', '{desc.replace("'", "\\'")}')">
          <div>
            <span class="font-mono font-bold text-teal-600 dark:text-teal-400 mr-2">{code}</span>
            <span class="text-main">{desc}</span>
          </div>
          <span class="text-[10px] text-muted uppercase font-semibold">{cat}</span>
        </div>
        """
    if not results:
        html = "<div class='p-3 text-center text-xs text-muted'>No matching ICD-10 diagnoses found</div>"
    return HTMLResponse(html)


@router.post("/encounter/{encounter_id}/diagnoses", response_class=HTMLResponse)
def add_diagnosis(
    encounter_id: int,
    request: Request,
    icd10_code: str = Form(...),
    description: str = Form(...),
    is_primary: bool = Form(False),
    user: User = Depends(require_permission("consultations.notes.write")),
    session: Session = Depends(get_session),
):
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    # If marking as primary, demote existing primary diagnoses
    if is_primary:
        existing_primaries = session.exec(
            select(Diagnosis).where(Diagnosis.encounter_id == encounter_id, Diagnosis.is_primary == True)
        ).all()
        for p in existing_primaries:
            p.is_primary = False
            session.add(p)

    dx = Diagnosis(
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        icd10_code=icd10_code.strip().upper(),
        description=description.strip(),
        is_primary=is_primary,
    )
    session.add(dx)
    session.commit()

    diagnoses = session.exec(
        select(Diagnosis).where(Diagnosis.encounter_id == encounter_id).order_by(col(Diagnosis.is_primary).desc())
    ).all()
    can_write = user_has_permission(user, "consultations.notes.write", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        diagnoses=diagnoses,
        can_write=can_write,
    )
    return templates.TemplateResponse("modules/consultations/partials/diagnoses_list.html", ctx)


@router.delete("/encounter/{encounter_id}/diagnoses/{dx_id}", response_class=HTMLResponse)
def remove_diagnosis(
    encounter_id: int,
    dx_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.notes.write")),
    session: Session = Depends(get_session),
):
    dx = session.get(Diagnosis, dx_id)
    if dx and dx.encounter_id == encounter_id:
        session.delete(dx)
        session.commit()

    encounter = session.get(Encounter, encounter_id)
    diagnoses = session.exec(
        select(Diagnosis).where(Diagnosis.encounter_id == encounter_id).order_by(col(Diagnosis.is_primary).desc())
    ).all()
    can_write = user_has_permission(user, "consultations.notes.write", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        diagnoses=diagnoses,
        can_write=can_write,
    )
    return templates.TemplateResponse("modules/consultations/partials/diagnoses_list.html", ctx)


# -------------------------------------------------------------
# E-PRESCRIPTION & SAFETY RULES ENGINE ENDPOINTS
# -------------------------------------------------------------


@router.get("/search/drugs", response_class=HTMLResponse)
def drug_search(
    q: str = Query(""),
    user: User = Depends(require_permission("consultations.encounter.read")),
):
    results = search_drugs(q, limit=12)
    html = ""
    for d in results:
        b_name = d["brand_name"]
        g_name = d["generic_name"]
        strn = d.get("strength", "")
        form = d.get("form", "")
        route = d.get("route", "Oral")
        freq = d.get("default_frequency", "OD")
        days = d.get("default_duration_days", 5)
        inst = d.get("default_instructions", "").replace("'", "\\'")
        html += f"""
        <div class="px-3 py-2 hover:bg-teal-50 dark:hover:bg-teal-950/40 cursor-pointer flex items-center justify-between text-xs border-b border-subtle transition-colors"
             onclick="selectDrug('{b_name.replace("'", "\\'")}', '{g_name.replace("'", "\\'")}', '{strn}', '{route}', '{freq}', {days}, '{inst}')">
          <div>
            <span class="font-semibold text-main">{b_name}</span>
            <span class="text-secondary text-[11px] ml-1">({g_name})</span>
            <span class="text-teal-600 dark:text-teal-400 font-mono text-[11px] ml-1.5">{strn}</span>
          </div>
          <span class="text-[10px] text-muted font-mono">{form} • {route}</span>
        </div>
        """
    if not results:
        html = "<div class='p-3 text-center text-xs text-muted'>No matching medications found in Drug Master</div>"
    return HTMLResponse(html)


@router.post("/encounter/{encounter_id}/prescriptions/check", response_class=HTMLResponse)
def check_safety_alert(
    encounter_id: int,
    request: Request,
    drug_name: str = Form(...),
    generic_name: Optional[str] = Form(None),
    user: User = Depends(require_permission("consultations.prescription.write")),
    session: Session = Depends(get_session),
):
    """
    Pluggable Clinical Rules Pre-Flight Check:
    Verifies drug-allergy, duplicate therapy, and drug-drug interactions.
    If conflicts found, returns the Safety Warning Modal HTML.
    If clear, returns HTTP 204 No Content.
    """
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    patient = session.get(Patient, encounter.patient_id)
    prescription = session.exec(
        select(Prescription).where(Prescription.encounter_id == encounter_id)
    ).first()

    existing_items: List[Dict[str, str]] = []
    if prescription:
        items = session.exec(
            select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id)
        ).all()
        for it in items:
            existing_items.append({"drug_name": it.drug_name, "generic_name": it.generic_name or it.drug_name})

    alerts = check_prescription_safety(
        patient_allergies=patient.allergies if patient else None,
        candidate_drug_name=drug_name,
        candidate_generic=generic_name or drug_name,
        existing_items=existing_items,
    )

    if not alerts:
        # No conflict detected
        return Response(status_code=204)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        patient=patient,
        candidate_drug=drug_name,
        candidate_generic=generic_name or drug_name,
        alerts=alerts,
    )
    return templates.TemplateResponse("modules/consultations/partials/safety_alert_modal.html", ctx)


@router.post("/encounter/{encounter_id}/prescriptions/add", response_class=HTMLResponse)
def add_prescription_item(
    encounter_id: int,
    request: Request,
    drug_name: str = Form(...),
    generic_name: Optional[str] = Form(None),
    dosage: str = Form(...),
    frequency: str = Form("OD"),
    route: str = Form("Oral"),
    duration_days: int = Form(5),
    quantity: Optional[int] = Form(None),
    instructions: Optional[str] = Form(None),
    override_reason: Optional[str] = Form(None),
    user: User = Depends(require_permission("consultations.prescription.write")),
    session: Session = Depends(get_session),
):
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    patient = session.get(Patient, encounter.patient_id)
    prescription = session.exec(
        select(Prescription).where(Prescription.encounter_id == encounter_id)
    ).first()
    if not prescription:
        prescription = Prescription(
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            doctor_id=encounter.doctor_id,
            doctor_name=encounter.doctor_name,
            status="Active",
        )
        session.add(prescription)
        session.commit()
        session.refresh(prescription)

    # Check for safety conflicts
    items = session.exec(
        select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id)
    ).all()
    existing_items = [{"drug_name": it.drug_name, "generic_name": it.generic_name or it.drug_name} for it in items]

    alerts = check_prescription_safety(
        patient_allergies=patient.allergies if patient else None,
        candidate_drug_name=drug_name,
        candidate_generic=generic_name or drug_name,
        existing_items=existing_items,
    )

    if alerts and not (override_reason and override_reason.strip()):
        # Conflict exists and no override supplied: render modal with error
        ctx = get_ui_context(
            request,
            user=user,
            session=session,
            encounter=encounter,
            patient=patient,
            candidate_drug=drug_name,
            candidate_generic=generic_name or drug_name,
            alerts=alerts,
            error="Clinical override justification is mandatory to proceed.",
        )
        return templates.TemplateResponse("modules/consultations/partials/safety_alert_modal.html", ctx, status_code=409)

    # If safety alerts overridden, log to AuditLog and emit event
    if alerts and override_reason:
        prescription.safety_override_reason = override_reason.strip()
        session.add(prescription)

        audit_entry = AuditLog(
            user_id=user.id,
            username=user.username,
            ip_address=request.client.host if request.client else None,
            module="consultations",
            entity="prescription_safety_override",
            entity_id=str(prescription.id),
            action="SAFETY_OVERRIDE",
            changes={
                "drug": drug_name,
                "generic": generic_name,
                "override_reason": override_reason.strip(),
                "alerts": alerts,
            },
        )
        session.add(audit_entry)

        event_bus.emit(
            "prescription.safety_overridden",
            {
                "prescription_id": prescription.id,
                "encounter_id": encounter_id,
                "patient_id": patient.id if patient else None,
                "drug": drug_name,
                "override_reason": override_reason.strip(),
                "alerts_count": len(alerts),
            },
            user_id=user.id,
            username=user.username,
        )

    # Auto-calculate quantity if not explicitly provided
    calc_qty = quantity if (quantity and quantity > 0) else calculate_quantity(frequency, duration_days)

    item = PrescriptionItem(
        prescription_id=prescription.id,
        drug_name=drug_name.strip(),
        generic_name=generic_name.strip() if generic_name else None,
        dosage=dosage.strip(),
        frequency=frequency.strip(),
        route=route.strip(),
        duration_days=duration_days,
        quantity=calc_qty,
        instructions=instructions.strip() if instructions else None,
    )
    session.add(item)
    session.commit()

    updated_items = session.exec(
        select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id)
    ).all()
    can_prescribe = user_has_permission(user, "consultations.prescription.write", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        prescription=prescription,
        prescription_items=updated_items,
        can_prescribe=can_prescribe,
    )
    return templates.TemplateResponse("modules/consultations/partials/prescriptions_list.html", ctx)


@router.delete("/encounter/{encounter_id}/prescriptions/items/{item_id}", response_class=HTMLResponse)
def remove_prescription_item(
    encounter_id: int,
    item_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.prescription.write")),
    session: Session = Depends(get_session),
):
    item = session.get(PrescriptionItem, item_id)
    prescription = session.exec(select(Prescription).where(Prescription.encounter_id == encounter_id)).first()

    if item and prescription and item.prescription_id == prescription.id:
        session.delete(item)
        session.commit()

    updated_items = session.exec(
        select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id)
    ).all() if prescription else []
    can_prescribe = user_has_permission(user, "consultations.prescription.write", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=session.get(Encounter, encounter_id),
        prescription=prescription,
        prescription_items=updated_items,
        can_prescribe=can_prescribe,
    )
    return templates.TemplateResponse("modules/consultations/partials/prescriptions_list.html", ctx)


@router.post("/encounter/{encounter_id}/prescriptions/repeat", response_class=HTMLResponse)
def repeat_last_prescription(
    encounter_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.prescription.write")),
    session: Session = Depends(get_session),
):
    """Clones medications from the patient's most recent prior prescription into this encounter"""
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    current_rx = session.exec(select(Prescription).where(Prescription.encounter_id == encounter_id)).first()
    if not current_rx:
        current_rx = Prescription(
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            doctor_id=encounter.doctor_id,
            doctor_name=encounter.doctor_name,
            status="Active",
        )
        session.add(current_rx)
        session.commit()
        session.refresh(current_rx)

    # Find prior prescription
    prior_rx = session.exec(
        select(Prescription)
        .where(
            Prescription.patient_id == encounter.patient_id,
            Prescription.id != current_rx.id,
        )
        .order_by(col(Prescription.prescribed_at).desc())
    ).first()

    if prior_rx:
        prior_items = session.exec(
            select(PrescriptionItem).where(PrescriptionItem.prescription_id == prior_rx.id)
        ).all()
        for pit in prior_items:
            new_item = PrescriptionItem(
                prescription_id=current_rx.id,
                drug_name=pit.drug_name,
                generic_name=pit.generic_name,
                dosage=pit.dosage,
                frequency=pit.frequency,
                route=pit.route,
                duration_days=pit.duration_days,
                quantity=pit.quantity,
                instructions=pit.instructions,
            )
            session.add(new_item)
        session.commit()

    updated_items = session.exec(
        select(PrescriptionItem).where(PrescriptionItem.prescription_id == current_rx.id)
    ).all()
    can_prescribe = user_has_permission(user, "consultations.prescription.write", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        prescription=current_rx,
        prescription_items=updated_items,
        can_prescribe=can_prescribe,
    )
    return templates.TemplateResponse("modules/consultations/partials/prescriptions_list.html", ctx)


# -------------------------------------------------------------
# ORDERS (LAB & IMAGING) ENDPOINTS
# -------------------------------------------------------------


@router.post("/encounter/{encounter_id}/orders", response_class=HTMLResponse)
def place_order(
    encounter_id: int,
    request: Request,
    type: str = Form("Lab"),
    test_name: str = Form(...),
    priority: str = Form("Routine"),
    clinical_notes: Optional[str] = Form(None),
    user: User = Depends(require_permission("consultations.orders.create")),
    session: Session = Depends(get_session),
):
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    clean_test = test_name.strip()
    if not clean_test:
        raise HTTPException(status_code=400, detail="Test name cannot be empty")

    order = Order(
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        doctor_id=encounter.doctor_id,
        type=type,
        test_name=clean_test,
        priority=priority,
        clinical_notes=clinical_notes.strip() if clinical_notes else None,
        status="Placed",
        placed_at=utc_now(),
    )
    session.add(order)
    session.commit()
    session.refresh(order)

    # Emit domain event for Lab / Radiology modules to subscribe to!
    event_bus.emit(
        "order.placed",
        {
            "order_id": order.id,
            "encounter_id": encounter.id,
            "patient_id": encounter.patient_id,
            "patient_mrn": encounter.patient_mrn,
            "patient_name": encounter.patient_name,
            "doctor_id": encounter.doctor_id,
            "type": order.type,
            "test_name": order.test_name,
            "priority": order.priority,
            "clinical_notes": order.clinical_notes,
        },
        user_id=user.id,
        username=user.username,
    )

    orders = session.exec(
        select(Order).where(Order.encounter_id == encounter_id).order_by(col(Order.placed_at).desc())
    ).all()
    can_order = user_has_permission(user, "consultations.orders.create", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        orders=orders,
        can_order=can_order,
    )
    return templates.TemplateResponse("modules/consultations/partials/orders_list.html", ctx)


@router.delete("/encounter/{encounter_id}/orders/{order_id}", response_class=HTMLResponse)
def remove_order(
    encounter_id: int,
    order_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.orders.create")),
    session: Session = Depends(get_session),
):
    order = session.get(Order, order_id)
    if order and order.encounter_id == encounter_id:
        session.delete(order)
        session.commit()

    orders = session.exec(
        select(Order).where(Order.encounter_id == encounter_id).order_by(col(Order.placed_at).desc())
    ).all()
    can_order = user_has_permission(user, "consultations.orders.create", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=session.get(Encounter, encounter_id),
        orders=orders,
        can_order=can_order,
    )
    return templates.TemplateResponse("modules/consultations/partials/orders_list.html", ctx)


# -------------------------------------------------------------
# FOLLOW-UP & REFERRAL ENDPOINTS
# -------------------------------------------------------------


@router.post("/encounter/{encounter_id}/followup", response_class=HTMLResponse)
def schedule_followup(
    encounter_id: int,
    request: Request,
    recommended_date: str = Form(...),
    notes: Optional[str] = Form(None),
    book_appointment: bool = Form(False),
    user: User = Depends(require_permission("consultations.encounter.start")),
    session: Session = Depends(get_session),
):
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    parsed_date = datetime.strptime(recommended_date, "%Y-%m-%d").date()

    follow_up = session.exec(select(FollowUp).where(FollowUp.encounter_id == encounter_id)).first()
    if not follow_up:
        follow_up = FollowUp(
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            doctor_id=encounter.doctor_id,
            recommended_date=parsed_date,
            notes=notes,
        )
        session.add(follow_up)
    else:
        follow_up.recommended_date = parsed_date
        follow_up.notes = notes
        session.add(follow_up)

    created_appt_id = None
    if book_appointment:
        # Create scheduled follow-up appointment in appointments table
        patient = session.get(Patient, encounter.patient_id)
        doctor = session.get(Doctor, encounter.doctor_id)
        if patient and doctor:
            slot_time = "10:00"
            start_dt = datetime.combine(parsed_date, datetime.strptime(slot_time, "%H:%M").time()).replace(tzinfo=timezone.utc)
            end_dt = start_dt + timedelta(minutes=doctor.slot_length or 20)

            # Check if slot occupied, if so advance
            existing = session.exec(
                select(Appointment).where(
                    Appointment.doctor_id == doctor.id,
                    Appointment.appointment_date == recommended_date,
                    Appointment.start_time == slot_time,
                    Appointment.status != "Cancelled",
                )
            ).first()
            if existing:
                slot_time = "11:00"
                start_dt = datetime.combine(parsed_date, datetime.strptime(slot_time, "%H:%M").time()).replace(tzinfo=timezone.utc)
                end_dt = start_dt + timedelta(minutes=doctor.slot_length or 20)

            appt = Appointment(
                patient_id=patient.id,
                patient_name=patient.full_name,
                patient_mrn=patient.mrn,
                patient_phone=patient.phone,
                doctor_id=doctor.id,
                doctor_name=doctor.name,
                department=doctor.department,
                appointment_date=recommended_date,
                start_time=slot_time,
                end_time=(start_dt + timedelta(minutes=doctor.slot_length)).strftime("%H:%M"),
                start_datetime=start_dt,
                end_datetime=end_dt,
                type="Follow-up",
                status="Booked",
                source="Referral",
                notes=f"Follow-up from encounter #{encounter.id}: {notes or ''}",
            )
            session.add(appt)
            session.commit()
            session.refresh(appt)
            follow_up.appointment_id = appt.id
            created_appt_id = appt.id

    session.commit()

    badge = f"<span class='text-xs text-emerald-600 dark:text-emerald-400 font-medium'>Follow-up saved for {recommended_date}"
    if created_appt_id:
        badge += f" (Appointment #{created_appt_id} booked!)"
    badge += "</span>"
    return HTMLResponse(badge)


@router.post("/encounter/{encounter_id}/referral", response_class=HTMLResponse)
def create_referral(
    encounter_id: int,
    request: Request,
    referred_to_specialty: str = Form(...),
    referred_to_doctor: Optional[str] = Form(None),
    reason: str = Form(...),
    notes: Optional[str] = Form(None),
    user: User = Depends(require_permission("consultations.notes.write")),
    session: Session = Depends(get_session),
):
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    ref = session.exec(select(Referral).where(Referral.encounter_id == encounter_id)).first()
    if not ref:
        ref = Referral(
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            referring_doctor_id=encounter.doctor_id,
            referring_doctor_name=encounter.doctor_name,
            referred_to_specialty=referred_to_specialty,
            referred_to_doctor=referred_to_doctor,
            reason=reason.strip(),
            notes=notes,
        )
    else:
        ref.referred_to_specialty = referred_to_specialty
        ref.referred_to_doctor = referred_to_doctor
        ref.reason = reason.strip()
        ref.notes = notes

    session.add(ref)
    session.commit()

    return HTMLResponse(
        f"""<div class='p-2 rounded bg-teal-50 dark:bg-teal-950/40 border border-teal-200 dark:border-teal-800 text-xs text-teal-800 dark:text-teal-300'>
            <b>Referral Created:</b> Referred to <b>{referred_to_specialty}</b> ({referred_to_doctor or 'Department'}).
           </div>"""
    )


# -------------------------------------------------------------
# COMPLETE ENCOUNTER & VISIT SUMMARY / PDF ENDPOINTS
# -------------------------------------------------------------


@router.post("/encounter/{encounter_id}/complete", response_class=HTMLResponse)
def complete_encounter(
    encounter_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.encounter.complete")),
    session: Session = Depends(get_session),
):
    """
    Completes the encounter:
    - Sets encounter status = 'Completed', completed_at = now
    - If linked to appointment, sets appointment status = 'Completed', completed_at = now
    - Sets QueueToken = 'Done'
    - Emits 'encounter.completed'
    """
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    encounter.status = "Completed"
    encounter.completed_at = utc_now()
    session.add(encounter)

    # Link to appointment
    if encounter.appointment_id:
        appt = session.get(Appointment, encounter.appointment_id)
        if appt:
            appt.status = "Completed"
            appt.completed_at = utc_now()
            session.add(appt)

            # Update queue token
            token = session.exec(select(QueueToken).where(QueueToken.appointment_id == appt.id)).first()
            if token:
                token.status = "Done"
                token.completed_at = utc_now()
                session.add(token)

    session.commit()

    # Ensure prescription.signed is emitted for Pharmacy
    rx = session.exec(select(Prescription).where(Prescription.encounter_id == encounter.id)).first()
    if rx:
        rx_items = session.exec(select(PrescriptionItem).where(PrescriptionItem.prescription_id == rx.id)).all()
        if rx_items:
            event_bus.emit(
                "prescription.signed",
                {
                    "prescription_id": rx.id,
                    "encounter_id": encounter.id,
                    "patient_id": encounter.patient_id,
                    "patient_mrn": encounter.patient_mrn,
                    "patient_name": encounter.patient_name,
                    "doctor_id": encounter.doctor_id,
                    "doctor_name": encounter.doctor_name,
                    "safety_overrides": rx.safety_overrides,
                    "items": [
                        {
                            "id": item.id,
                            "drug_name": item.drug_name,
                            "generic_name": item.generic_name,
                            "dose": item.dose,
                            "frequency": item.frequency,
                            "duration": item.duration,
                            "route": item.route,
                            "quantity": item.quantity,
                            "instructions": item.instructions,
                        }
                        for item in rx_items
                    ],
                    "signed_at": encounter.completed_at.isoformat() if encounter.completed_at else utc_now().isoformat(),
                },
                user_id=user.id,
                username=user.username,
            )

    # Emit domain event so Billing and future modules can subscribe!
    event_bus.emit(
        "encounter.completed",
        {
            "id": encounter.id,
            "patient_id": encounter.patient_id,
            "patient_mrn": encounter.patient_mrn,
            "patient_name": encounter.patient_name,
            "doctor_id": encounter.doctor_id,
            "doctor_name": encounter.doctor_name,
            "appointment_id": encounter.appointment_id,
            "completed_at": encounter.completed_at.isoformat(),
        },
        user_id=user.id,
        username=user.username,
        ip_address=request.client.host if request.client else None,
    )

    response = Response(status_code=200)
    response.headers["HX-Redirect"] = f"/consultations/encounter/{encounter.id}/summary"
    return response


@router.get("/encounter/{encounter_id}/summary", response_class=HTMLResponse)
def visit_summary_view(
    encounter_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.encounter.read")),
    session: Session = Depends(get_session),
):
    """Printable browser view for clinical visit summary & prescription"""
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    patient = session.get(Patient, encounter.patient_id)
    doctor = session.get(Doctor, encounter.doctor_id)
    vitals = session.exec(
        select(Vitals).where(Vitals.encounter_id == encounter.id).order_by(col(Vitals.recorded_at).desc())
    ).first()
    note = session.exec(select(ClinicalNote).where(ClinicalNote.encounter_id == encounter.id)).first()
    diagnoses = session.exec(select(Diagnosis).where(Diagnosis.encounter_id == encounter.id)).all()
    prescription = session.exec(select(Prescription).where(Prescription.encounter_id == encounter.id)).first()
    rx_items = session.exec(
        select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id)
    ).all() if prescription else []
    orders = session.exec(select(Order).where(Order.encounter_id == encounter.id)).all()
    follow_up = session.exec(select(FollowUp).where(FollowUp.encounter_id == encounter.id)).first()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        encounter=encounter,
        patient=patient,
        doctor=doctor,
        vitals=vitals,
        note=note,
        diagnoses=diagnoses,
        prescription=prescription,
        prescription_items=rx_items,
        orders=orders,
        follow_up=follow_up,
        hospital_info=settings_registry.get_all("hospital") or {},
    )
    return templates.TemplateResponse("modules/consultations/summary.html", ctx)


@router.get("/encounter/{encounter_id}/pdf")
def download_visit_summary_pdf(
    encounter_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.encounter.read")),
    session: Session = Depends(get_session),
):
    """Generates and downloads official branded visit summary PDF via ReportLab"""
    encounter = session.get(Encounter, encounter_id)
    if not encounter:
        raise HTTPException(status_code=404, detail="Encounter not found")

    patient = session.get(Patient, encounter.patient_id)
    doctor = session.get(Doctor, encounter.doctor_id)
    vitals = session.exec(
        select(Vitals).where(Vitals.encounter_id == encounter.id).order_by(col(Vitals.recorded_at).desc())
    ).first()
    note = session.exec(select(ClinicalNote).where(ClinicalNote.encounter_id == encounter.id)).first()
    diagnoses = session.exec(select(Diagnosis).where(Diagnosis.encounter_id == encounter.id)).all()
    prescription = session.exec(select(Prescription).where(Prescription.encounter_id == encounter.id)).first()
    rx_items = session.exec(
        select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id)
    ).all() if prescription else []
    orders = session.exec(select(Order).where(Order.encounter_id == encounter.id)).all()
    follow_up = session.exec(select(FollowUp).where(FollowUp.encounter_id == encounter.id)).first()

    hospital_info = settings_registry.get_all("hospital") or {
        "name": "MediCore Central Hospital",
        "address": "100 Healthcare Boulevard, Metro City",
        "phone": "+1 (555) 234-5678",
        "emergency_contact": "911 / (555) 999-0000",
    }

    enc_data = {
        "id": encounter.id,
        "date": encounter.started_at.strftime("%Y-%m-%d %H:%M"),
        "chief_complaint": encounter.chief_complaint,
        "status": encounter.status,
    }
    pat_data = {
        "name": patient.full_name if patient else "Unknown",
        "mrn": patient.mrn if patient else "N/A",
        "dob": patient.date_of_birth if patient else "N/A",
        "gender": patient.gender if patient else "N/A",
        "blood_group": patient.blood_group if patient else "N/A",
        "allergies": patient.allergies if patient else "",
    }
    doc_data = {
        "name": doctor.name if doctor else encounter.doctor_name,
        "department": doctor.department if doctor else encounter.department,
        "license": f"MC-LIC-{1000 + (doctor.id if doctor else 1)}",
    }
    vitals_dict = None
    if vitals:
        vitals_dict = {
            "systolic": vitals.systolic,
            "diastolic": vitals.diastolic,
            "pulse": vitals.pulse,
            "temp_c": vitals.temp_c,
            "spo2": vitals.spo2,
            "height_cm": vitals.height_cm,
            "weight_kg": vitals.weight_kg,
            "bmi": vitals.bmi,
            "bsa": vitals.bsa,
        }
    note_dict = None
    if note:
        note_dict = {
            "subjective": note.subjective,
            "objective": note.objective,
            "assessment": note.assessment,
            "plan": note.plan,
            "is_signed": note.is_signed,
            "signed_by": note.signed_by,
            "addenda": note.addenda or [],
        }
    dx_list = [{"icd10_code": d.icd10_code, "description": d.description, "is_primary": d.is_primary} for d in diagnoses]
    rx_list = [
        {
            "drug_name": it.drug_name,
            "generic_name": it.generic_name,
            "dosage": it.dosage,
            "frequency": it.frequency,
            "route": it.route,
            "duration_days": it.duration_days,
            "quantity": it.quantity,
            "instructions": it.instructions,
        }
        for it in rx_items
    ]
    orders_list = [{"type": o.type, "test_name": o.test_name, "priority": o.priority, "status": o.status} for o in orders]
    fu_dict = {"date": str(follow_up.recommended_date), "notes": follow_up.notes} if follow_up else None

    pdf_bytes = generate_visit_summary_pdf(
        hospital_info=hospital_info,
        encounter_data=enc_data,
        patient_data=pat_data,
        doctor_data=doc_data,
        vitals_data=vitals_dict,
        diagnoses=dx_list,
        clinical_note=note_dict,
        prescriptions=rx_list,
        orders=orders_list,
        follow_up=fu_dict,
    )

    filename = f"MediCore_Visit_Summary_Encounter_{encounter.id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -------------------------------------------------------------
# PATIENT 360 REAL-DATA TABS ENDPOINTS
# -------------------------------------------------------------


@router.get("/patient/{patient_id}/visits", response_class=HTMLResponse)
def patient_360_visits(
    patient_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.encounter.read")),
    session: Session = Depends(get_session),
):
    encounters = session.exec(
        select(Encounter)
        .where(Encounter.patient_id == patient_id)
        .order_by(col(Encounter.started_at).desc())
    ).all()
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        patient_id=patient_id,
        encounters=encounters,
    )
    return templates.TemplateResponse("modules/consultations/partials/patient360_visits.html", ctx)


@router.get("/patient/{patient_id}/vitals", response_class=HTMLResponse)
def patient_360_vitals(
    patient_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.encounter.read")),
    session: Session = Depends(get_session),
):
    vitals_list = session.exec(
        select(Vitals)
        .where(Vitals.patient_id == patient_id)
        .order_by(col(Vitals.recorded_at).desc())
    ).all()
    latest_vitals = vitals_list[0] if vitals_list else None
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        patient_id=patient_id,
        vitals_list=vitals_list,
        latest_vitals=latest_vitals,
    )
    return templates.TemplateResponse("modules/consultations/partials/patient360_vitals.html", ctx)


@router.get("/patient/{patient_id}/prescriptions", response_class=HTMLResponse)
def patient_360_prescriptions(
    patient_id: int,
    request: Request,
    user: User = Depends(require_permission("consultations.encounter.read")),
    session: Session = Depends(get_session),
):
    prescriptions = session.exec(
        select(Prescription)
        .where(Prescription.patient_id == patient_id)
        .order_by(col(Prescription.prescribed_at).desc())
    ).all()
    rx_ids = [p.id for p in prescriptions if p.id]
    items: List[PrescriptionItem] = []
    if rx_ids:
        items = session.exec(
            select(PrescriptionItem)
            .where(PrescriptionItem.prescription_id.in_(rx_ids))
            .order_by(col(PrescriptionItem.id).desc())
        ).all()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        patient_id=patient_id,
        prescriptions=prescriptions,
        items=items,
    )
    return templates.TemplateResponse("modules/consultations/partials/patient360_prescriptions.html", ctx)

