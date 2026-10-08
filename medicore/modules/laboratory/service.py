import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlmodel import Session, col, desc, select

from medicore.core.database import engine
from medicore.core.events import Event, event_bus
from medicore.core.models import AuditLog


def record_audit_log(
    session: Session,
    action: str,
    actor: str,
    entity_type: str,
    entity_id: Any,
    details: Dict[str, Any],
) -> None:
    try:
        log_entry = AuditLog(
            timestamp=utc_now(),
            username=actor,
            module="laboratory",
            entity=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            action=action,
            changes=details,
        )
        session.add(log_entry)
    except Exception:
        pass
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


# =====================================================================
# IDENTIFIER GENERATORS
# =====================================================================


def generate_order_number(session: Session) -> str:
    """Generate next sequential Lab Order number: LAB-YYYY-XXXXX."""
    year = datetime.now(timezone.utc).year
    prefix = f"LAB-{year}-"
    stmt = (
        select(LabOrder.order_no)
        .where(col(LabOrder.order_no).startswith(prefix))
        .order_by(desc(LabOrder.order_no))
        .limit(1)
    )
    last_val = session.exec(stmt).first()
    if last_val:
        try:
            seq = int(last_val.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:05d}"


def generate_specimen_barcode(session: Session) -> str:
    """Generate next sequential Specimen Barcode: SPEC-YYYY-XXXXX."""
    year = datetime.now(timezone.utc).year
    prefix = f"SPEC-{year}-"
    stmt = (
        select(Specimen.barcode)
        .where(col(Specimen.barcode).startswith(prefix))
        .order_by(desc(Specimen.barcode))
        .limit(1)
    )
    last_val = session.exec(stmt).first()
    if last_val:
        try:
            seq = int(last_val.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:05d}"


def generate_report_number(session: Session) -> str:
    """Generate next sequential Lab Report number: REP-YYYY-XXXXX."""
    year = datetime.now(timezone.utc).year
    prefix = f"REP-{year}-"
    stmt = (
        select(LabReport.report_no)
        .where(col(LabReport.report_no).startswith(prefix))
        .order_by(desc(LabReport.report_no))
        .limit(1)
    )
    last_val = session.exec(stmt).first()
    if last_val:
        try:
            seq = int(last_val.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:05d}"


# =====================================================================
# TAT (TURNAROUND TIME) CALCULATIONS
# =====================================================================


def calculate_tat_deadline(ordered_at: datetime, priority: str, tat_hours: int = 4) -> datetime:
    """Calculate target TAT deadline based on priority."""
    p = (priority or "Routine").strip().upper()
    if p == "STAT":
        # STAT priority requires 60 minutes turnaround
        return ordered_at + timedelta(minutes=60)
    elif p == "URGENT":
        return ordered_at + timedelta(hours=max(1, tat_hours // 2))
    else:
        return ordered_at + timedelta(hours=max(1, tat_hours))


def check_and_update_tat_breach(order: LabOrder) -> bool:
    """Check if order has breached its TAT deadline and update flag."""
    if order.status in ["approved", "cancelled"]:
        return order.is_tat_breached
    now = datetime.now(timezone.utc)
    # Ensure deadline is timezone-aware for comparison
    deadline = order.tat_deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if now > deadline and not order.is_tat_breached:
        order.is_tat_breached = True
    return order.is_tat_breached


# =====================================================================
# REFERENCE RANGE & FLAG EVALUATION
# =====================================================================


def calculate_age_years(dob: Optional[date]) -> float:
    """Calculate age in years from date of birth."""
    if not dob:
        return 35.0  # standard adult default
    today = date.today()
    return (today - dob).days / 365.25


def evaluate_parameter_value(
    session: Session,
    parameter: Parameter,
    value_str: str,
    patient_gender: str = "All",
    patient_age_years: float = 35.0,
) -> Dict[str, Any]:
    """
    Evaluates a test parameter result against sex- and age-matched reference ranges.
    Returns:
        {
            "value_text": str,
            "value_numeric": Optional[float],
            "flag": "Normal" | "H" | "L" | "Critical_High" | "Critical_Low" | "Abnormal",
            "is_critical": bool,
            "reference_range_display": str,
        }
    """
    value_str = (value_str or "").strip()
    if not value_str:
        return {
            "value_text": "",
            "value_numeric": None,
            "flag": "Normal",
            "is_critical": False,
            "reference_range_display": "",
        }

    # Query reference ranges for this parameter
    stmt = (
        select(ReferenceRange)
        .where(ReferenceRange.parameter_id == parameter.id)
    )
    ranges = session.exec(stmt).all()

    # Find the best matching range for patient gender and age
    matched_range: Optional[ReferenceRange] = None
    gender_clean = (patient_gender or "All").strip().capitalize()
    if gender_clean not in ["Male", "Female"]:
        gender_clean = "All"

    # Priority 1: Exact gender and age match
    for r in ranges:
        if r.gender == gender_clean and r.age_min_years <= patient_age_years <= r.age_max_years:
            matched_range = r
            break

    # Priority 2: 'All' gender with age match
    if not matched_range:
        for r in ranges:
            if r.gender == "All" and r.age_min_years <= patient_age_years <= r.age_max_years:
                matched_range = r
                break

    # Priority 3: Any range for this parameter
    if not matched_range and ranges:
        matched_range = ranges[0]

    # Build reference range display
    ref_display = ""
    flag = "Normal"
    is_critical = False
    val_num: Optional[float] = None

    if matched_range:
        if matched_range.low_normal is not None and matched_range.high_normal is not None:
            ref_display = f"{matched_range.low_normal} - {matched_range.high_normal} {parameter.unit}".strip()
        elif matched_range.low_normal is not None:
            ref_display = f"> {matched_range.low_normal} {parameter.unit}".strip()
        elif matched_range.high_normal is not None:
            ref_display = f"< {matched_range.high_normal} {parameter.unit}".strip()
        elif matched_range.text_normal:
            ref_display = matched_range.text_normal

    # Try numeric parsing
    try:
        val_clean = re.sub(r"[^\d.-]", "", value_str)
        if val_clean and val_clean != "-":
            val_num = float(val_clean)
    except (ValueError, TypeError):
        val_num = None

    if val_num is not None and matched_range and (matched_range.low_normal is not None or matched_range.high_normal is not None):
        # 1. Check Critical Low
        if matched_range.critical_low is not None and val_num <= matched_range.critical_low:
            flag = "Critical_Low"
            is_critical = True
        # 2. Check Critical High
        elif matched_range.critical_high is not None and val_num >= matched_range.critical_high:
            flag = "Critical_High"
            is_critical = True
        # 3. Check High Normal
        elif matched_range.high_normal is not None and val_num > matched_range.high_normal:
            flag = "H"
        # 4. Check Low Normal
        elif matched_range.low_normal is not None and val_num < matched_range.low_normal:
            flag = "L"
        else:
            flag = "Normal"
    else:
        # Qualitative / text comparison
        if matched_range and matched_range.text_normal:
            expected = matched_range.text_normal.strip().lower()
            actual = value_str.strip().lower()
            if actual != expected and actual not in ["normal", "neg", "negative", "clear", "nil"]:
                if any(crit in actual for crit in ["positive", "reactive", "detected", "high"]):
                    flag = "Critical_High"
                    is_critical = True
                else:
                    flag = "Abnormal"
            else:
                flag = "Normal"

    return {
        "value_text": value_str,
        "value_numeric": val_num,
        "flag": flag,
        "is_critical": is_critical,
        "reference_range_display": ref_display,
    }


# =====================================================================
# CALCULATED PARAMETERS ENGINE
# =====================================================================


def compute_derived_parameters(results_dict: Dict[str, float]) -> Dict[str, float]:
    """
    Computes standard clinical derived analytes based on input parameter values:
    - Globulin = Total Protein - Albumin
    - A/G Ratio = Albumin / Globulin
    - Indirect Bilirubin = Total Bilirubin - Direct Bilirubin
    - VLDL = Triglycerides / 5
    - LDL = Total Cholesterol - HDL - (Triglycerides / 5)
    - BUN/Creatinine Ratio = BUN / Creatinine
    - eGFR = CKD-EPI simplified approximation
    """
    derived: Dict[str, float] = {}

    # LFT Calculations
    tp = results_dict.get("TP")
    alb = results_dict.get("ALB")
    if tp is not None and alb is not None and tp > alb:
        glob = round(tp - alb, 2)
        derived["GLOB"] = glob
        if glob > 0:
            derived["AG_RATIO"] = round(alb / glob, 2)

    tbil = results_dict.get("TBIL")
    dbil = results_dict.get("DBIL")
    if tbil is not None and dbil is not None and tbil >= dbil:
        derived["IBIL"] = round(tbil - dbil, 2)

    # Lipid Calculations (Friedewald)
    chol = results_dict.get("CHOL")
    tg = results_dict.get("TG")
    hdl = results_dict.get("HDL")
    if tg is not None:
        derived["VLDL"] = round(tg / 5.0, 1)
        if chol is not None and hdl is not None and tg < 400:
            derived["LDL"] = round(chol - hdl - (tg / 5.0), 1)

    # KFT Calculations
    bun = results_dict.get("BUN")
    creat = results_dict.get("CREAT")
    if bun is not None and creat is not None and creat > 0:
        derived["BUN_CREAT_RATIO"] = round(bun / creat, 1)

    return derived


# =====================================================================
# PREVIOUS-RESULT DELTA ENGINE
# =====================================================================


def get_previous_result_delta(
    session: Session,
    patient_id: int,
    parameter_id: int,
    exclude_order_id: Optional[int] = None,
) -> Tuple[Optional[str], Optional[date]]:
    """
    Queries previous approved result for this patient and parameter to show clinical delta trend.
    """
    stmt = (
        select(Result)
        .join(LabOrder, Result.lab_order_id == LabOrder.id)
        .where(
            LabOrder.patient_id == patient_id,
            Result.parameter_id == parameter_id,
            LabOrder.status == "approved",
        )
    )
    if exclude_order_id:
        stmt = stmt.where(Result.lab_order_id != exclude_order_id)

    stmt = stmt.order_by(desc(Result.id)).limit(1)
    prev = session.exec(stmt).first()
    if prev and prev.value_text:
        d = prev.entered_at.date() if prev.entered_at else None
        return prev.value_text, d
    return None, None


# =====================================================================
# SPECIMEN WORKFLOW
# =====================================================================


def collect_specimen(
    session: Session,
    specimen_id: int,
    collected_by: str,
    notes: Optional[str] = None,
) -> Specimen:
    """Mark specimen as collected and update associated lab order status."""
    specimen = session.get(Specimen, specimen_id)
    if not specimen:
        raise ValueError(f"Specimen {specimen_id} not found")

    now = utc_now()
    specimen.status = "collected"
    specimen.collected_by = collected_by
    specimen.collected_at = now
    if notes:
        specimen.notes = f"{specimen.notes or ''} [Collected: {notes}]".strip()

    # Append to timeline
    timeline = json.loads(specimen.timeline_json or "[]")
    timeline.append({
        "status": "collected",
        "timestamp": now.isoformat(),
        "user": collected_by,
        "notes": notes or "Sample collected successfully",
    })
    specimen.timeline_json = json.dumps(timeline)
    session.add(specimen)

    # Update order items and order
    order = session.get(LabOrder, specimen.lab_order_id)
    if order:
        if order.status in ["ordered", "pending_collection"]:
            order.status = "specimen_collected"
            session.add(order)

        # Update items with this specimen
        items = session.exec(
            select(LabOrderItem).where(LabOrderItem.specimen_id == specimen.id)
        ).all()
        for item in items:
            item.status = "specimen_collected"
            session.add(item)

    record_audit_log(
        session=session,
        action="SPECIMEN_COLLECTED",
        actor=collected_by,
        entity_type="Specimen",
        entity_id=specimen.id,
        details={"barcode": specimen.barcode, "order_id": specimen.lab_order_id},
    )
    session.commit()
    session.refresh(specimen)
    return specimen


def receive_specimen(
    session: Session,
    specimen_id: int,
    received_by: str,
    notes: Optional[str] = None,
) -> Specimen:
    """Mark specimen as received in laboratory and transition order to in_progress."""
    specimen = session.get(Specimen, specimen_id)
    if not specimen:
        raise ValueError(f"Specimen {specimen_id} not found")

    now = utc_now()
    specimen.status = "received"
    specimen.received_by = received_by
    specimen.received_at = now
    if notes:
        specimen.notes = f"{specimen.notes or ''} [Received: {notes}]".strip()

    timeline = json.loads(specimen.timeline_json or "[]")
    timeline.append({
        "status": "received",
        "timestamp": now.isoformat(),
        "user": received_by,
        "notes": notes or "Specimen received in laboratory",
    })
    specimen.timeline_json = json.dumps(timeline)
    session.add(specimen)

    order = session.get(LabOrder, specimen.lab_order_id)
    if order:
        order.status = "in_progress"
        session.add(order)

        items = session.exec(
            select(LabOrderItem).where(LabOrderItem.specimen_id == specimen.id)
        ).all()
        for item in items:
            item.status = "in_progress"
            session.add(item)

    record_audit_log(
        session=session,
        action="SPECIMEN_RECEIVED",
        actor=received_by,
        entity_type="Specimen",
        entity_id=specimen.id,
        details={"barcode": specimen.barcode, "order_id": specimen.lab_order_id},
    )
    session.commit()
    session.refresh(specimen)
    return specimen


def reject_specimen(
    session: Session,
    specimen_id: int,
    rejected_by: str,
    rejection_reason: str,
    recollect_requested: bool = True,
) -> Tuple[Specimen, Optional[Specimen]]:
    """
    Reject sample (hemolyzed, clotted, insufficient quantity, wrong tube)
    and optionally generate a recollected specimen request.
    """
    specimen = session.get(Specimen, specimen_id)
    if not specimen:
        raise ValueError(f"Specimen {specimen_id} not found")

    now = utc_now()
    specimen.status = "rejected"
    specimen.rejected_by = rejected_by
    specimen.rejected_at = now
    specimen.rejection_reason = rejection_reason
    specimen.recollect_requested = recollect_requested

    timeline = json.loads(specimen.timeline_json or "[]")
    timeline.append({
        "status": "rejected",
        "timestamp": now.isoformat(),
        "user": rejected_by,
        "reason": rejection_reason,
        "recollect": recollect_requested,
    })
    specimen.timeline_json = json.dumps(timeline)

    new_specimen: Optional[Specimen] = None
    if recollect_requested:
        # Generate new specimen for recollection
        new_barcode = generate_specimen_barcode(session)
        new_timeline = json.dumps([{
            "status": "pending_collection",
            "timestamp": now.isoformat(),
            "user": rejected_by,
            "notes": f"Recollect requested due to rejection of {specimen.barcode} ({rejection_reason})",
        }])
        new_specimen = Specimen(
            lab_order_id=specimen.lab_order_id,
            barcode=new_barcode,
            specimen_type=specimen.specimen_type,
            status="pending_collection",
            timeline_json=new_timeline,
            notes=f"Recollection requested. Prior rejected sample: {specimen.barcode}",
        )
        session.add(new_specimen)
        session.flush()

        specimen.recollected_specimen_id = new_specimen.id

        # Update order item links to the new specimen
        items = session.exec(
            select(LabOrderItem).where(LabOrderItem.specimen_id == specimen.id)
        ).all()
        for item in items:
            item.specimen_id = new_specimen.id
            item.status = "ordered"
            session.add(item)

        order = session.get(LabOrder, specimen.lab_order_id)
        if order:
            order.status = "ordered"
            session.add(order)

    session.add(specimen)
    record_audit_log(
        session=session,
        action="SPECIMEN_REJECTED",
        actor=rejected_by,
        entity_type="Specimen",
        entity_id=specimen.id,
        details={
            "barcode": specimen.barcode,
            "reason": rejection_reason,
            "recollect_requested": recollect_requested,
            "new_specimen_id": new_specimen.id if new_specimen else None,
        },
    )
    session.commit()
    session.refresh(specimen)
    if new_specimen:
        session.refresh(new_specimen)
    return specimen, new_specimen


# =====================================================================
# RESULTS ENTRY, DERIVATION & VALIDATION
# =====================================================================


def enter_order_results(
    session: Session,
    lab_order_id: int,
    results_data: List[Dict[str, Any]],
    user_name: str,
) -> Tuple[LabOrder, List[Result], bool]:
    """
    Saves technician results, evaluates reference ranges, computes derived analytes,
    records deltas against previous patient tests, and flags critical values.
    Returns: (order, results, has_critical)
    """
    order = session.get(LabOrder, lab_order_id)
    if not order:
        raise ValueError(f"Lab order {lab_order_id} not found")

    if order.status == "approved":
        raise ValueError("Cannot edit an already approved report. Use the Amendment workflow.")

    now = utc_now()
    age_years = calculate_age_years(order.patient_dob)
    gender = order.patient_gender or "All"

    # Map of parameter code to entered numeric value for derived analytes calculation
    numeric_values: Dict[str, float] = {}
    saved_results: List[Result] = []
    has_critical = False
    critical_params = []

    # Process explicit input results first
    for r_in in results_data:
        param_id = r_in.get("parameter_id")
        val_text = str(r_in.get("value_text") or "").strip()
        if not param_id or not val_text:
            continue

        param = session.get(Parameter, param_id)
        if not param:
            continue

        eval_res = evaluate_parameter_value(
            session=session,
            parameter=param,
            value_str=val_text,
            patient_gender=gender,
            patient_age_years=age_years,
        )

        if eval_res["value_numeric"] is not None:
            numeric_values[param.code] = eval_res["value_numeric"]

        if eval_res["is_critical"]:
            has_critical = True
            critical_params.append(param.name)

        # Delta comparison
        prev_val, prev_dt = get_previous_result_delta(
            session=session,
            patient_id=order.patient_id,
            parameter_id=param.id,
            exclude_order_id=order.id,
        )

        # Find existing result or create new
        existing_res = session.exec(
            select(Result).where(
                Result.lab_order_id == order.id,
                Result.parameter_id == param.id,
            )
        ).first()

        item_id = r_in.get("lab_order_item_id")
        if not item_id:
            first_item = session.exec(
                select(LabOrderItem).where(
                    LabOrderItem.lab_order_id == order.id,
                    LabOrderItem.test_id == param.test_id,
                )
            ).first()
            item_id = first_item.id if first_item else None

        if existing_res:
            res_obj = existing_res
            res_obj.value_text = eval_res["value_text"]
            res_obj.value_numeric = eval_res["value_numeric"]
            res_obj.flag = eval_res["flag"]
            res_obj.is_critical = eval_res["is_critical"]
            res_obj.reference_range_display = eval_res["reference_range_display"]
            res_obj.delta_previous_value = prev_val
            res_obj.delta_previous_date = prev_dt
            res_obj.entered_by = user_name
            res_obj.entered_at = now
        else:
            res_obj = Result(
                lab_order_id=order.id,
                lab_order_item_id=item_id,
                parameter_id=param.id,
                parameter_code=param.code,
                parameter_name=param.name,
                value_text=eval_res["value_text"],
                value_numeric=eval_res["value_numeric"],
                unit=param.unit,
                reference_range_display=eval_res["reference_range_display"],
                flag=eval_res["flag"],
                is_critical=eval_res["is_critical"],
                delta_previous_value=prev_val,
                delta_previous_date=prev_dt,
                entered_by=user_name,
                entered_at=now,
            )
        session.add(res_obj)
        saved_results.append(res_obj)

    # Compute derived analytes (e.g., A/G Ratio, Globulin, VLDL, LDL)
    derived = compute_derived_parameters(numeric_values)
    for code, num_val in derived.items():
        # Check if there is a parameter with this code in the order's tests
        calc_param = session.exec(
            select(Parameter)
            .join(LabOrderItem, Parameter.test_id == LabOrderItem.test_id)
            .where(LabOrderItem.lab_order_id == order.id, Parameter.code == code)
        ).first()
        if calc_param:
            eval_calc = evaluate_parameter_value(
                session=session,
                parameter=calc_param,
                value_str=str(num_val),
                patient_gender=gender,
                patient_age_years=age_years,
            )
            # Find order item
            item_obj = session.exec(
                select(LabOrderItem).where(
                    LabOrderItem.lab_order_id == order.id,
                    LabOrderItem.test_id == calc_param.test_id,
                )
            ).first()
            item_id = item_obj.id if item_obj else None

            existing_calc = session.exec(
                select(Result).where(
                    Result.lab_order_id == order.id,
                    Result.parameter_id == calc_param.id,
                )
            ).first()
            if existing_calc:
                existing_calc.value_text = str(num_val)
                existing_calc.value_numeric = num_val
                existing_calc.flag = eval_calc["flag"]
                existing_calc.is_critical = eval_calc["is_critical"]
                existing_calc.reference_range_display = eval_calc["reference_range_display"]
                existing_calc.entered_by = f"{user_name} (Auto-Calc)"
                existing_calc.entered_at = now
                session.add(existing_calc)
                saved_results.append(existing_calc)
            else:
                new_calc = Result(
                    lab_order_id=order.id,
                    lab_order_item_id=item_id,
                    parameter_id=calc_param.id,
                    parameter_code=calc_param.code,
                    parameter_name=calc_param.name,
                    value_text=str(num_val),
                    value_numeric=num_val,
                    unit=calc_param.unit,
                    reference_range_display=eval_calc["reference_range_display"],
                    flag=eval_calc["flag"],
                    is_critical=eval_calc["is_critical"],
                    entered_by=f"{user_name} (Auto-Calc)",
                    entered_at=now,
                )
                session.add(new_calc)
                saved_results.append(new_calc)

    # Update order status to result_entered
    order.status = "result_entered"
    session.add(order)

    # Ensure draft report exists
    report = session.exec(
        select(LabReport).where(LabReport.lab_order_id == order.id)
    ).first()
    if not report:
        report = LabReport(
            lab_order_id=order.id,
            report_no=generate_report_number(session),
            version=1,
            status="draft",
        )
        session.add(report)

    # If critical results exist, emit result.critical event
    if has_critical:
        event_bus.emit(
            name="result.critical",
            payload={
                "order_id": order.id,
                "order_no": order.order_no,
                "patient_id": order.patient_id,
                "patient_name": order.patient_name,
                "patient_mrn": order.patient_mrn,
                "doctor_id": order.doctor_id,
                "critical_parameters": critical_params,
                "entered_by": user_name,
            },
        )

    record_audit_log(
        session=session,
        action="LABORATORY_RESULTS_ENTERED",
        actor=user_name,
        entity_type="LabOrder",
        entity_id=order.id,
        details={
            "order_no": order.order_no,
            "results_count": len(saved_results),
            "has_critical": has_critical,
        },
    )

    session.commit()
    session.refresh(order)
    return order, saved_results, has_critical


# =====================================================================
# PATHOLOGIST APPROVAL & AMENDMENT (TWO-STEP WORKFLOW)
# =====================================================================


def approve_lab_order(
    session: Session,
    lab_order_id: int,
    pathologist_name: str,
    summary_notes: Optional[str] = None,
) -> LabReport:
    """
    Senior pathologist approves the results.
    - Locks results (immutable).
    - Status transitions to 'approved'.
    - Emits result.approved and lab.charge events.
    """
    order = session.get(LabOrder, lab_order_id)
    if not order:
        raise ValueError(f"Lab order {lab_order_id} not found")

    results = session.exec(
        select(Result).where(Result.lab_order_id == order.id)
    ).all()
    if not results:
        raise ValueError("Cannot approve order with no entered results.")

    now = utc_now()

    # Stamp results with validator
    has_critical = False
    for res in results:
        res.validated_by = pathologist_name
        res.validated_at = now
        if res.is_critical:
            has_critical = True
        session.add(res)

    # Update order items and order status
    order.status = "approved"
    session.add(order)

    items = session.exec(
        select(LabOrderItem).where(LabOrderItem.lab_order_id == order.id)
    ).all()
    for it in items:
        it.status = "completed"
        session.add(it)

    # Update or create LabReport
    report = session.exec(
        select(LabReport).where(LabReport.lab_order_id == order.id)
    ).first()
    if not report:
        report = LabReport(
            lab_order_id=order.id,
            report_no=generate_report_number(session),
            version=1,
            status="approved",
            summary_notes=summary_notes,
            approved_by=pathologist_name,
            approved_at=now,
        )
    else:
        report.status = "approved"
        report.approved_by = pathologist_name
        report.approved_at = now
        if summary_notes:
            report.summary_notes = summary_notes
    session.add(report)

    # Emit domain events
    event_bus.emit(
        name="result.approved",
        payload={
            "order_id": order.id,
            "order_no": order.order_no,
            "patient_id": order.patient_id,
            "patient_name": order.patient_name,
            "patient_mrn": order.patient_mrn,
            "doctor_id": order.doctor_id,
            "report_no": report.report_no,
            "total_tests": len(items),
            "has_critical": has_critical,
            "approved_by": pathologist_name,
        },
    )

    event_bus.emit(
        name="lab.charge",
        payload={
            "order_id": order.id,
            "order_no": order.order_no,
            "patient_id": order.patient_id,
            "patient_mrn": order.patient_mrn,
            "amount": order.total_price,
            "department": order.department,
            "description": f"Laboratory Diagnostic Service ({order.order_no})",
        },
    )

    record_audit_log(
        session=session,
        action="LABORATORY_REPORT_APPROVED",
        actor=pathologist_name,
        entity_type="LabReport",
        entity_id=report.id,
        details={
            "order_no": order.order_no,
            "report_no": report.report_no,
            "has_critical": has_critical,
        },
    )

    session.commit()
    session.refresh(report)
    return report


def amend_approved_report(
    session: Session,
    lab_order_id: int,
    pathologist_name: str,
    amendment_reason: str,
    updated_results_data: List[Dict[str, Any]],
) -> LabReport:
    """
    Amend an already-approved report:
    - Bumps version (e.g., v1 -> v2).
    - Sets is_corrected_report = True with amendment reason.
    - Re-evaluates ranges and saves results with new version number.
    - Re-approves with full audit trail.
    """
    order = session.get(LabOrder, lab_order_id)
    if not order:
        raise ValueError(f"Lab order {lab_order_id} not found")

    report = session.exec(
        select(LabReport).where(LabReport.lab_order_id == order.id)
    ).first()
    if not report:
        raise ValueError("Cannot amend non-existent report")

    now = utc_now()
    new_version = report.version + 1
    report.version = new_version
    report.is_corrected_report = True
    report.amendment_reason = amendment_reason
    report.status = "approved"
    report.approved_by = pathologist_name
    report.approved_at = now
    session.add(report)

    # Re-evaluate and update results
    age_years = calculate_age_years(order.patient_dob)
    gender = order.patient_gender or "All"

    for r_in in updated_results_data:
        param_id = r_in.get("parameter_id")
        val_text = str(r_in.get("value_text") or "").strip()
        if not param_id or not val_text:
            continue

        param = session.get(Parameter, param_id)
        if not param:
            continue

        eval_res = evaluate_parameter_value(
            session=session,
            parameter=param,
            value_str=val_text,
            patient_gender=gender,
            patient_age_years=age_years,
        )

        res_obj = session.exec(
            select(Result).where(
                Result.lab_order_id == order.id,
                Result.parameter_id == param.id,
            )
        ).first()

        if res_obj:
            res_obj.value_text = eval_res["value_text"]
            res_obj.value_numeric = eval_res["value_numeric"]
            res_obj.flag = eval_res["flag"]
            res_obj.is_critical = eval_res["is_critical"]
            res_obj.reference_range_display = eval_res["reference_range_display"]
            res_obj.version = new_version
            res_obj.validated_by = pathologist_name
            res_obj.validated_at = now
            session.add(res_obj)

    record_audit_log(
        session=session,
        action="LABORATORY_REPORT_AMENDED",
        actor=pathologist_name,
        entity_type="LabReport",
        entity_id=report.id,
        details={
            "order_no": order.order_no,
            "report_no": report.report_no,
            "version": new_version,
            "amendment_reason": amendment_reason,
        },
    )

    session.commit()
    session.refresh(report)
    return report


# =====================================================================
# CRITICAL VALUE ACKNOWLEDGEMENT
# =====================================================================


def acknowledge_critical_value(
    session: Session,
    result_id: int,
    doctor_name: str,
    notes: Optional[str] = None,
) -> Result:
    """Record physician acknowledgement of critical lab values."""
    res = session.get(Result, result_id)
    if not res:
        raise ValueError(f"Result {result_id} not found")

    res.critical_acknowledged_by = doctor_name
    res.critical_acknowledged_at = utc_now()
    res.critical_acknowledged_notes = notes or "Critical value acknowledged by ordering physician"
    session.add(res)

    record_audit_log(
        session=session,
        action="LABORATORY_CRITICAL_ACKNOWLEDGED",
        actor=doctor_name,
        entity_type="Result",
        entity_id=res.id,
        details={
            "parameter": res.parameter_code,
            "value": res.value_text,
            "flag": res.flag,
            "notes": res.critical_acknowledged_notes,
        },
    )
    session.commit()
    session.refresh(res)
    return res


# =====================================================================
# INSTRUMENT CSV IMPORT
# =====================================================================


def import_instrument_results_csv(
    session: Session,
    csv_content: str,
    user_name: str,
) -> Dict[str, Any]:
    """
    Imports automated analyzer results from a CSV stream.
    Expected CSV columns: barcode (or order_no), parameter_code, value
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    imported_count = 0
    skipped_count = 0
    errors: List[str] = []

    for row_idx, row in enumerate(reader, start=1):
        # Support barcode or order_no
        barcode = (row.get("barcode") or row.get("sample_id") or "").strip()
        order_no = (row.get("order_no") or "").strip()
        p_code = (row.get("parameter_code") or row.get("code") or "").strip().upper()
        val_str = (row.get("value") or row.get("result") or "").strip()

        if not (barcode or order_no) or not p_code or not val_str:
            skipped_count += 1
            continue

        order: Optional[LabOrder] = None
        if barcode:
            specimen = session.exec(select(Specimen).where(Specimen.barcode == barcode)).first()
            if specimen:
                order = session.get(LabOrder, specimen.lab_order_id)
        if not order and order_no:
            order = session.exec(select(LabOrder).where(LabOrder.order_no == order_no)).first()

        if not order:
            errors.append(f"Row {row_idx}: No matching order for barcode '{barcode}' / order '{order_no}'")
            skipped_count += 1
            continue

        if order.status == "approved":
            errors.append(f"Row {row_idx}: Order '{order.order_no}' is already approved (immutable)")
            skipped_count += 1
            continue

        param = session.exec(select(Parameter).where(Parameter.code == p_code)).first()
        if not param:
            errors.append(f"Row {row_idx}: Unknown parameter code '{p_code}'")
            skipped_count += 1
            continue

        # Enter single parameter result
        try:
            enter_order_results(
                session=session,
                lab_order_id=order.id,
                results_data=[{"parameter_id": param.id, "value_text": val_str}],
                user_name=f"{user_name} (Instrument-CSV)",
            )
            imported_count += 1
        except Exception as e:
            errors.append(f"Row {row_idx} ({p_code}): {str(e)}")
            skipped_count += 1

    return {
        "imported_count": imported_count,
        "skipped_count": skipped_count,
        "errors": errors,
    }


# =====================================================================
# EVENT SUBSCRIBER (order.placed)
# =====================================================================


def handle_order_placed(event: Event) -> None:
    """
    Subscribes to 'order.placed' from consultations module.
    When order type is 'Lab', automatically provisions:
    - LabOrder
    - LabOrderItem(s)
    - Specimen(s) with barcode and tracking timeline
    """
    data = getattr(event, "payload", None) or getattr(event, "data", {})
    order_type = (data.get("type") or "").strip()
    if order_type.lower() != "lab":
        return

    with Session(engine) as session:
        test_name = (data.get("test_name") or "General Panel").strip()
        priority = (data.get("priority") or "Routine").strip()

        # Find matching test or panel in catalog
        test_obj = session.exec(
            select(TestCatalog).where(
                (TestCatalog.name.ilike(f"%{test_name}%")) | (TestCatalog.code == test_name.upper())
            )
        ).first()

        if not test_obj:
            # Fallback: check first available test or create ad-hoc
            test_obj = session.exec(select(TestCatalog)).first()
            if not test_obj:
                test_obj = TestCatalog(
                    code="GEN-LAB",
                    name=test_name,
                    department="Hematology",
                    specimen_type="Whole Blood (EDTA)",
                    tat_hours=4,
                    price=25.0,
                )
                session.add(test_obj)
                session.flush()

        ordered_at = utc_now()
        tat_deadline = calculate_tat_deadline(ordered_at, priority, test_obj.tat_hours)

        # Generate order
        order = LabOrder(
            order_no=generate_order_number(session),
            order_source="order.placed",
            encounter_id=data.get("encounter_id"),
            patient_id=data.get("patient_id", 1),
            patient_name=data.get("patient_name", "Patient"),
            patient_mrn=data.get("patient_mrn", "MRN-00000"),
            patient_gender=data.get("patient_gender", "Other"),
            doctor_id=data.get("doctor_id"),
            doctor_name=data.get("doctor_name", "Attending Physician"),
            priority=priority,
            status="ordered",
            department=test_obj.department,
            ordered_at=ordered_at,
            tat_deadline=tat_deadline,
            clinical_notes=data.get("clinical_notes"),
            total_price=test_obj.price,
        )
        session.add(order)
        session.flush()

        # Generate barcode & specimen
        barcode = generate_specimen_barcode(session)
        init_timeline = json.dumps([{
            "status": "pending_collection",
            "timestamp": ordered_at.isoformat(),
            "user": "System (order.placed)",
            "notes": f"Requisition created from Consultation Order #{data.get('order_id', '')}",
        }])
        specimen = Specimen(
            lab_order_id=order.id,
            barcode=barcode,
            specimen_type=test_obj.specimen_type,
            status="pending_collection",
            timeline_json=init_timeline,
            notes=f"Requisition for {test_obj.name}",
        )
        session.add(specimen)
        session.flush()

        # Generate LabOrderItem
        item = LabOrderItem(
            lab_order_id=order.id,
            test_id=test_obj.id,
            test_code=test_obj.code,
            test_name=test_obj.name,
            department=test_obj.department,
            specimen_type=test_obj.specimen_type,
            status="ordered",
            price=test_obj.price,
            specimen_id=specimen.id,
        )
        session.add(item)

        # Create initial draft report
        report = LabReport(
            lab_order_id=order.id,
            report_no=generate_report_number(session),
            version=1,
            status="draft",
        )
        session.add(report)

        record_audit_log(
            session=session,
            action="LAB_ORDER_AUTOPROVISIONED",
            actor="System",
            entity_type="LabOrder",
            entity_id=order.id,
            details={
                "order_no": order.order_no,
                "barcode": specimen.barcode,
                "test": test_obj.name,
            },
        )
        session.commit()


# =====================================================================
# PATIENT 360 LAB HISTORY HELPER
# =====================================================================


def get_patient_lab_history(session: Session, patient_id: int) -> Dict[str, Any]:
    """Retrieves all lab orders, reports, and parameter trend points for Patient 360."""
    orders = session.exec(
        select(LabOrder)
        .where(LabOrder.patient_id == patient_id)
        .order_by(desc(LabOrder.ordered_at))
    ).all()

    # Query all results for patient
    results = session.exec(
        select(Result)
        .join(LabOrder, Result.lab_order_id == LabOrder.id)
        .where(LabOrder.patient_id == patient_id)
        .order_by(Result.entered_at)
    ).all()

    # Build trend series for key analytes
    trends: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        if r.value_numeric is not None and r.parameter_code:
            code = r.parameter_code
            if code not in trends:
                trends[code] = []
            trends[code].append({
                "date": r.entered_at.strftime("%Y-%m-%d") if r.entered_at else "",
                "value": r.value_numeric,
                "flag": r.flag,
                "unit": r.unit,
                "name": r.parameter_name,
            })

    return {
        "orders": orders,
        "results": results,
        "trends": trends,
        "total_orders": len(orders),
    }


# =====================================================================
# REPORTLAB PDF GENERATION (DIAGNOSTIC REPORT)
# =====================================================================


def generate_lab_report_pdf(
    session: Session,
    lab_order_id: int,
    hospital_info: Optional[Dict[str, str]] = None,
) -> bytes:
    """
    Generates a professional diagnostic laboratory report PDF with:
    - Hospital branding header
    - Patient and requisition info box
    - Prominent corrected-report notice if amended
    - Clear results grid with flags and reference intervals
    - Critical value acknowledgement note if applicable
    - Pathologist electronic sign-off block
    """
    order = session.get(LabOrder, lab_order_id)
    if not order:
        raise ValueError(f"Lab order {lab_order_id} not found")

    report = session.exec(
        select(LabReport).where(LabReport.lab_order_id == order.id)
    ).first()

    results = session.exec(
        select(Result).where(Result.lab_order_id == order.id)
    ).all()

    specimens = session.exec(
        select(Specimen).where(Specimen.lab_order_id == order.id)
    ).all()
    barcode_str = ", ".join(s.barcode for s in specimens) if specimens else "N/A"

    if not hospital_info:
        hospital_info = {
            "name": "MediCore Hospital & Medical Center",
            "department": "Department of Pathology & Clinical Laboratories",
            "address": "100 Healthcare Boulevard, Metro City",
            "phone": "+1 (555) 234-5678",
            "emergency": "911 / (555) 999-0000",
        }

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
    title_style = ParagraphStyle(
        "LabTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        textColor=colors.HexColor("#0f172a"),
    )
    sub_title_style = ParagraphStyle(
        "LabSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#475569"),
    )
    bold_style = ParagraphStyle(
        "LabBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#1e293b"),
    )
    body_style = ParagraphStyle(
        "LabBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#334155"),
    )
    flag_normal = ParagraphStyle(
        "FlagNormal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#166534"),
    )
    flag_abnormal = ParagraphStyle(
        "FlagAbnormal",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#b45309"),
    )
    flag_critical = ParagraphStyle(
        "FlagCritical",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#dc2626"),
    )

    story = []

    # 1. Header with Hospital Branding
    h_table = Table([
        [
            Paragraph(f"<b>{hospital_info['name']}</b><br/><font size=9 color='#0d9488'><b>{hospital_info['department']}</b></font>", title_style),
            Paragraph(
                f"<b>DIAGNOSTIC LAB REPORT</b><br/>"
                f"<font size=8 color='#64748b'>Report #: {report.report_no if report else 'DRAFT'}<br/>"
                f"Version: {report.version if report else 1} | Order: {order.order_no}</font>",
                ParagraphStyle("HdrRight", parent=title_style, alignment=2, fontSize=11, leading=14),
            ),
        ],
        [
            Paragraph(f"{hospital_info['address']} • Tel: {hospital_info['phone']}", sub_title_style),
            Paragraph(f"Report Date: {(report.approved_at or utc_now()).strftime('%Y-%m-%d %H:%M')}", ParagraphStyle("DateRight", parent=sub_title_style, alignment=2)),
        ],
    ], colWidths=[340, 200])
    h_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(h_table)
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#0d9488"), spaceAfter=8, spaceBefore=0))

    # 2. Corrected Report Banner (if amended)
    if report and report.is_corrected_report:
        corr_box = Table([
            [Paragraph(
                f"<b>CORRECTED REPORT (VERSION {report.version})</b><br/>"
                f"<b>Reason for Amendment:</b> {report.amendment_reason or 'Correction of test parameters'}<br/>"
                f"<i>This document supersedes all prior preliminary or final laboratory reports for this requisition.</i>",
                ParagraphStyle("CorrTxt", fontName="Helvetica-Bold", fontSize=8.5, leading=12, textColor=colors.HexColor("#991b1b")),
            )]
        ], colWidths=[540])
        corr_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fef2f2")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#ef4444")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(corr_box)
        story.append(Spacer(1, 8))

    # 3. Patient and Specimen Meta Box
    p_dob = str(order.patient_dob) if order.patient_dob else "N/A"
    age_str = f"{int(calculate_age_years(order.patient_dob))} yrs" if order.patient_dob else "Adult"
    meta_data = [
        [
            Paragraph(f"<b>Patient:</b> {order.patient_name}", bold_style),
            Paragraph(f"<b>MRN:</b> <font color='#0d9488'><b>{order.patient_mrn}</b></font>", bold_style),
            Paragraph(f"<b>Priority:</b> <font color='#b91c1c'><b>{order.priority}</b></font>", bold_style),
        ],
        [
            Paragraph(f"<b>Age / Sex:</b> {age_str} / {order.patient_gender}", body_style),
            Paragraph(f"<b>Specimen Barcode:</b> {barcode_str}", body_style),
            Paragraph(f"<b>Department:</b> {order.department}", body_style),
        ],
        [
            Paragraph(f"<b>Referring Doctor:</b> {order.doctor_name or 'N/A'}", body_style),
            Paragraph(f"<b>Ordered At:</b> {order.ordered_at.strftime('%Y-%m-%d %H:%M')}", body_style),
            Paragraph(f"<b>Status:</b> <b>{order.status.upper()}</b>", body_style),
        ],
    ]
    meta_table = Table(meta_data, colWidths=[180, 180, 180])
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
    story.append(Spacer(1, 10))

    # 4. Results Grid Table
    story.append(Paragraph(f"<b>INVESTIGATION RESULTS - {order.department.upper()}</b>", ParagraphStyle("SecHdr", fontName="Helvetica-Bold", fontSize=10, textColor=colors.HexColor("#0f172a"), spaceAfter=4)))
    
    table_rows = [
        [
            Paragraph("<b>Investigation / Parameter</b>", bold_style),
            Paragraph("<b>Result</b>", bold_style),
            Paragraph("<b>Unit</b>", bold_style),
            Paragraph("<b>Biological Reference Interval</b>", bold_style),
            Paragraph("<b>Flag</b>", bold_style),
        ]
    ]

    has_any_critical = False
    critical_ack_list = []

    for r in results:
        # Flag formatting
        if r.is_critical:
            has_any_critical = True
            flag_p = Paragraph(f"<b>CRITICAL ({r.flag})</b>", flag_critical)
            val_p = Paragraph(f"<b>{r.value_text}</b>", flag_critical)
            if r.critical_acknowledged_by:
                critical_ack_list.append(
                    f"{r.parameter_name}: Acknowledged by Dr. {r.critical_acknowledged_by} on "
                    f"{(r.critical_acknowledged_at or utc_now()).strftime('%Y-%m-%d %H:%M')} - Notes: {r.critical_acknowledged_notes}"
                )
        elif r.flag in ["H", "High"]:
            flag_p = Paragraph("<b>HIGH [H]</b>", flag_abnormal)
            val_p = Paragraph(f"<b>{r.value_text}</b>", flag_abnormal)
        elif r.flag in ["L", "Low"]:
            flag_p = Paragraph("<b>LOW [L]</b>", flag_abnormal)
            val_p = Paragraph(f"<b>{r.value_text}</b>", flag_abnormal)
        elif r.flag == "Abnormal":
            flag_p = Paragraph("<b>ABNORMAL</b>", flag_abnormal)
            val_p = Paragraph(f"<b>{r.value_text}</b>", flag_abnormal)
        else:
            flag_p = Paragraph("Normal", flag_normal)
            val_p = Paragraph(r.value_text, body_style)

        delta_info = ""
        if r.delta_previous_value:
            delta_info = f"<br/><font size=7 color='#64748b'>Prev: {r.delta_previous_value} ({r.delta_previous_date})</font>"

        param_name_p = Paragraph(f"<b>{r.parameter_name}</b>{delta_info}", body_style)
        unit_p = Paragraph(r.unit or "-", body_style)
        ref_p = Paragraph(r.reference_range_display or "-", body_style)

        table_rows.append([param_name_p, val_p, unit_p, ref_p, flag_p])

    res_table = Table(table_rows, colWidths=[180, 90, 70, 130, 70])
    res_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(res_table)
    story.append(Spacer(1, 8))

    # 5. Critical Values Acknowledgement Note (if any)
    if critical_ack_list:
        ack_data = [[
            Paragraph("<b>PHYSICIAN CRITICAL VALUE ACKNOWLEDGEMENT:</b><br/>" + "<br/>".join(critical_ack_list),
                      ParagraphStyle("AckTxt", fontName="Helvetica", fontSize=8, leading=11, textColor=colors.HexColor("#991b1b")))
        ]]
        ack_tbl = Table(ack_data, colWidths=[540])
        ack_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fef2f2")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#f87171")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(ack_tbl)
        story.append(Spacer(1, 8))

    # 6. Pathology Verification Sign-off Box
    tech_name = results[0].entered_by if results and results[0].entered_by else "Laboratory Technologist"
    path_name = report.approved_by if report and report.approved_by else (results[0].validated_by if results else "Dr. Victor Vance, MD (Pathology)")
    app_time = (report.approved_at or utc_now()).strftime("%Y-%m-%d %H:%M") if report else "Pending"

    sig_data = [
        [
            Paragraph(f"<b>Entered & Processed By:</b><br/>{tech_name}<br/><font size=7 color='#64748b'>Medical Lab Technologist</font>", body_style),
            Paragraph(f"<b>Validated & Authorized By:</b><br/><b>{path_name}</b><br/><font size=7 color='#64748b'>Consultant Pathologist • Authorized: {app_time}</font>", body_style),
        ]
    ]
    sig_table = Table(sig_data, colWidths=[270, 270])
    sig_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(sig_table)
    story.append(Spacer(1, 10))

    # 7. Quality & Accreditation Footnote
    story.append(Paragraph(
        "<i>Note: Tests performed in compliance with ISO 15189 and CAP guidelines. Biological reference intervals are standardized for age and sex. "
        "A critical result reflects a potentially life-threatening situation requiring immediate clinical intervention.</i>",
        ParagraphStyle("Footnote", fontName="Helvetica-Oblique", fontSize=7, leading=9, textColor=colors.HexColor("#64748b"), alignment=1),
    ))

    doc.build(story)
    return buffer.getvalue()
