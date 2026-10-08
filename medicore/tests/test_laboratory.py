import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
import pytest
from sqlmodel import Session, col, select

from medicore.core.database import engine
from medicore.core.events import Event, event_bus
from medicore.core.models import User
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
    compute_derived_parameters,
    enter_order_results,
    evaluate_parameter_value,
    generate_lab_report_pdf,
    generate_order_number,
    generate_report_number,
    generate_specimen_barcode,
    get_patient_lab_history,
    get_previous_result_delta,
    handle_order_placed,
    import_instrument_results_csv,
    receive_specimen,
    reject_specimen,
)
from medicore.modules.patients.models import Patient


@pytest.fixture
def lab_session():
    """Provides a database session for laboratory test suite."""
    with Session(engine) as session:
        yield session


def get_or_create_test(session: Session, code: str, name: str, dept: str = "Hematology", spec: str = "Whole Blood") -> TestCatalog:
    obj = session.exec(select(TestCatalog).where(TestCatalog.code == code)).first()
    if not obj:
        obj = TestCatalog(code=code, name=name, department=dept, specimen_type=spec, tat_hours=4, price=25.0)
        session.add(obj)
        session.commit()
        session.refresh(obj)
    return obj


def get_or_create_param(session: Session, test_id: int, code: str, name: str, unit: str = "", data_type: str = "numeric") -> Parameter:
    param = session.exec(select(Parameter).where(Parameter.test_id == test_id, Parameter.code == code)).first()
    if not param:
        param = Parameter(test_id=test_id, code=code, name=name, unit=unit, data_type=data_type)
        session.add(param)
        session.commit()
        session.refresh(param)
    return param


# =========================================================================
# 1. TAT & DEADLINE CALCULATIONS
# =========================================================================


def test_tat_deadline_and_breach_detection(lab_session):
    now = datetime.now(timezone.utc)
    
    # STAT priority: 60 minutes
    stat_deadline = calculate_tat_deadline(now, "STAT", tat_hours=4)
    assert stat_deadline == now + timedelta(minutes=60)

    # Urgent priority: tat_hours // 2
    urgent_deadline = calculate_tat_deadline(now, "Urgent", tat_hours=6)
    assert urgent_deadline == now + timedelta(hours=3)

    # Routine priority: tat_hours
    routine_deadline = calculate_tat_deadline(now, "Routine", tat_hours=8)
    assert routine_deadline == now + timedelta(hours=8)

    # Breach detection
    past_deadline = now - timedelta(hours=2)
    order = LabOrder(
        order_no=generate_order_number(lab_session),
        patient_id=1,
        patient_name="Test Patient",
        patient_mrn="MRN-TEST-01",
        priority="STAT",
        status="ordered",
        department="Hematology",
        ordered_at=now - timedelta(hours=3),
        tat_deadline=past_deadline,
        is_tat_breached=False,
    )
    lab_session.add(order)
    lab_session.commit()
    lab_session.refresh(order)

    breached = check_and_update_tat_breach(order)
    assert breached is True
    assert order.is_tat_breached is True


# =========================================================================
# 2. SPECIMEN FLOW: COLLECT, RECEIVE, REJECT WITH RECOLLECT REQUEST
# =========================================================================


def test_specimen_flow_lifecycle(lab_session):
    now = utc_now()
    test_obj = get_or_create_test(lab_session, "CBC-SPEC", "CBC Specimen Test", "Hematology")

    order = LabOrder(
        order_no=generate_order_number(lab_session),
        patient_id=1,
        patient_name="Alice Specimen",
        patient_mrn="MRN-SPEC-01",
        priority="Routine",
        status="ordered",
        department="Hematology",
        ordered_at=now,
        tat_deadline=now + timedelta(hours=4),
    )
    lab_session.add(order)
    lab_session.commit()
    lab_session.refresh(order)

    barcode = generate_specimen_barcode(lab_session)
    specimen = Specimen(
        lab_order_id=order.id,
        barcode=barcode,
        specimen_type="Whole Blood (EDTA)",
        status="pending_collection",
        timeline_json=json.dumps([{"status": "pending_collection", "timestamp": now.isoformat(), "user": "System"}]),
    )
    lab_session.add(specimen)
    lab_session.commit()
    lab_session.refresh(specimen)

    item = LabOrderItem(
        lab_order_id=order.id,
        test_id=test_obj.id,
        test_code=test_obj.code,
        test_name=test_obj.name,
        department="Hematology",
        specimen_type="Whole Blood (EDTA)",
        status="ordered",
        specimen_id=specimen.id,
    )
    lab_session.add(item)
    lab_session.commit()

    # 1. Collect
    collected = collect_specimen(lab_session, specimen.id, "nurse.john", "Venipuncture left arm")
    assert collected.status == "collected"
    assert collected.collected_by == "nurse.john"
    assert collected.collected_at is not None

    lab_session.refresh(order)
    assert order.status == "specimen_collected"

    # 2. Receive in Lab
    received = receive_specimen(lab_session, specimen.id, "tech.alex")
    assert received.status == "received"
    assert received.received_by == "tech.alex"

    lab_session.refresh(order)
    assert order.status == "in_progress"

    # 3. Reject with Recollect Request
    rejected, new_specimen = reject_specimen(
        lab_session,
        specimen.id,
        "tech.alex",
        rejection_reason="Grossly hemolyzed sample",
        recollect_requested=True,
    )
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "Grossly hemolyzed sample"
    assert rejected.recollect_requested is True
    assert new_specimen is not None
    assert new_specimen.status == "pending_collection"
    assert new_specimen.barcode != rejected.barcode

    lab_session.refresh(order)
    assert order.status == "ordered"  # Returned to ordered awaiting recollection


# =========================================================================
# 3. REFERENCE RANGE EVALUATION & AUTO-FLAGGING
# =========================================================================


def test_reference_range_evaluation(lab_session):
    test_obj = get_or_create_test(lab_session, "TEST-CHEM-EVAL", "Chemistry Test Eval", "Biochemistry", "Serum")
    param = get_or_create_param(lab_session, test_obj.id, "GLU_EVAL", "Glucose Eval", "mg/dL", "numeric")

    # Reference range: 70 - 99 normal, < 40 Critical Low, > 400 Critical High
    rng = lab_session.exec(select(ReferenceRange).where(ReferenceRange.parameter_id == param.id)).first()
    if not rng:
        rng = ReferenceRange(
            parameter_id=param.id,
            gender="All",
            age_min_years=0.0,
            age_max_years=120.0,
            low_normal=70.0,
            high_normal=99.0,
            critical_low=40.0,
            critical_high=400.0,
        )
        lab_session.add(rng)
        lab_session.commit()

    # Normal value
    eval_norm = evaluate_parameter_value(lab_session, param, "85", "Male", 40.0)
    assert eval_norm["flag"] == "Normal"
    assert eval_norm["is_critical"] is False
    assert eval_norm["value_numeric"] == 85.0

    # High value
    eval_high = evaluate_parameter_value(lab_session, param, "145", "Male", 40.0)
    assert eval_high["flag"] == "H"
    assert eval_high["is_critical"] is False

    # Low value
    eval_low = evaluate_parameter_value(lab_session, param, "58", "Female", 30.0)
    assert eval_low["flag"] == "L"
    assert eval_low["is_critical"] is False

    # Critical High
    eval_crit_h = evaluate_parameter_value(lab_session, param, "450", "Male", 50.0)
    assert eval_crit_h["flag"] == "Critical_High"
    assert eval_crit_h["is_critical"] is True

    # Critical Low
    eval_crit_l = evaluate_parameter_value(lab_session, param, "32", "Female", 25.0)
    assert eval_crit_l["flag"] == "Critical_Low"
    assert eval_crit_l["is_critical"] is True


# =========================================================================
# 4. CALCULATED PARAMETERS ENGINE (DERIVED ANALYTES)
# =========================================================================


def test_calculated_parameters_engine():
    input_values = {
        "TP": 7.5,
        "ALB": 4.5,
        "TBIL": 2.0,
        "DBIL": 0.5,
        "TG": 150.0,
        "CHOL": 200.0,
        "HDL": 50.0,
        "BUN": 20.0,
        "CREAT": 1.0,
    }

    derived = compute_derived_parameters(input_values)

    assert derived["GLOB"] == 3.0
    assert derived["AG_RATIO"] == 1.5
    assert derived["IBIL"] == 1.5
    assert derived["VLDL"] == 30.0
    assert derived["LDL"] == 120.0
    assert derived["BUN_CREAT_RATIO"] == 20.0


# =========================================================================
# 5. TWO-STEP APPROVAL & REPORT AMENDMENT WORKFLOW
# =========================================================================


def test_two_step_workflow_and_amendments(lab_session):
    now = utc_now()
    test_obj = get_or_create_test(lab_session, "CBC-TWO-STEP", "Complete Blood Two Step", "Hematology")
    p_hb = get_or_create_param(lab_session, test_obj.id, "HB_2S", "Hemoglobin Two Step", "g/dL")
    p_plt = get_or_create_param(lab_session, test_obj.id, "PLT_2S", "Platelet Two Step", "10^9/L")

    order = LabOrder(
        order_no=generate_order_number(lab_session),
        patient_id=2,
        patient_name="Bob Henderson",
        patient_mrn="MRN-BOB-02",
        priority="Routine",
        status="in_progress",
        department="Hematology",
        ordered_at=now,
        tat_deadline=now + timedelta(hours=4),
        total_price=30.0,
    )
    lab_session.add(order)
    lab_session.commit()
    lab_session.refresh(order)

    item = LabOrderItem(
        lab_order_id=order.id,
        test_id=test_obj.id,
        test_code=test_obj.code,
        test_name=test_obj.name,
        department=test_obj.department,
        price=30.0,
    )
    lab_session.add(item)
    lab_session.commit()
    lab_session.refresh(item)

    # Step 1: Technician enters results
    results_in = [
        {"parameter_id": p_hb.id, "value_text": "14.2", "lab_order_item_id": item.id},
        {"parameter_id": p_plt.id, "value_text": "220", "lab_order_item_id": item.id},
    ]
    order_updated, results, has_crit = enter_order_results(
        lab_session, order.id, results_in, "tech.alex"
    )
    assert order_updated.status == "result_entered"
    assert len(results) == 2

    report = lab_session.exec(select(LabReport).where(LabReport.lab_order_id == order.id)).first()
    assert report.status == "draft"
    assert report.version == 1

    # Step 2: Senior Pathologist approves report
    report_approved = approve_lab_order(lab_session, order.id, "Dr. Victor Vance, MD")
    assert report_approved.status == "approved"
    assert report_approved.approved_by == "Dr. Victor Vance, MD"

    lab_session.refresh(order_updated)
    assert order_updated.status == "approved"

    # Immutability check: technician cannot directly edit approved order
    with pytest.raises(ValueError, match="Cannot edit an already approved report"):
        enter_order_results(lab_session, order.id, results_in, "tech.alex")

    # Step 3: Pathologist amends the approved report
    amend_results = [
        {"parameter_id": p_plt.id, "value_text": "245"},  # Corrected platelet count
    ]
    amended_report = amend_approved_report(
        lab_session,
        lab_order_id=order.id,
        pathologist_name="Dr. Victor Vance, MD",
        amendment_reason="Dilution re-check confirms higher platelet count",
        updated_results_data=amend_results,
    )
    assert amended_report.version == 2
    assert amended_report.is_corrected_report is True
    assert "Dilution re-check" in amended_report.amendment_reason

    # Verify updated result version
    plt_res = lab_session.exec(
        select(Result).where(Result.lab_order_id == order.id, Result.parameter_id == p_plt.id)
    ).first()
    assert plt_res.value_text == "245"
    assert plt_res.version == 2


# =========================================================================
# 6. CRITICAL VALUE NOTIFICATION & DOCTOR ACKNOWLEDGEMENT
# =========================================================================


def test_critical_value_and_acknowledgement(lab_session):
    now = utc_now()
    test_obj = get_or_create_test(lab_session, "CRIT-TST-ACK", "Critical Test Ack", "Biochemistry", "Serum")
    p_k = get_or_create_param(lab_session, test_obj.id, "POTASSIUM_ACK", "Potassium Ack", "mmol/L")

    rng = lab_session.exec(select(ReferenceRange).where(ReferenceRange.parameter_id == p_k.id)).first()
    if not rng:
        rng = ReferenceRange(
            parameter_id=p_k.id,
            gender="All",
            low_normal=3.5,
            high_normal=5.0,
            critical_low=2.8,
            critical_high=6.2,
        )
        lab_session.add(rng)
        lab_session.commit()

    order = LabOrder(
        order_no=generate_order_number(lab_session),
        patient_id=3,
        patient_name="Clara Oswald",
        patient_mrn="MRN-CLARA-03",
        priority="STAT",
        status="in_progress",
        department="Biochemistry",
        ordered_at=now,
        tat_deadline=now + timedelta(minutes=60),
    )
    lab_session.add(order)
    lab_session.commit()
    lab_session.refresh(order)

    item = LabOrderItem(
        lab_order_id=order.id,
        test_id=test_obj.id,
        test_code=test_obj.code,
        test_name=test_obj.name,
        department=test_obj.department,
    )
    lab_session.add(item)
    lab_session.commit()
    lab_session.refresh(item)

    # Record subscribed event
    critical_events = []
    def on_crit(evt):
        critical_events.append(evt)
    event_bus.subscribe("result.critical", on_crit)

    # Enter critical high potassium: 6.8 mmol/L
    order_up, results, has_crit = enter_order_results(
        lab_session,
        order.id,
        [{"parameter_id": p_k.id, "value_text": "6.8"}],
        "tech.alex",
    )
    assert has_crit is True
    res_k = results[0]
    assert res_k.is_critical is True
    assert res_k.flag == "Critical_High"
    assert len(critical_events) >= 1

    # Doctor acknowledges critical value
    ack_res = acknowledge_critical_value(
        lab_session,
        res_k.id,
        doctor_name="Dr. Sarah Chen, MD",
        notes="Notified via telephone. Initiated urgent calcium gluconate and insulin-dextrose.",
    )
    assert ack_res.critical_acknowledged_by == "Dr. Sarah Chen, MD"
    assert "calcium gluconate" in ack_res.critical_acknowledged_notes
    assert ack_res.critical_acknowledged_at is not None


# =========================================================================
# 7. INSTRUMENT CSV IMPORT
# =========================================================================


def test_instrument_csv_import(lab_session):
    now = utc_now()
    test_obj = get_or_create_test(lab_session, "ANA-TST-CSV", "Analyzer CSV Test", "Hematology", "Whole Blood")
    param = get_or_create_param(lab_session, test_obj.id, "WBC_CSV", "WBC Analyzer CSV", "10^9/L")

    order = LabOrder(
        order_no=generate_order_number(lab_session),
        patient_id=4,
        patient_name="Analyzer Patient",
        patient_mrn="MRN-ANA-04",
        status="in_progress",
        department="Hematology",
        ordered_at=now,
        tat_deadline=now + timedelta(hours=4),
    )
    lab_session.add(order)
    lab_session.commit()
    lab_session.refresh(order)

    barcode = generate_specimen_barcode(lab_session)
    specimen = Specimen(lab_order_id=order.id, barcode=barcode, specimen_type="Whole Blood", status="received")
    lab_session.add(specimen)
    lab_session.commit()

    item = LabOrderItem(
        lab_order_id=order.id,
        test_id=test_obj.id,
        test_code=test_obj.code,
        test_name=test_obj.name,
        department=test_obj.department,
        specimen_id=specimen.id,
    )
    lab_session.add(item)
    lab_session.commit()

    # CSV with matching barcode
    csv_data = f"""barcode,parameter_code,value
{barcode},WBC_CSV,8.4
"""
    result = import_instrument_results_csv(lab_session, csv_data, "tech.alex")
    assert result["imported_count"] == 1
    assert result["skipped_count"] == 0

    # Verify result saved
    saved_res = lab_session.exec(
        select(Result).where(Result.lab_order_id == order.id, Result.parameter_code == "WBC_CSV")
    ).first()
    assert saved_res is not None
    assert saved_res.value_text == "8.4"


# =========================================================================
# 8. EVENT INTEGRATION: order.placed PROVISIONS LAB ORDER
# =========================================================================


def test_order_placed_event_subscription(lab_session):
    # Ensure event handler is subscribed
    event_bus.subscribe("order.placed", handle_order_placed)

    # Emit order.placed event with type "Lab"
    event_bus.emit(
        name="order.placed",
        payload={
            "order_id": 999,
            "encounter_id": 88,
            "type": "Lab",
            "test_name": "Complete Blood Count",
            "priority": "STAT",
            "patient_id": 5,
            "patient_name": "Diana Prince",
            "patient_mrn": "MRN-DIANA-05",
            "patient_gender": "Female",
            "doctor_name": "Dr. Bruce Wayne",
            "clinical_notes": "Suspected acute sepsis",
        },
    )

    # Verify that LabOrder and Specimen were auto-provisioned
    order = lab_session.exec(
        select(LabOrder).where(LabOrder.patient_mrn == "MRN-DIANA-05")
    ).first()
    assert order is not None
    assert order.priority == "STAT"
    assert order.status == "ordered"

    specimen = lab_session.exec(
        select(Specimen).where(Specimen.lab_order_id == order.id)
    ).first()
    assert specimen is not None
    assert specimen.barcode.startswith("SPEC-")
    assert specimen.status == "pending_collection"


# =========================================================================
# 9. REPORTLAB PDF REPORT GENERATION
# =========================================================================


def test_lab_report_pdf_generation(lab_session):
    now = utc_now()
    order = LabOrder(
        order_no=generate_order_number(lab_session),
        patient_id=6,
        patient_name="Arthur Dent",
        patient_mrn="MRN-ART-06",
        priority="Routine",
        status="approved",
        department="Hematology",
        ordered_at=now,
        tat_deadline=now + timedelta(hours=4),
    )
    lab_session.add(order)
    lab_session.commit()
    lab_session.refresh(order)

    report = LabReport(
        lab_order_id=order.id,
        report_no=generate_report_number(lab_session),
        version=1,
        status="approved",
        approved_by="Dr. Victor Vance, MD",
        approved_at=now,
    )
    lab_session.add(report)

    param = get_or_create_param(lab_session, 1, "HB_PDF_GEN", "Hemoglobin PDF", "g/dL")

    res = Result(
        lab_order_id=order.id,
        lab_order_item_id=1,
        parameter_id=param.id,
        parameter_code=param.code,
        parameter_name=param.name,
        value_text="15.0",
        unit="g/dL",
        reference_range_display="13.5 - 17.5 g/dL",
        flag="Normal",
    )
    lab_session.add(res)
    lab_session.commit()

    pdf_bytes = generate_lab_report_pdf(lab_session, order.id)
    assert pdf_bytes is not None
    assert len(pdf_bytes) > 500
    assert pdf_bytes.startswith(b"%PDF")
