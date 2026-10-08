import math
from typing import Optional
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlmodel import Session, col, or_, select
from medicore.core.database import get_session
from medicore.core.events import event_bus
from medicore.core.models import AuditLog, CustomFieldDefinition, User
from medicore.core.security import get_current_user, require_permission
from medicore.core.settings import settings_registry
from medicore.modules.patients.models import Patient
from medicore.modules.patients.schema import patient_schema
from medicore.ui.templating import get_ui_context, templates

router = APIRouter(prefix="/patients", tags=["Patients"])


@router.get("", response_class=HTMLResponse)
def list_patients_page(
    request: Request,
    user: User = Depends(require_permission("patients.patient.read")),
    session: Session = Depends(get_session),
    selected_id: Optional[int] = Query(None),
):
    effective_schema = patient_schema.get_effective_schema(session, user)

    # Initial query for first batch
    query = select(Patient).order_by(col(Patient.id).desc()).limit(15)
    patients = session.exec(query).all()
    total_count = session.exec(select(Patient)).all()

    # Pre-select first patient if none specified
    first_patient = None
    if selected_id:
        first_patient = session.get(Patient, selected_id)
    elif patients:
        first_patient = patients[0]

    audit_logs = []
    if first_patient:
        audit_logs = session.exec(
            select(AuditLog)
            .where(AuditLog.module == "patients", AuditLog.entity_id == str(first_patient.id))
            .order_by(col(AuditLog.timestamp).desc())
            .limit(10)
        ).all()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        schema=effective_schema,
        patients=patients,
        selected_patient=first_patient,
        audit_logs=audit_logs,
        total_count=len(total_count),
        page=1,
        page_size=15,
        total_pages=math.ceil(len(total_count) / 15) or 1,
    )
    return templates.TemplateResponse("modules/patients/index.html", ctx)


@router.get("/table", response_class=HTMLResponse)
def table_partial(
    request: Request,
    user: User = Depends(require_permission("patients.patient.read")),
    session: Session = Depends(get_session),
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    gender: Optional[str] = Query(None),
    blood_group: Optional[str] = Query(None),
    sort: Optional[str] = Query("-id"),
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=5, le=100),
    selected_id: Optional[int] = Query(None),
):
    effective_schema = patient_schema.get_effective_schema(session, user)
    stmt = select(Patient)

    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Patient.mrn.ilike(term),
                Patient.first_name.ilike(term),
                Patient.last_name.ilike(term),
                Patient.phone.ilike(term),
                Patient.email.ilike(term),
            )
        )

    if status and status != "All":
        stmt = stmt.where(Patient.status == status)
    if gender and gender != "All":
        stmt = stmt.where(Patient.gender == gender)
    if blood_group and blood_group != "All":
        stmt = stmt.where(Patient.blood_group == blood_group)

    # Sorting
    if sort == "mrn":
        stmt = stmt.order_by(col(Patient.mrn).asc())
    elif sort == "-mrn":
        stmt = stmt.order_by(col(Patient.mrn).desc())
    elif sort == "name":
        stmt = stmt.order_by(col(Patient.first_name).asc())
    elif sort == "dob":
        stmt = stmt.order_by(col(Patient.date_of_birth).asc())
    else:
        stmt = stmt.order_by(col(Patient.id).desc())

    all_matches = session.exec(stmt).all()
    total_count = len(all_matches)
    total_pages = max(1, math.ceil(total_count / page_size))

    offset = (page - 1) * page_size
    paged_patients = all_matches[offset : offset + page_size]

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        schema=effective_schema,
        patients=paged_patients,
        selected_patient_id=selected_id,
        total_count=total_count,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        q=q or "",
        status_filter=status or "",
        gender_filter=gender or "",
        blood_filter=blood_group or "",
    )
    return templates.TemplateResponse("modules/patients/table_partial.html", ctx)


@router.get("/detail/{patient_id}", response_class=HTMLResponse)
def patient_detail(
    patient_id: int,
    request: Request,
    user: User = Depends(require_permission("patients.patient.read")),
    session: Session = Depends(get_session),
):
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    effective_schema = patient_schema.get_effective_schema(session, user)

    audit_logs = session.exec(
        select(AuditLog)
        .where(AuditLog.module == "patients", AuditLog.entity_id == str(patient.id))
        .order_by(col(AuditLog.timestamp).desc())
        .limit(15)
    ).all()

    # Emit viewed event
    event_bus.emit(
        "patient.viewed",
        {"id": patient.id, "mrn": patient.mrn, "name": patient.full_name},
        user_id=user.id,
        username=user.username,
        ip_address=request.client.host if request.client else None,
    )

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        schema=effective_schema,
        patient=patient,
        audit_logs=audit_logs,
    )
    return templates.TemplateResponse("modules/patients/detail_pane.html", ctx)


@router.get("/new", response_class=HTMLResponse)
def new_patient_modal(
    request: Request,
    user: User = Depends(require_permission("patients.patient.create")),
    session: Session = Depends(get_session),
):
    effective_schema = patient_schema.get_effective_schema(session, user)
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        schema=effective_schema,
        patient=None,
        action_url="/patients",
        modal_title="Register New Patient",
    )
    return templates.TemplateResponse("modules/patients/form_modal.html", ctx)


@router.get("/edit/{patient_id}", response_class=HTMLResponse)
def edit_patient_modal(
    patient_id: int,
    request: Request,
    user: User = Depends(require_permission("patients.patient.update")),
    session: Session = Depends(get_session),
):
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    effective_schema = patient_schema.get_effective_schema(session, user)
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        schema=effective_schema,
        patient=patient,
        action_url=f"/patients/{patient.id}",
        modal_title=f"Edit Patient - {patient.mrn}",
    )
    return templates.TemplateResponse("modules/patients/form_modal.html", ctx)


@router.post("/check-duplicate", response_class=HTMLResponse)
async def check_duplicate(
    request: Request,
    session: Session = Depends(get_session),
):
    form = await request.form()
    phone = str(form.get("phone", "")).strip()
    first_name = str(form.get("first_name", "")).strip()
    last_name = str(form.get("last_name", "")).strip()
    dob = str(form.get("date_of_birth", "")).strip()
    exclude_id = form.get("id")

    if not phone and not (first_name and last_name and dob):
        return HTMLResponse("")

    stmt = select(Patient)
    clauses = []
    if phone and len(phone) >= 7:
        clauses.append(Patient.phone == phone)
    if first_name and last_name and dob:
        clauses.append(
            (Patient.first_name.ilike(first_name))
            & (Patient.last_name.ilike(last_name))
            & (Patient.date_of_birth == dob)
        )

    if not clauses:
        return HTMLResponse("")

    stmt = stmt.where(or_(*clauses))
    if exclude_id and str(exclude_id).isdigit():
        stmt = stmt.where(Patient.id != int(exclude_id))

    matches = session.exec(stmt.limit(3)).all()
    if not matches:
        return HTMLResponse("")

    ctx = {"request": request, "matches": matches}
    return templates.TemplateResponse("modules/patients/duplicate_warning.html", ctx)


@router.post("", response_class=HTMLResponse)
async def create_patient(
    request: Request,
    user: User = Depends(require_permission("patients.patient.create")),
    session: Session = Depends(get_session),
):
    form = await request.form()

    # Extract base fields
    first_name = str(form.get("first_name", "")).strip()
    last_name = str(form.get("last_name", "")).strip()
    dob = str(form.get("date_of_birth", "")).strip()
    phone = str(form.get("phone", "")).strip()

    if not first_name or not last_name or not dob or not phone:
        raise HTTPException(status_code=400, detail="Missing required patient fields")

    # Server-enforced duplicate detection guard
    dup_clauses = []
    if phone and len(phone) >= 7:
        dup_clauses.append(Patient.phone == phone)
    if first_name and last_name and dob:
        dup_clauses.append(
            (Patient.first_name.ilike(first_name))
            & (Patient.last_name.ilike(last_name))
            & (Patient.date_of_birth == dob)
        )

    override_duplicate = str(form.get("override_duplicate", "")).lower() in ("true", "1", "yes", "on")
    override_reason = str(form.get("override_reason", "")).strip()

    if dup_clauses:
        dup_matches = session.exec(select(Patient).where(or_(*dup_clauses))).all()
        if dup_matches and not (override_duplicate and override_reason):
            match_str = ", ".join([f"{m.full_name} ({m.mrn}, Phone: {m.phone})" for m in dup_matches[:2]])
            effective_schema = patient_schema.get_effective_schema(session, user)
            ctx = get_ui_context(
                request,
                user=user,
                session=session,
                schema=effective_schema,
                patient=None,
                action_url="/patients",
                modal_title="Register New Patient",
                duplicate_warning_msg=f"Duplicate Record Alert: Matching patient record(s) found ({match_str}). Confirmation and reason are required to proceed.",
                form_override_reason=override_reason,
            )
            return templates.TemplateResponse("modules/patients/form_modal.html", ctx, status_code=status.HTTP_409_CONFLICT)

    # Generate MRN
    mrn = settings_registry.generate_mrn()

    # Custom fields collection
    custom_defs = session.exec(select(CustomFieldDefinition).where(CustomFieldDefinition.entity_name == "patient")).all()
    custom_data = {}
    for c_def in custom_defs:
        key = f"custom_fields.{c_def.field_name}"
        if key in form:
            val = form.get(key)
            if c_def.field_type == "boolean":
                custom_data[c_def.field_name] = val in ("true", "1", "on", "yes")
            else:
                custom_data[c_def.field_name] = str(val).strip()

    patient = Patient(
        mrn=mrn,
        first_name=first_name,
        last_name=last_name,
        date_of_birth=dob,
        gender=str(form.get("gender", "Female")),
        blood_group=str(form.get("blood_group", "")).strip() or None,
        phone=phone,
        email=str(form.get("email", "")).strip() or None,
        address=str(form.get("address", "")).strip() or None,
        emergency_contact_name=str(form.get("emergency_contact_name", "")).strip() or None,
        emergency_contact_phone=str(form.get("emergency_contact_phone", "")).strip() or None,
        allergies=str(form.get("allergies", "")).strip() or None,
        status=str(form.get("status", "Active")),
        custom_fields=custom_data,
    )

    session.add(patient)
    session.commit()
    session.refresh(patient)

    # If duplicate was overridden, write dedicated audit log
    if dup_clauses and override_duplicate and override_reason:
        existing_matches = session.exec(select(Patient).where(or_(*dup_clauses)).where(Patient.id != patient.id)).all()
        if existing_matches:
            audit = AuditLog(
                user_id=user.id,
                username=user.username,
                module="patients",
                entity="patient",
                entity_id=str(patient.id),
                action="DUPLICATE_OVERRIDE",
                changes={
                    "reason": override_reason,
                    "matched_mrns": [m.mrn for m in existing_matches],
                    "matched_ids": [m.id for m in existing_matches],
                },
            )
            session.add(audit)
            session.commit()
            event_bus.emit(
                "patient.duplicate_overridden",
                {"id": patient.id, "reason": override_reason, "matched_mrns": [m.mrn for m in existing_matches]},
                user_id=user.id,
                username=user.username,
            )

    # Emit event
    event_bus.emit(
        "patient.created",
        {
            "id": patient.id,
            "mrn": patient.mrn,
            "full_name": patient.full_name,
            "status": patient.status,
            "phone": patient.phone,
        },
        user_id=user.id,
        username=user.username,
        ip_address=request.client.host if request.client else None,
    )

    # Return HTMX response with trigger to refresh table and close modal
    response = HTMLResponse(
        content=f"""
        <div id="modal-container"></div>
        <script>
            window.dispatchEvent(new CustomEvent('patient-created', {{ detail: {{ id: {patient.id} }} }}));
            htmx.ajax('GET', '/patients/table?selected_id={patient.id}', {{target: '#patients-table-container', swap: 'innerHTML'}});
            htmx.ajax('GET', '/patients/detail/{patient.id}', {{target: '#detail-pane', swap: 'innerHTML'}});
        </script>
        """
    )
    response.headers["HX-Trigger"] = "patientListChanged"
    return response


@router.post("/{patient_id}", response_class=HTMLResponse)
async def update_patient(
    patient_id: int,
    request: Request,
    user: User = Depends(require_permission("patients.patient.update")),
    session: Session = Depends(get_session),
):
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    form = await request.form()
    patient.first_name = str(form.get("first_name", patient.first_name)).strip()
    patient.last_name = str(form.get("last_name", patient.last_name)).strip()
    patient.date_of_birth = str(form.get("date_of_birth", patient.date_of_birth)).strip()
    patient.gender = str(form.get("gender", patient.gender)).strip()
    patient.blood_group = str(form.get("blood_group", "")).strip() or None
    patient.phone = str(form.get("phone", patient.phone)).strip()
    patient.email = str(form.get("email", "")).strip() or None
    patient.address = str(form.get("address", "")).strip() or None
    patient.emergency_contact_name = str(form.get("emergency_contact_name", "")).strip() or None
    patient.emergency_contact_phone = str(form.get("emergency_contact_phone", "")).strip() or None
    patient.allergies = str(form.get("allergies", "")).strip() or None
    patient.status = str(form.get("status", patient.status)).strip()

    # Update custom fields
    custom_defs = session.exec(select(CustomFieldDefinition).where(CustomFieldDefinition.entity_name == "patient")).all()
    custom_data = dict(patient.custom_fields or {})
    for c_def in custom_defs:
        key = f"custom_fields.{c_def.field_name}"
        if key in form:
            val = form.get(key)
            if c_def.field_type == "boolean":
                custom_data[c_def.field_name] = val in ("true", "1", "on", "yes")
            else:
                custom_data[c_def.field_name] = str(val).strip()
    patient.custom_fields = custom_data

    session.add(patient)
    session.commit()
    session.refresh(patient)

    # Emit updated event
    event_bus.emit(
        "patient.updated",
        {
            "id": patient.id,
            "mrn": patient.mrn,
            "full_name": patient.full_name,
            "status": patient.status,
        },
        user_id=user.id,
        username=user.username,
        ip_address=request.client.host if request.client else None,
    )

    response = HTMLResponse(
        content=f"""
        <div id="modal-container"></div>
        <script>
            htmx.ajax('GET', '/patients/table?selected_id={patient.id}', {{target: '#patients-table-container', swap: 'innerHTML'}});
            htmx.ajax('GET', '/patients/detail/{patient.id}', {{target: '#detail-pane', swap: 'innerHTML'}});
        </script>
        """
    )
    return response


@router.delete("/{patient_id}", response_class=HTMLResponse)
def delete_patient(
    patient_id: int,
    request: Request,
    user: User = Depends(require_permission("patients.patient.delete")),
    session: Session = Depends(get_session),
):
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    patient.status = "Archived"
    session.add(patient)
    session.commit()

    event_bus.emit(
        "patient.deleted",
        {"id": patient.id, "mrn": patient.mrn, "full_name": patient.full_name},
        user_id=user.id,
        username=user.username,
        ip_address=request.client.host if request.client else None,
    )

    return HTMLResponse(
        content="""
        <script>
            htmx.ajax('GET', '/patients/table', {target: '#patients-table-container', swap: 'innerHTML'});
            document.getElementById('detail-pane').innerHTML = '<div class="p-8 text-center text-slate-400">Patient archived. Select another patient from the table.</div>';
        </script>
        """
    )
