from datetime import date, datetime
import json
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlmodel import Session, col, desc, func, or_, select

from medicore.core.database import get_session
from medicore.core.models import AuditLog, User
from medicore.core.security import get_current_user, require_permission, user_has_permission
from medicore.modules.laboratory.models import (
    LabOrder,
    LabOrderItem,
    LabReport,
    Parameter,
    ReferenceRange,
    Result,
    Specimen,
    TestCatalog,
    TestPanel,
    utc_now,
)
from medicore.modules.laboratory.service import (
    acknowledge_critical_value,
    amend_approved_report,
    approve_lab_order,
    calculate_tat_deadline,
    check_and_update_tat_breach,
    collect_specimen,
    enter_order_results,
    generate_lab_report_pdf,
    generate_order_number,
    generate_report_number,
    generate_specimen_barcode,
    get_patient_lab_history,
    get_previous_result_delta,
    import_instrument_results_csv,
    receive_specimen,
    reject_specimen,
)
from medicore.ui.templating import get_ui_context, templates

router = APIRouter(prefix="/laboratory", tags=["Laboratory"])


# =========================================================================
# WORKLIST DASHBOARD
# =========================================================================


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def laboratory_worklist(
    request: Request,
    priority: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    breached_only: Optional[bool] = Query(False),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Laboratory Worklist dashboard with priority, TAT breach, and department filtering."""
    query = select(LabOrder)

    if priority and priority != "All":
        query = query.where(LabOrder.priority == priority)
    if department and department != "All":
        query = query.where(LabOrder.department == department)
    if status_filter and status_filter != "All":
        query = query.where(LabOrder.status == status_filter)
    if breached_only:
        query = query.where(LabOrder.is_tat_breached == True)
    if search:
        search_term = f"%{search.strip()}%"
        query = query.where(
            or_(
                col(LabOrder.order_no).ilike(search_term),
                col(LabOrder.patient_name).ilike(search_term),
                col(LabOrder.patient_mrn).ilike(search_term),
                col(LabOrder.doctor_name).ilike(search_term),
            )
        )

    # Calculate metrics
    all_orders = session.exec(select(LabOrder)).all()
    # Update TAT breaches dynamically
    for o in all_orders:
        check_and_update_tat_breach(o)
    session.commit()

    total_orders = len(all_orders)
    stat_urgent_pending = sum(
        1 for o in all_orders if o.priority in ["STAT", "Urgent"] and o.status not in ["approved", "cancelled"]
    )
    tat_breached_count = sum(
        1 for o in all_orders if o.is_tat_breached and o.status not in ["approved", "cancelled"]
    )
    unapproved_count = sum(
        1 for o in all_orders if o.status == "result_entered"
    )
    approved_count = sum(
        1 for o in all_orders if o.status == "approved"
    )

    # Critical results count requiring acknowledgement
    critical_unack_count = session.exec(
        select(func.count(Result.id)).where(
            Result.is_critical == True,
            Result.critical_acknowledged_by == None,
        )
    ).one()

    # Pagination & Ordering: STAT priority first, then ordered_at desc
    total_matching = len(session.exec(query).all())
    query = query.order_by(
        desc(LabOrder.priority == "STAT"),
        desc(LabOrder.priority == "Urgent"),
        desc(LabOrder.ordered_at),
    ).offset((page - 1) * limit).limit(limit)

    orders = session.exec(query).all()

    # Get specimens mapping for orders
    order_ids = [o.id for o in orders if o.id]
    specimens_map = {}
    if order_ids:
        specs = session.exec(
            select(Specimen).where(col(Specimen.lab_order_id).in_(order_ids))
        ).all()
        for s in specs:
            specimens_map.setdefault(s.lab_order_id, []).append(s)

    departments = ["Hematology", "Biochemistry", "Microbiology", "Pathology", "Urinalysis", "Serology", "Immunology"]

    ctx = get_ui_context(request, title="Laboratory Worklist", current_user=current_user)
    ctx.update({
        "orders": orders,
        "specimens_map": specimens_map,
        "departments": departments,
        "total_orders": total_orders,
        "stat_urgent_pending": stat_urgent_pending,
        "tat_breached_count": tat_breached_count,
        "unapproved_count": unapproved_count,
        "approved_count": approved_count,
        "critical_unack_count": critical_unack_count,
        "current_priority": priority or "All",
        "current_department": department or "All",
        "current_status": status_filter or "All",
        "breached_only": breached_only,
        "search": search or "",
        "page": page,
        "total_pages": max(1, (total_matching + limit - 1) // limit),
        "total_matching": total_matching,
    })

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("modules/laboratory/partials/worklist_table.html", ctx)

    return templates.TemplateResponse("modules/laboratory/index.html", ctx)


@router.get("/worklist/table", response_class=HTMLResponse)
def laboratory_worklist_table(
    request: Request,
    priority: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    breached_only: Optional[bool] = Query(False),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """HTMX partial for refreshing worklist table."""
    return laboratory_worklist(
        request=request,
        priority=priority,
        department=department,
        status_filter=status_filter,
        breached_only=breached_only,
        search=search,
        page=page,
        limit=limit,
        session=session,
        current_user=current_user,
    )


# =========================================================================
# MANUAL REQUISITION / WALK-IN CREATION
# =========================================================================


@router.get("/orders/new", response_class=HTMLResponse)
def new_order_form(
    request: Request,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Form to manually order lab tests."""
    tests = session.exec(select(TestCatalog).where(TestCatalog.is_active == True).order_by(TestCatalog.name)).all()
    ctx = get_ui_context(request, title="New Lab Requisition", current_user=current_user)
    ctx.update({"tests": tests})
    return templates.TemplateResponse("modules/laboratory/order_form.html", ctx)


@router.post("/orders/new")
def create_lab_requisition(
    request: Request,
    patient_name: str = Form(...),
    patient_mrn: str = Form(...),
    patient_gender: str = Form("Other"),
    priority: str = Form("Routine"),
    doctor_name: Optional[str] = Form("Attending Physician"),
    test_id: int = Form(...),
    clinical_notes: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Process manual requisition submission."""
    test = session.get(TestCatalog, test_id)
    if not test:
        raise HTTPException(status_code=404, detail="Test not found")

    ordered_at = utc_now()
    tat_deadline = calculate_tat_deadline(ordered_at, priority, test.tat_hours)

    order = LabOrder(
        order_no=generate_order_number(session),
        order_source="walkin",
        patient_id=1,
        patient_name=patient_name,
        patient_mrn=patient_mrn,
        patient_gender=patient_gender,
        doctor_name=doctor_name,
        priority=priority,
        status="ordered",
        department=test.department,
        ordered_at=ordered_at,
        tat_deadline=tat_deadline,
        clinical_notes=clinical_notes,
        total_price=test.price,
    )
    session.add(order)
    session.flush()

    barcode = generate_specimen_barcode(session)
    specimen = Specimen(
        lab_order_id=order.id,
        barcode=barcode,
        specimen_type=test.specimen_type,
        status="pending_collection",
        timeline_json=json.dumps([{
            "status": "pending_collection",
            "timestamp": ordered_at.isoformat(),
            "user": current_user.username if current_user else "Receptionist",
            "notes": "Requisition booked",
        }]),
    )
    session.add(specimen)
    session.flush()

    item = LabOrderItem(
        lab_order_id=order.id,
        test_id=test.id,
        test_code=test.code,
        test_name=test.name,
        department=test.department,
        specimen_type=test.specimen_type,
        price=test.price,
        specimen_id=specimen.id,
    )
    session.add(item)

    report = LabReport(
        lab_order_id=order.id,
        report_no=generate_report_number(session),
        version=1,
        status="draft",
    )
    session.add(report)
    session.commit()

    return RedirectResponse(url="/laboratory", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================================
# SPECIMEN WORKFLOW (COLLECT, RECEIVE, REJECT, BARCODE LABEL)
# =========================================================================


@router.post("/specimen/{specimen_id}/collect")
def collect_specimen_endpoint(
    specimen_id: int,
    request: Request,
    notes: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Mark biological specimen as collected."""
    user_name = current_user.username if current_user else "Phlebotomist"
    try:
        collect_specimen(session, specimen_id, user_name, notes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(url="/laboratory", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/specimen/{specimen_id}/receive")
def receive_specimen_endpoint(
    specimen_id: int,
    request: Request,
    notes: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Mark specimen as received inside central laboratory."""
    user_name = current_user.username if current_user else "LabTechnician"
    try:
        receive_specimen(session, specimen_id, user_name, notes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(url="/laboratory", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/specimen/{specimen_id}/reject")
def reject_specimen_endpoint(
    specimen_id: int,
    request: Request,
    rejection_reason: str = Form(...),
    recollect_requested: bool = Form(True),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Reject compromised specimen (hemolyzed, clotted, insufficient volume) and request recollect."""
    user_name = current_user.username if current_user else "LabTechnician"
    try:
        reject_specimen(session, specimen_id, user_name, rejection_reason, recollect_requested)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(url="/laboratory", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/specimen/{specimen_id}/label", response_class=HTMLResponse)
def print_specimen_label(
    specimen_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Renders printable barcode label modal with patient demographics."""
    specimen = session.get(Specimen, specimen_id)
    if not specimen:
        raise HTTPException(status_code=404, detail="Specimen not found")

    order = session.get(LabOrder, specimen.lab_order_id)
    ctx = get_ui_context(request, title="Specimen Barcode Label", current_user=current_user)
    ctx.update({
        "specimen": specimen,
        "order": order,
    })
    return templates.TemplateResponse("modules/laboratory/partials/specimen_label.html", ctx)


# =========================================================================
# RESULT ENTRY GRID & VALIDATION
# =========================================================================


@router.get("/order/{order_id}/results", response_class=HTMLResponse)
def result_entry_screen(
    order_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Result entry screen with parameter grids, reference ranges, auto-calc, and previous deltas."""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Lab order not found")

    items = session.exec(
        select(LabOrderItem).where(LabOrderItem.lab_order_id == order.id)
    ).all()

    # Get all parameters for all tests in this order
    test_ids = [it.test_id for it in items]
    parameters: List[Parameter] = []
    if test_ids:
        parameters = session.exec(
            select(Parameter).where(col(Parameter.test_id).in_(test_ids)).order_by(Parameter.display_order)
        ).all()

    # Existing results mapped by parameter_id
    existing_results = session.exec(
        select(Result).where(Result.lab_order_id == order.id)
    ).all()
    results_map = {r.parameter_id: r for r in existing_results}

    # Reference ranges mapped by parameter_id
    param_ids = [p.id for p in parameters if p.id]
    ranges_map: Dict[int, List[ReferenceRange]] = {}
    if param_ids:
        all_ranges = session.exec(
            select(ReferenceRange).where(col(ReferenceRange.parameter_id).in_(param_ids))
        ).all()
        for rng in all_ranges:
            ranges_map.setdefault(rng.parameter_id, []).append(rng)

    # Previous results deltas
    prev_deltas = {}
    for p in parameters:
        p_val, p_dt = get_previous_result_delta(
            session=session,
            patient_id=order.patient_id,
            parameter_id=p.id,
            exclude_order_id=order.id,
        )
        if p_val:
            prev_deltas[p.id] = {"value": p_val, "date": p_dt}

    report = session.exec(
        select(LabReport).where(LabReport.lab_order_id == order.id)
    ).first()

    ctx = get_ui_context(request, title=f"Result Entry: {order.order_no}", current_user=current_user)
    ctx.update({
        "order": order,
        "items": items,
        "parameters": parameters,
        "results_map": results_map,
        "ranges_map": ranges_map,
        "prev_deltas": prev_deltas,
        "report": report,
    })
    return templates.TemplateResponse("modules/laboratory/result_entry.html", ctx)


@router.post("/order/{order_id}/results/save")
async def save_order_results_endpoint(
    order_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Save technician entered results, compute derived parameters, auto-flag, and emit critical alert."""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Lab order not found")

    form_data = await request.form()
    user_name = current_user.username if current_user else "Alex Rivera (Tech)"

    results_data: List[Dict[str, Any]] = []
    for key, val in form_data.items():
        if key.startswith("param_"):
            try:
                param_id = int(key.replace("param_", ""))
                val_str = str(val).strip()
                if val_str:
                    results_data.append({"parameter_id": param_id, "value_text": val_str})
            except ValueError:
                continue

    try:
        order, results, has_critical = enter_order_results(
            session=session,
            lab_order_id=order_id,
            results_data=results_data,
            user_name=user_name,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse(url=f"/laboratory/order/{order_id}/results", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================================
# TWO-STEP APPROVAL & AMENDMENT WORKFLOW
# =========================================================================


@router.post("/order/{order_id}/approve")
def approve_order_endpoint(
    order_id: int,
    request: Request,
    summary_notes: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Pathologist validates and authorizes report. Locks results and emits result.approved & lab.charge."""
    pathologist_name = current_user.username if current_user else "Dr. Victor Vance, MD"
    try:
        report = approve_lab_order(session, order_id, pathologist_name, summary_notes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse(url=f"/laboratory/order/{order_id}/report", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/order/{order_id}/amend", response_class=HTMLResponse)
def amend_order_modal(
    order_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Amendment modal with reason input and editable parameter values."""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    report = session.exec(
        select(LabReport).where(LabReport.lab_order_id == order.id)
    ).first()

    results = session.exec(
        select(Result).where(Result.lab_order_id == order.id)
    ).all()

    ctx = get_ui_context(request, title=f"Amend Report: {order.order_no}", current_user=current_user)
    ctx.update({
        "order": order,
        "report": report,
        "results": results,
    })
    return templates.TemplateResponse("modules/laboratory/partials/amend_modal.html", ctx)


@router.post("/order/{order_id}/amend")
async def process_amendment(
    order_id: int,
    request: Request,
    amendment_reason: str = Form(...),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Amend previously approved report: bumps version, flags corrected report notice, updates values."""
    pathologist_name = current_user.username if current_user else "Dr. Victor Vance, MD"
    form_data = await request.form()

    updated_results: List[Dict[str, Any]] = []
    for key, val in form_data.items():
        if key.startswith("param_"):
            try:
                param_id = int(key.replace("param_", ""))
                val_str = str(val).strip()
                if val_str:
                    updated_results.append({"parameter_id": param_id, "value_text": val_str})
            except ValueError:
                continue

    try:
        report = amend_approved_report(
            session=session,
            lab_order_id=order_id,
            pathologist_name=pathologist_name,
            amendment_reason=amendment_reason,
            updated_results_data=updated_results,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse(url=f"/laboratory/order/{order_id}/report", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================================
# CRITICAL VALUE ACKNOWLEDGEMENT
# =========================================================================


@router.post("/result/{result_id}/acknowledge")
def acknowledge_critical_result(
    result_id: int,
    request: Request,
    notes: Optional[str] = Form(None),
    doctor_name: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Physician acknowledges critical laboratory value."""
    doc = doctor_name or (current_user.username if current_user else "Attending Physician")
    try:
        res = acknowledge_critical_value(session, result_id, doc, notes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/laboratory/order/{res.lab_order_id}/results", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================================
# LAB REPORT VIEW & BRANDED PDF
# =========================================================================


@router.get("/order/{order_id}/report", response_class=HTMLResponse)
def view_lab_report(
    order_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Diagnostic laboratory report view."""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Lab order not found")

    report = session.exec(
        select(LabReport).where(LabReport.lab_order_id == order.id)
    ).first()

    results = session.exec(
        select(Result).where(Result.lab_order_id == order.id)
    ).all()

    specimens = session.exec(
        select(Specimen).where(Specimen.lab_order_id == order.id)
    ).all()

    items = session.exec(
        select(LabOrderItem).where(LabOrderItem.lab_order_id == order.id)
    ).all()

    ctx = get_ui_context(request, title=f"Lab Report: {order.order_no}", current_user=current_user)
    ctx.update({
        "order": order,
        "report": report,
        "results": results,
        "specimens": specimens,
        "items": items,
    })
    return templates.TemplateResponse("modules/laboratory/report_detail.html", ctx)


@router.get("/order/{order_id}/report/pdf")
def download_lab_report_pdf(
    order_id: int,
    session: Session = Depends(get_session),
):
    """Generates and downloads branded diagnostic laboratory report PDF via ReportLab."""
    order = session.get(LabOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Lab order not found")

    pdf_bytes = generate_lab_report_pdf(session, order_id)
    filename = f"Diagnostic_Lab_Report_{order.order_no}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# =========================================================================
# INSTRUMENT CSV IMPORT
# =========================================================================


@router.get("/import", response_class=HTMLResponse)
def import_instrument_form(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user),
):
    """Form to upload CSV files from automated laboratory analyzers."""
    ctx = get_ui_context(request, title="Analyzer CSV Import", current_user=current_user)
    return templates.TemplateResponse("modules/laboratory/import_csv.html", ctx)


@router.post("/import", response_class=HTMLResponse)
async def process_instrument_csv(
    request: Request,
    file: Optional[UploadFile] = File(None),
    csv_text: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Process analyzer CSV data stream."""
    content = ""
    if file and file.filename:
        raw_bytes = await file.read()
        content = raw_bytes.decode("utf-8", errors="ignore")
    elif csv_text:
        content = csv_text

    if not content.strip():
        raise HTTPException(status_code=400, detail="No CSV data provided.")

    user_name = current_user.username if current_user else "LabTechnician"
    res = import_instrument_results_csv(session, content, user_name)

    ctx = get_ui_context(request, title="Analyzer Import Results", current_user=current_user)
    ctx.update({"result": res})
    return templates.TemplateResponse("modules/laboratory/import_result.html", ctx)


# =========================================================================
# PATIENT 360 LABS INTEGRATION
# =========================================================================


@router.get("/patient/{patient_id}/results", response_class=HTMLResponse)
def patient_labs_history(
    patient_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Patient 360 Labs Tab: Comprehensive investigation history and trend charts."""
    history = get_patient_lab_history(session, patient_id)
    ctx = get_ui_context(request, title="Patient Lab History", current_user=current_user)
    ctx.update({
        "patient_id": patient_id,
        "orders": history["orders"],
        "results": history["results"],
        "trends": history["trends"],
        "total_orders": history["total_orders"],
    })
    return templates.TemplateResponse("modules/laboratory/partials/patient_labs_tab.html", ctx)


# =========================================================================
# TEST CATALOG & REFERENCE RANGE ADMIN
# =========================================================================


@router.get("/catalog", response_class=HTMLResponse)
def test_catalog_admin(
    request: Request,
    department: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Catalog manager showing test directory, LOINC mapping, and reference intervals."""
    query = select(TestCatalog).where(TestCatalog.is_active == True)
    if department and department != "All":
        query = query.where(TestCatalog.department == department)
    if search:
        st = f"%{search.strip()}%"
        query = query.where(
            or_(
                col(TestCatalog.code).ilike(st),
                col(TestCatalog.name).ilike(st),
                col(TestCatalog.specimen_type).ilike(st),
            )
        )

    tests = session.exec(query.order_by(TestCatalog.department, TestCatalog.name)).all()

    # Query all parameters count per test
    test_ids = [t.id for t in tests if t.id]
    param_counts: Dict[int, int] = {}
    if test_ids:
        rows = session.exec(
            select(Parameter.test_id, func.count(Parameter.id)).where(col(Parameter.test_id).in_(test_ids)).group_by(Parameter.test_id)
        ).all()
        for t_id, cnt in rows:
            param_counts[t_id] = cnt

    departments = ["Hematology", "Biochemistry", "Microbiology", "Pathology", "Urinalysis", "Serology", "Immunology"]

    ctx = get_ui_context(request, title="Lab Test Catalog", current_user=current_user)
    ctx.update({
        "tests": tests,
        "param_counts": param_counts,
        "departments": departments,
        "current_department": department or "All",
        "search": search or "",
    })
    return templates.TemplateResponse("modules/laboratory/catalog_admin.html", ctx)


@router.get("/catalog/test/{test_id}", response_class=HTMLResponse)
def test_detail_admin(
    test_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Test details with parameter list, LOINC codes, and reference range editor."""
    test = session.get(TestCatalog, test_id)
    if not test:
        raise HTTPException(status_code=404, detail="Test not found")

    parameters = session.exec(
        select(Parameter).where(Parameter.test_id == test.id).order_by(Parameter.display_order)
    ).all()

    param_ids = [p.id for p in parameters if p.id]
    ranges_map: Dict[int, List[ReferenceRange]] = {}
    if param_ids:
        all_ranges = session.exec(
            select(ReferenceRange).where(col(ReferenceRange.parameter_id).in_(param_ids))
        ).all()
        for r in all_ranges:
            ranges_map.setdefault(r.parameter_id, []).append(r)

    ctx = get_ui_context(request, title=f"Test: {test.name}", current_user=current_user)
    ctx.update({
        "test": test,
        "parameters": parameters,
        "ranges_map": ranges_map,
    })
    return templates.TemplateResponse("modules/laboratory/test_detail.html", ctx)


@router.post("/catalog/parameter/{parameter_id}/range")
def save_parameter_range(
    parameter_id: int,
    request: Request,
    gender: str = Form("All"),
    age_min_years: float = Form(0.0),
    age_max_years: float = Form(120.0),
    low_normal: Optional[float] = Form(None),
    high_normal: Optional[float] = Form(None),
    critical_low: Optional[float] = Form(None),
    critical_high: Optional[float] = Form(None),
    text_normal: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Create or update reference range for a parameter."""
    param = session.get(Parameter, parameter_id)
    if not param:
        raise HTTPException(status_code=404, detail="Parameter not found")

    rng = ReferenceRange(
        parameter_id=param.id,
        gender=gender,
        age_min_years=age_min_years,
        age_max_years=age_max_years,
        low_normal=low_normal,
        high_normal=high_normal,
        critical_low=critical_low,
        critical_high=critical_high,
        text_normal=text_normal,
    )
    session.add(rng)
    session.commit()
    return RedirectResponse(url=f"/laboratory/catalog/test/{param.test_id}", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================================
# DASHBOARD WIDGET
# =========================================================================


@router.get("/widget/worklist", response_class=HTMLResponse)
def lab_dashboard_widget(
    request: Request,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Dashboard widget showing STAT requisitions, pending collections, and TAT breaches."""
    stat_orders = session.exec(
        select(LabOrder)
        .where(
            LabOrder.priority == "STAT",
            col(LabOrder.status).notin_(["approved", "cancelled"]),
        )
        .order_by(LabOrder.ordered_at)
        .limit(5)
    ).all()

    tat_breached = session.exec(
        select(LabOrder)
        .where(
            LabOrder.is_tat_breached == True,
            col(LabOrder.status).notin_(["approved", "cancelled"]),
        )
        .limit(5)
    ).all()

    ctx = get_ui_context(request, title="Laboratory Quick View", current_user=current_user)
    ctx.update({
        "stat_orders": stat_orders,
        "tat_breached": tat_breached,
    })
    return templates.TemplateResponse("modules/laboratory/widgets/worklist.html", ctx)
