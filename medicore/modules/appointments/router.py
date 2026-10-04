import math
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session, col, or_, select
from medicore.core.database import get_session
from medicore.core.events import event_bus
from medicore.core.models import AuditLog, CustomFieldDefinition, User
from medicore.core.security import get_current_user, require_permission
from medicore.core.settings import settings_registry
from medicore.modules.appointments.models import Appointment, Doctor, QueueToken, utc_now
from medicore.modules.appointments.schema import appointment_schema, doctor_schema
from medicore.modules.appointments.service import (
    calculate_appointment_stats,
    check_booking_conflict,
    generate_doctor_slots,
    generate_queue_token,
)
from medicore.modules.patients.models import Patient
from medicore.ui.templating import get_ui_context, templates

router = APIRouter(prefix="/appointments", tags=["Appointments"])


def _get_doctor_for_user(user: User, session: Session) -> Optional[Doctor]:
    if not user:
        return None
    return session.exec(select(Doctor).where(Doctor.user_id == user.id)).first()


# ---------------------------------------------------------------------------
# Main Page (Calendar, Table, Queue Desk)
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def appointments_index(
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.read")),
    session: Session = Depends(get_session),
    tab: str = Query("calendar"),
    cal_view: str = Query("day"),
    date_str: Optional[str] = Query(None, alias="date"),
    doctor_id: Optional[int] = Query(None),
    department: Optional[str] = Query(None),
    selected_id: Optional[int] = Query(None),
):
    today = datetime.now(timezone.utc).date()
    cur_date_str = date_str or today.strftime("%Y-%m-%d")

    # If doctor user, default to their doctor profile if not explicitly set
    doc_profile = _get_doctor_for_user(user, session)
    if doc_profile and not doctor_id and not user.is_superuser:
        doctor_id = doc_profile.id

    effective_schema = appointment_schema.get_effective_schema(session, user)
    doctors = session.exec(select(Doctor).where(Doctor.is_active == True).order_by(Doctor.name)).all()

    # Pre-calculate calendar data
    time_slots = [f"{h:02d}" for h in range(8, 19)]  # 08:00 to 18:00
    cur_dt = datetime.strptime(cur_date_str, "%Y-%m-%d").date()

    prev_date = (cur_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (cur_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    today_date = today.strftime("%Y-%m-%d")
    current_date_label = cur_dt.strftime("%A, %B %d, %Y")

    # Day view appointments
    stmt = select(Appointment).where(Appointment.appointment_date == cur_date_str)
    if doctor_id:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if department:
        stmt = stmt.where(Appointment.department == department)
    day_appts = session.exec(stmt.order_by(Appointment.start_time)).all()

    hour_appointments = {}
    for h in time_slots:
        hour_appointments[h] = [
            a for a in day_appts if a.start_time.startswith(h)
        ]

    # Week view appointments
    start_of_week = cur_dt - timedelta(days=cur_dt.weekday())
    week_days = []
    for d in range(7):
        w_day = start_of_week + timedelta(days=d)
        w_day_str = w_day.strftime("%Y-%m-%d")
        w_stmt = select(Appointment).where(Appointment.appointment_date == w_day_str)
        if doctor_id:
            w_stmt = w_stmt.where(Appointment.doctor_id == doctor_id)
        if department:
            w_stmt = w_stmt.where(Appointment.department == department)
        appts_for_day = session.exec(w_stmt.order_by(Appointment.start_time)).all()
        week_days.append({
            "date": w_day_str,
            "day_name": w_day.strftime("%a"),
            "is_today": w_day == today,
            "appointments": appts_for_day,
        })

    # Table view initial batch
    table_stmt = select(Appointment).order_by(col(Appointment.id).desc())
    if doctor_id:
        table_stmt = table_stmt.where(Appointment.doctor_id == doctor_id)
    all_table_appts = session.exec(table_stmt).all()
    paged_table_appts = all_table_appts[:15]

    # Queue tokens for today
    queue_stmt = select(QueueToken).where(QueueToken.date == cur_date_str).order_by(QueueToken.sequence)
    if department:
        queue_stmt = queue_stmt.where(QueueToken.department == department)
    queue_tokens = session.exec(queue_stmt).all()

    # Pre-select appointment for detail pane
    selected_appt = None
    if selected_id:
        selected_appt = session.get(Appointment, selected_id)
    elif day_appts:
        selected_appt = day_appts[0]
    elif paged_table_appts:
        selected_appt = paged_table_appts[0]

    linked_token = None
    if selected_appt:
        linked_token = session.exec(
            select(QueueToken).where(QueueToken.appointment_id == selected_appt.id)
        ).first()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        schema=effective_schema,
        active_tab=tab,
        cal_view=cal_view,
        current_date=cur_date_str,
        current_date_label=current_date_label,
        prev_date=prev_date,
        next_date=next_date,
        today_date=today_date,
        selected_doctor_id=doctor_id,
        selected_dept=department,
        doctors=doctors,
        time_slots=time_slots,
        hour_appointments=hour_appointments,
        week_days=week_days,
        appointments=paged_table_appts,
        selected_appointment=selected_appt,
        selected_appointment_id=selected_appt.id if selected_appt else None,
        queue_token=linked_token,
        queue_tokens=queue_tokens,
        total_count=len(all_table_appts),
        page=1,
        page_size=15,
        total_pages=max(1, math.ceil(len(all_table_appts) / 15)),
    )
    return templates.TemplateResponse("modules/appointments/index.html", ctx)


# ---------------------------------------------------------------------------
# Calendar Partial
# ---------------------------------------------------------------------------

@router.get("/calendar", response_class=HTMLResponse)
def calendar_partial(
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.read")),
    session: Session = Depends(get_session),
    cal_view: str = Query("day"),
    date_str: Optional[str] = Query(None, alias="date"),
    doctor_id: Optional[int] = Query(None),
    department: Optional[str] = Query(None),
):
    today = datetime.now(timezone.utc).date()
    cur_date_str = date_str or today.strftime("%Y-%m-%d")
    cur_dt = datetime.strptime(cur_date_str, "%Y-%m-%d").date()

    doctors = session.exec(select(Doctor).where(Doctor.is_active == True).order_by(Doctor.name)).all()
    time_slots = [f"{h:02d}" for h in range(8, 19)]

    prev_date = (cur_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (cur_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    today_date = today.strftime("%Y-%m-%d")
    current_date_label = cur_dt.strftime("%A, %B %d, %Y")

    stmt = select(Appointment).where(Appointment.appointment_date == cur_date_str)
    if doctor_id:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if department:
        stmt = stmt.where(Appointment.department == department)
    day_appts = session.exec(stmt.order_by(Appointment.start_time)).all()

    hour_appointments = {}
    for h in time_slots:
        hour_appointments[h] = [a for a in day_appts if a.start_time.startswith(h)]

    start_of_week = cur_dt - timedelta(days=cur_dt.weekday())
    week_days = []
    for d in range(7):
        w_day = start_of_week + timedelta(days=d)
        w_day_str = w_day.strftime("%Y-%m-%d")
        w_stmt = select(Appointment).where(Appointment.appointment_date == w_day_str)
        if doctor_id:
            w_stmt = w_stmt.where(Appointment.doctor_id == doctor_id)
        if department:
            w_stmt = w_stmt.where(Appointment.department == department)
        appts_for_day = session.exec(w_stmt.order_by(Appointment.start_time)).all()
        week_days.append({
            "date": w_day_str,
            "day_name": w_day.strftime("%a"),
            "is_today": w_day == today,
            "appointments": appts_for_day,
        })

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        cal_view=cal_view,
        current_date=cur_date_str,
        current_date_label=current_date_label,
        prev_date=prev_date,
        next_date=next_date,
        today_date=today_date,
        selected_doctor_id=doctor_id,
        selected_dept=department,
        doctors=doctors,
        time_slots=time_slots,
        hour_appointments=hour_appointments,
        week_days=week_days,
    )
    return templates.TemplateResponse("modules/appointments/calendar_partial.html", ctx)


# ---------------------------------------------------------------------------
# Dense Table Partial
# ---------------------------------------------------------------------------

@router.get("/table", response_class=HTMLResponse)
def table_partial(
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.read")),
    session: Session = Depends(get_session),
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    doctor_id: Optional[int] = Query(None),
    date_str: Optional[str] = Query(None, alias="date"),
    sort: Optional[str] = Query("-id"),
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=5, le=100),
    selected_id: Optional[int] = Query(None),
):
    effective_schema = appointment_schema.get_effective_schema(session, user)
    stmt = select(Appointment)

    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Appointment.patient_name.ilike(term),
                Appointment.patient_mrn.ilike(term),
                Appointment.doctor_name.ilike(term),
                Appointment.notes.ilike(term),
            )
        )

    if status and status != "All":
        stmt = stmt.where(Appointment.status == status)
    if department and department != "All":
        stmt = stmt.where(Appointment.department == department)
    if type and type != "All":
        stmt = stmt.where(Appointment.type == type)
    if doctor_id:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if date_str:
        stmt = stmt.where(Appointment.appointment_date == date_str)

    if sort == "appointment_date":
        stmt = stmt.order_by(col(Appointment.appointment_date).asc(), col(Appointment.start_time).asc())
    elif sort == "-appointment_date":
        stmt = stmt.order_by(col(Appointment.appointment_date).desc(), col(Appointment.start_time).desc())
    elif sort == "patient_name":
        stmt = stmt.order_by(col(Appointment.patient_name).asc())
    elif sort == "doctor_name":
        stmt = stmt.order_by(col(Appointment.doctor_name).asc())
    elif sort == "status":
        stmt = stmt.order_by(col(Appointment.status).asc())
    else:
        stmt = stmt.order_by(col(Appointment.id).desc())

    all_matches = session.exec(stmt).all()
    total_count = len(all_matches)
    total_pages = max(1, math.ceil(total_count / page_size))

    offset = (page - 1) * page_size
    paged_appts = all_matches[offset : offset + page_size]

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        schema=effective_schema,
        appointments=paged_appts,
        selected_appointment_id=selected_id,
        total_count=total_count,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        q=q or "",
        status_filter=status or "",
        department_filter=department or "",
        type_filter=type or "",
        doctor_id_filter=doctor_id or "",
        date_filter=date_str or "",
    )
    return templates.TemplateResponse("modules/appointments/table_partial.html", ctx)


# ---------------------------------------------------------------------------
# Split-Pane Detail View
# ---------------------------------------------------------------------------

@router.get("/detail/{appointment_id}", response_class=HTMLResponse)
def appointment_detail(
    appointment_id: int,
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.read")),
    session: Session = Depends(get_session),
):
    appt = session.get(Appointment, appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    queue_token = session.exec(
        select(QueueToken).where(QueueToken.appointment_id == appt.id)
    ).first()

    event_bus.emit(
        "appointment.viewed",
        {"id": appt.id, "patient_name": appt.patient_name, "doctor_name": appt.doctor_name},
        user_id=user.id,
        username=user.username,
        ip_address=request.client.host if request.client else None,
    )

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        appointment=appt,
        queue_token=queue_token,
    )
    return templates.TemplateResponse("modules/appointments/detail_pane.html", ctx)


# ---------------------------------------------------------------------------
# Dynamic Slot Picker API
# ---------------------------------------------------------------------------

@router.get("/slots", response_class=HTMLResponse)
def get_doctor_slots_api(
    request: Request,
    doctor_id: Optional[int] = Query(None),
    appointment_date: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    if not doctor_id or not appointment_date:
        return HTMLResponse("<div class='text-xs text-muted p-2'>Please select both a doctor and date to view slots.</div>")

    doctor = session.get(Doctor, doctor_id)
    if not doctor:
        return HTMLResponse("<div class='text-xs text-rose-500 p-2'>Doctor not found</div>")

    slots = generate_doctor_slots(doctor, appointment_date, session)
    ctx = {"request": request, "slots": slots}
    return templates.TemplateResponse("modules/appointments/slots_partial.html", ctx)


# ---------------------------------------------------------------------------
# Booking Modal & Creation
# ---------------------------------------------------------------------------

@router.get("/new", response_class=HTMLResponse)
def new_appointment_modal(
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.create")),
    session: Session = Depends(get_session),
    patient_id: Optional[int] = Query(None),
    doctor_id: Optional[int] = Query(None),
    date_str: Optional[str] = Query(None, alias="date"),
    start_time: Optional[str] = Query(None),
):
    today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    def_date = date_str or today_str

    pre_patient = session.get(Patient, patient_id) if patient_id else None
    doctors = session.exec(select(Doctor).where(Doctor.is_active == True).order_by(Doctor.name)).all()
    patients = session.exec(select(Patient).order_by(col(Patient.id).desc()).limit(100)).all()

    first_doc = session.get(Doctor, doctor_id) if doctor_id else (doctors[0] if doctors else None)
    slots = generate_doctor_slots(first_doc, def_date, session) if first_doc else []

    def_start = start_time or (slots[0]["start_time"] if slots else "09:00")
    def_end = (slots[0]["end_time"] if slots else "09:20")

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        pre_patient=pre_patient,
        patients=patients,
        doctors=doctors,
        selected_doctor_id=first_doc.id if first_doc else None,
        default_date=def_date,
        default_start_time=def_start,
        default_end_time=def_end,
        slots=slots,
        conflict_error=None,
    )
    return templates.TemplateResponse("modules/appointments/new_modal.html", ctx)


@router.post("", response_class=HTMLResponse)
async def create_appointment(
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.create")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    patient_id = int(form.get("patient_id"))
    doctor_id = int(form.get("doctor_id"))
    date_str = str(form.get("appointment_date")).strip()
    start_time = str(form.get("start_time")).strip()
    end_time = str(form.get("end_time")).strip()
    appt_type = str(form.get("type", "Consultation")).strip()
    source = str(form.get("source", "Reception")).strip()
    notes = str(form.get("notes", "")).strip() or None

    doctor = session.get(Doctor, doctor_id)
    patient = session.get(Patient, patient_id)
    if not doctor or not patient:
        raise HTTPException(status_code=400, detail="Invalid doctor or patient")

    # Conflict check
    conflict = check_booking_conflict(doctor.id, date_str, start_time, end_time, session)
    if conflict:
        # Re-render modal with conflict message
        doctors = session.exec(select(Doctor).where(Doctor.is_active == True).order_by(Doctor.name)).all()
        patients = session.exec(select(Patient).order_by(col(Patient.id).desc()).limit(100)).all()
        slots = generate_doctor_slots(doctor, date_str, session)
        ctx = get_ui_context(
            request,
            user=user,
            session=session,
            pre_patient=patient,
            patients=patients,
            doctors=doctors,
            selected_doctor_id=doctor.id,
            default_date=date_str,
            default_start_time=start_time,
            default_end_time=end_time,
            slots=slots,
            conflict_error=f"Dr. {doctor.name} already has an overlapping appointment ({conflict.start_time} - {conflict.end_time}) on {date_str}.",
        )
        return templates.TemplateResponse("modules/appointments/new_modal.html", ctx)

    # Compute start & end datetimes
    start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    appointment = Appointment(
        patient_id=patient.id,
        patient_name=patient.full_name,
        patient_mrn=patient.mrn,
        patient_phone=patient.phone,
        doctor_id=doctor.id,
        doctor_name=doctor.name,
        department=doctor.department,
        appointment_date=date_str,
        start_time=start_time,
        end_time=end_time,
        start_datetime=start_dt,
        end_datetime=end_dt,
        type=appt_type,
        status="Booked",
        source=source,
        notes=notes,
    )
    session.add(appointment)
    session.commit()
    session.refresh(appointment)

    # Emit domain event
    event_bus.emit(
        "appointment.created",
        {
            "id": appointment.id,
            "patient_name": appointment.patient_name,
            "patient_mrn": appointment.patient_mrn,
            "doctor_name": appointment.doctor_name,
            "department": appointment.department,
            "appointment_date": appointment.appointment_date,
            "start_time": appointment.start_time,
            "type": appointment.type,
        },
        user_id=user.id,
        username=user.username,
        ip_address=request.client.host if request.client else None,
    )

    response = HTMLResponse(
        content=f"""
        <div id="modal-container"></div>
        <script>
            window.dispatchEvent(new CustomEvent('appointment-created', {{ detail: {{ id: {appointment.id} }} }}));
            htmx.ajax('GET', '/appointments/calendar?cal_view=day&date={appointment.appointment_date}&doctor_id={appointment.doctor_id}', {{target: '#appointments-calendar-container', swap: 'innerHTML'}});
            htmx.ajax('GET', '/appointments/detail/{appointment.id}', {{target: '#detail-pane', swap: 'innerHTML'}});
        </script>
        """
    )
    response.headers["HX-Trigger"] = "appointmentListChanged"
    return response


# ---------------------------------------------------------------------------
# Walk-In Registration & Live Queue Token Generator
# ---------------------------------------------------------------------------

@router.get("/walkin", response_class=HTMLResponse)
def walkin_modal(
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.create")),
    session: Session = Depends(get_session),
):
    patients = session.exec(select(Patient).order_by(col(Patient.id).desc()).limit(100)).all()
    doctors = session.exec(select(Doctor).where(Doctor.is_active == True).order_by(Doctor.name)).all()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        patients=patients,
        doctors=doctors,
    )
    return templates.TemplateResponse("modules/appointments/walkin_modal.html", ctx)


@router.post("/walkin", response_class=HTMLResponse)
async def create_walkin(
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.create")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    doctor_id = int(form.get("doctor_id"))
    doctor = session.get(Doctor, doctor_id)
    if not doctor:
        raise HTTPException(status_code=400, detail="Doctor not found")

    patient_id_val = form.get("patient_id")
    if patient_id_val and str(patient_id_val).isdigit():
        patient = session.get(Patient, int(patient_id_val))
    else:
        # Quick create patient
        fname = str(form.get("first_name", "WalkIn")).strip() or "WalkIn"
        lname = str(form.get("last_name", "Patient")).strip() or "Patient"
        phone = str(form.get("phone", "+1 555-0000")).strip()
        gender = str(form.get("gender", "Female")).strip()
        mrn = settings_registry.generate_mrn()

        patient = Patient(
            mrn=mrn,
            first_name=fname,
            last_name=lname,
            date_of_birth="1990-01-01",
            gender=gender,
            phone=phone,
            status="Active",
        )
        session.add(patient)
        session.commit()
        session.refresh(patient)

    now_utc = utc_now()
    today_str = now_utc.strftime("%Y-%m-%d")
    cur_time_str = now_utc.strftime("%H:%M")
    end_time_str = (now_utc + timedelta(minutes=doctor.slot_length or 20)).strftime("%H:%M")

    # Create Walk-in appointment directly with Checked-in status
    appt = Appointment(
        patient_id=patient.id,
        patient_name=patient.full_name,
        patient_mrn=patient.mrn,
        patient_phone=patient.phone,
        doctor_id=doctor.id,
        doctor_name=doctor.name,
        department=doctor.department,
        appointment_date=today_str,
        start_time=cur_time_str,
        end_time=end_time_str,
        start_datetime=now_utc,
        end_datetime=now_utc + timedelta(minutes=doctor.slot_length or 20),
        type=str(form.get("type", "Walk-in")),
        status="Checked-in",
        source="Walk-in",
        notes=str(form.get("notes", "Walk-in triage")).strip(),
        checked_in_at=now_utc,
    )
    session.add(appt)
    session.commit()
    session.refresh(appt)

    # Allocate live queue token
    token = generate_queue_token(
        doctor=doctor,
        patient_id=patient.id,
        patient_name=patient.full_name,
        patient_mrn=patient.mrn,
        appointment_id=appt.id,
        session=session,
        is_walk_in=True,
    )

    event_bus.emit(
        "appointment.checked_in",
        {"id": appt.id, "token_number": token.token_number, "patient_name": appt.patient_name},
        user_id=user.id,
        username=user.username,
        ip_address=request.client.host if request.client else None,
    )

    response = HTMLResponse(
        content=f"""
        <div id="modal-container"></div>
        <script>
            alert("Token Issued: {token.token_number}\\nPlease direct patient to {doctor.room_number or 'Room 1'}.");
            htmx.ajax('GET', '/appointments/detail/{appt.id}', {{target: '#detail-pane', swap: 'innerHTML'}});
            htmx.ajax('GET', '/appointments/calendar?cal_view=day&date={today_str}', {{target: '#appointments-calendar-container', swap: 'innerHTML'}});
        </script>
        """
    )
    response.headers["HX-Trigger"] = "appointmentListChanged"
    return response


# ---------------------------------------------------------------------------
# Status Flow Transitions & Rescheduling
# ---------------------------------------------------------------------------

@router.post("/{appointment_id}/status", response_class=HTMLResponse)
def update_appointment_status(
    appointment_id: int,
    request: Request,
    new_status: str = Query(...),
    user: User = Depends(require_permission("appointments.appointment.update")),
    session: Session = Depends(get_session),
):
    appt = session.get(Appointment, appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    old_status = appt.status
    appt.status = new_status
    now = utc_now()

    token = session.exec(select(QueueToken).where(QueueToken.appointment_id == appt.id)).first()

    if new_status == "Checked-in":
        appt.checked_in_at = now
        if not token:
            doctor = session.get(Doctor, appt.doctor_id)
            if doctor:
                token = generate_queue_token(
                    doctor=doctor,
                    patient_id=appt.patient_id,
                    patient_name=appt.patient_name,
                    patient_mrn=appt.patient_mrn,
                    appointment_id=appt.id,
                    session=session,
                    is_walk_in=False,
                )
        event_bus.emit(
            "appointment.checked_in",
            {"id": appt.id, "patient_name": appt.patient_name, "token": token.token_number if token else None},
            user_id=user.id,
            username=user.username,
        )

    elif new_status == "In-Consultation":
        appt.consultation_started_at = now
        if token:
            token.status = "Serving"
            session.add(token)
        event_bus.emit(
            "appointment.consultation_started",
            {"id": appt.id, "patient_name": appt.patient_name, "doctor_name": appt.doctor_name},
            user_id=user.id,
            username=user.username,
        )

    elif new_status == "Completed":
        appt.completed_at = now
        if token:
            token.status = "Done"
            session.add(token)
        event_bus.emit(
            "appointment.completed",
            {"id": appt.id, "patient_name": appt.patient_name, "doctor_name": appt.doctor_name},
            user_id=user.id,
            username=user.username,
        )

    elif new_status == "No-show":
        if token:
            token.status = "Skipped"
            session.add(token)
        event_bus.emit(
            "appointment.no_show",
            {"id": appt.id, "patient_name": appt.patient_name},
            user_id=user.id,
            username=user.username,
        )

    elif new_status == "Cancelled":
        if token:
            token.status = "Skipped"
            session.add(token)
        event_bus.emit(
            "appointment.cancelled",
            {"id": appt.id, "patient_name": appt.patient_name},
            user_id=user.id,
            username=user.username,
        )

    session.add(appt)
    session.commit()
    session.refresh(appt)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        appointment=appt,
        queue_token=token,
    )
    response = templates.TemplateResponse("modules/appointments/detail_pane.html", ctx)
    response.headers["HX-Trigger"] = "appointmentChanged"
    return response


@router.post("/{appointment_id}/reschedule", response_class=HTMLResponse)
async def reschedule_appointment(
    appointment_id: int,
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.update")),
    session: Session = Depends(get_session),
):
    appt = session.get(Appointment, appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    form = await request.form()
    new_date = str(form.get("appointment_date")).strip()
    new_start = str(form.get("start_time")).strip()
    new_end = str(form.get("end_time")).strip()
    reason = str(form.get("reschedule_reason", "")).strip()

    # Conflict check
    conflict = check_booking_conflict(appt.doctor_id, new_date, new_start, new_end, session, exclude_id=appt.id)
    if conflict:
        raise HTTPException(
            status_code=400,
            detail=f"Conflict detected: Dr. {appt.doctor_name} has an appointment ({conflict.start_time}-{conflict.end_time}) on {new_date}",
        )

    start_dt = datetime.strptime(f"{new_date} {new_start}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(f"{new_date} {new_end}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    appt.appointment_date = new_date
    appt.start_time = new_start
    appt.end_time = new_end
    appt.start_datetime = start_dt
    appt.end_datetime = end_dt
    if reason:
        appt.notes = (appt.notes or "") + f" [Rescheduled: {reason}]"

    session.add(appt)
    session.commit()
    session.refresh(appt)

    event_bus.emit(
        "appointment.rescheduled",
        {
            "id": appt.id,
            "patient_name": appt.patient_name,
            "new_date": new_date,
            "new_time": f"{new_start}-{new_end}",
            "reason": reason,
        },
        user_id=user.id,
        username=user.username,
    )

    token = session.exec(select(QueueToken).where(QueueToken.appointment_id == appt.id)).first()
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        appointment=appt,
        queue_token=token,
    )
    response = templates.TemplateResponse("modules/appointments/detail_pane.html", ctx)
    response.headers["HX-Trigger"] = "appointmentChanged"
    return response


@router.post("/{appointment_id}/reschedule_quick")
async def reschedule_quick(
    appointment_id: int,
    request: Request,
    user: User = Depends(require_permission("appointments.appointment.update")),
    session: Session = Depends(get_session),
):
    appt = session.get(Appointment, appointment_id)
    if not appt:
        return JSONResponse({"status": "error", "message": "Appointment not found"}, status_code=404)

    form = await request.form()
    new_date = str(form.get("appointment_date")).strip()
    new_start = str(form.get("start_time")).strip()

    doctor = session.get(Doctor, appt.doctor_id)
    slot_len = doctor.slot_length if doctor else 20
    h, m = int(new_start.split(":")[0]), int(new_start.split(":")[1])
    tot = h * 60 + m + slot_len
    new_end = f"{(tot // 60) % 24:02d}:{tot % 60:02d}"

    start_dt = datetime.strptime(f"{new_date} {new_start}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(f"{new_date} {new_end}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    appt.appointment_date = new_date
    appt.start_time = new_start
    appt.end_time = new_end
    appt.start_datetime = start_dt
    appt.end_datetime = end_dt

    session.add(appt)
    session.commit()

    event_bus.emit(
        "appointment.rescheduled",
        {"id": appt.id, "new_date": new_date, "new_time": f"{new_start}-{new_end}"},
        user_id=user.id,
        username=user.username,
    )

    return JSONResponse({"status": "ok", "id": appt.id})


# ---------------------------------------------------------------------------
# Waiting Room TV Display (/appointments/display)
# ---------------------------------------------------------------------------

@router.get("/display", response_class=HTMLResponse)
def waiting_room_display(
    request: Request,
    session: Session = Depends(get_session),
):
    h_profile = settings_registry.get("hospital", "profile", {}) or {}
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")

    tokens = session.exec(
        select(QueueToken)
        .where(QueueToken.date == today_str)
        .order_by(QueueToken.sequence)
    ).all()

    ctx = {
        "request": request,
        "hospital_name": h_profile.get("name", "MediCore Central Hospital"),
        "emergency_hotline": h_profile.get("emergency_hotline", "911"),
        "queue_tokens": tokens,
        "now_str": now_str,
    }
    return templates.TemplateResponse("modules/appointments/display.html", ctx)


@router.get("/display/content", response_class=HTMLResponse)
def waiting_room_display_content(
    request: Request,
    session: Session = Depends(get_session),
):
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")

    tokens = session.exec(
        select(QueueToken)
        .where(QueueToken.date == today_str)
        .order_by(QueueToken.sequence)
    ).all()

    ctx = {
        "request": request,
        "queue_tokens": tokens,
        "now_str": now_str,
    }
    return templates.TemplateResponse("modules/appointments/display_partial.html", ctx)


# ---------------------------------------------------------------------------
# Queue Desk & Token Management
# ---------------------------------------------------------------------------

@router.get("/queue", response_class=HTMLResponse)
def queue_desk(
    request: Request,
    user: User = Depends(require_permission("appointments.queue.manage")),
    session: Session = Depends(get_session),
):
    today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    tokens = session.exec(
        select(QueueToken).where(QueueToken.date == today_str).order_by(QueueToken.sequence)
    ).all()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        queue_tokens=tokens,
    )
    return templates.TemplateResponse("modules/appointments/queue.html", ctx)


@router.post("/queue/call/{token_id}")
def call_token(
    token_id: int,
    request: Request,
    user: User = Depends(require_permission("appointments.queue.manage")),
    session: Session = Depends(get_session),
):
    token = session.get(QueueToken, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    token.status = "Called"
    token.called_at = utc_now()
    session.add(token)
    session.commit()

    event_bus.emit(
        "token.called",
        {
            "token_id": token.id,
            "token_number": token.token_number,
            "room_number": token.room_number,
            "patient_name": token.patient_name,
        },
        user_id=user.id,
        username=user.username,
    )

    today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    tokens = session.exec(
        select(QueueToken).where(QueueToken.date == today_str).order_by(QueueToken.sequence)
    ).all()

    ctx = get_ui_context(request, user=user, session=session, queue_tokens=tokens)
    return templates.TemplateResponse("modules/appointments/queue.html", ctx)


@router.post("/queue/serve/{token_id}")
def serve_token(
    token_id: int,
    request: Request,
    user: User = Depends(require_permission("appointments.queue.manage")),
    session: Session = Depends(get_session),
):
    token = session.get(QueueToken, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    token.status = "Serving"
    session.add(token)

    if token.appointment_id:
        appt = session.get(Appointment, token.appointment_id)
        if appt and appt.status != "In-Consultation":
            appt.status = "In-Consultation"
            appt.consultation_started_at = utc_now()
            session.add(appt)

    session.commit()
    today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    tokens = session.exec(select(QueueToken).where(QueueToken.date == today_str).order_by(QueueToken.sequence)).all()
    ctx = get_ui_context(request, user=user, session=session, queue_tokens=tokens)
    return templates.TemplateResponse("modules/appointments/queue.html", ctx)


@router.post("/queue/done/{token_id}")
def done_token(
    token_id: int,
    request: Request,
    user: User = Depends(require_permission("appointments.queue.manage")),
    session: Session = Depends(get_session),
):
    token = session.get(QueueToken, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    token.status = "Done"
    session.add(token)

    if token.appointment_id:
        appt = session.get(Appointment, token.appointment_id)
        if appt and appt.status != "Completed":
            appt.status = "Completed"
            appt.completed_at = utc_now()
            session.add(appt)

    session.commit()
    today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    tokens = session.exec(select(QueueToken).where(QueueToken.date == today_str).order_by(QueueToken.sequence)).all()
    ctx = get_ui_context(request, user=user, session=session, queue_tokens=tokens)
    return templates.TemplateResponse("modules/appointments/queue.html", ctx)


@router.post("/queue/skip/{token_id}")
def skip_token(
    token_id: int,
    request: Request,
    user: User = Depends(require_permission("appointments.queue.manage")),
    session: Session = Depends(get_session),
):
    token = session.get(QueueToken, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    token.status = "Skipped"
    session.add(token)
    session.commit()

    today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    tokens = session.exec(select(QueueToken).where(QueueToken.date == today_str).order_by(QueueToken.sequence)).all()
    ctx = get_ui_context(request, user=user, session=session, queue_tokens=tokens)
    return templates.TemplateResponse("modules/appointments/queue.html", ctx)


# ---------------------------------------------------------------------------
# Statistics Widget
# ---------------------------------------------------------------------------

@router.get("/widgets/stats", response_class=HTMLResponse)
def stats_widget(
    request: Request,
    session: Session = Depends(get_session),
    date_str: Optional[str] = Query(None),
):
    cur_date = date_str or datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    stats = calculate_appointment_stats(cur_date, session)
    ctx = {"request": request, "stats": stats}
    return templates.TemplateResponse("modules/appointments/widgets/stats.html", ctx)
