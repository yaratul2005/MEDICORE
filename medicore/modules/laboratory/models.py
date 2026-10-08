from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from sqlmodel import Column, Field, JSON, Relationship, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TestCatalog(SQLModel, table=True):
    """Laboratory test definition / catalog item."""
    __tablename__ = "laboratory_tests"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)  # e.g., "CBC", "LFT", "KFT", "LIPID"
    name: str = Field(index=True)
    department: str = Field(index=True)  # Hematology, Biochemistry, Microbiology, Pathology, Urinalysis, Serology
    specimen_type: str = Field(default="Whole Blood (EDTA)")  # Whole Blood, Serum, Plasma, Urine, etc.
    tat_hours: int = Field(default=4)  # Turnaround time in hours
    price: float = Field(default=0.0)
    description: Optional[str] = Field(default=None)
    is_panel: bool = Field(default=False)
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    __test__ = False


class TestPanel(SQLModel, table=True):
    """Panel grouping tests or parameter profiles."""
    __tablename__ = "laboratory_panels"
    __test__ = False

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str = Field(index=True)
    department: str = Field(index=True)
    description: Optional[str] = Field(default=None)
    test_ids_json: str = Field(default="[]")  # JSON list of test IDs included
    is_active: bool = Field(default=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Parameter(SQLModel, table=True):
    """Individual analyte/parameter within a test (e.g. Hemoglobin, SGPT, Creatinine)."""
    __tablename__ = "laboratory_parameters"

    id: Optional[int] = Field(default=None, primary_key=True)
    test_id: int = Field(foreign_key="laboratory_tests.id", index=True)
    code: str = Field(index=True)  # e.g. "HB", "WBC", "PLT", "TBIL"
    name: str = Field(index=True)
    unit: str = Field(default="")  # e.g. "g/dL", "mg/dL", "10^9/L", "U/L"
    loinc_code: Optional[str] = Field(default=None, index=True)  # LOINC code for FHIR interoperability
    data_type: str = Field(default="numeric")  # numeric, text, calculated
    calculation_formula: Optional[str] = Field(default=None)  # e.g. "albumin / (total_protein - albumin)"
    display_order: int = Field(default=0)
    is_active: bool = Field(default=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class ReferenceRange(SQLModel, table=True):
    """Age and sex specific reference intervals with critical thresholds."""
    __tablename__ = "laboratory_reference_ranges"

    id: Optional[int] = Field(default=None, primary_key=True)
    parameter_id: int = Field(foreign_key="laboratory_parameters.id", index=True)
    gender: str = Field(default="All")  # All, Male, Female
    age_min_years: float = Field(default=0.0)
    age_max_years: float = Field(default=120.0)
    low_normal: Optional[float] = Field(default=None)
    high_normal: Optional[float] = Field(default=None)
    critical_low: Optional[float] = Field(default=None)
    critical_high: Optional[float] = Field(default=None)
    text_normal: Optional[str] = Field(default=None)  # For qualitative tests (e.g., "Negative", "Clear")
    notes: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class LabOrder(SQLModel, table=True):
    """Laboratory requisition order."""
    __tablename__ = "laboratory_orders"

    id: Optional[int] = Field(default=None, primary_key=True)
    order_no: str = Field(index=True, unique=True)  # e.g., "LAB-2026-00001"
    order_source: str = Field(default="order.placed", index=True)  # order.placed, encounter, walkin
    encounter_id: Optional[int] = Field(default=None, index=True)
    patient_id: int = Field(index=True)
    patient_name: str = Field(index=True)
    patient_mrn: str = Field(index=True)
    patient_gender: str = Field(default="Other")
    patient_dob: Optional[date] = Field(default=None)
    doctor_id: Optional[int] = Field(default=None, index=True)
    doctor_name: Optional[str] = Field(default=None)
    priority: str = Field(default="Routine", index=True)  # STAT, Urgent, Routine
    status: str = Field(default="ordered", index=True)  # ordered, specimen_collected, specimen_received, in_progress, result_entered, approved, cancelled
    department: str = Field(default="Hematology", index=True)
    ordered_at: datetime = Field(default_factory=utc_now, index=True)
    tat_deadline: datetime = Field(default_factory=utc_now, index=True)
    is_tat_breached: bool = Field(default=False, index=True)
    clinical_notes: Optional[str] = Field(default=None)
    total_price: float = Field(default=0.0)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class LabOrderItem(SQLModel, table=True):
    """Line item in a laboratory requisition."""
    __tablename__ = "laboratory_order_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    lab_order_id: int = Field(foreign_key="laboratory_orders.id", index=True)
    test_id: int = Field(foreign_key="laboratory_tests.id", index=True)
    test_code: str = Field(index=True)
    test_name: str = Field(index=True)
    department: str = Field(index=True)
    specimen_type: str = Field(default="Whole Blood")
    status: str = Field(default="ordered", index=True)  # ordered, specimen_collected, specimen_received, in_progress, completed, cancelled
    price: float = Field(default=0.0)
    specimen_id: Optional[int] = Field(default=None, index=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Specimen(SQLModel, table=True):
    """Biological sample tracking with barcode labeling and lifecycle timeline."""
    __tablename__ = "laboratory_specimens"

    id: Optional[int] = Field(default=None, primary_key=True)
    lab_order_id: int = Field(foreign_key="laboratory_orders.id", index=True)
    barcode: str = Field(index=True, unique=True)  # e.g., "SPEC-2026-00001"
    specimen_type: str = Field(index=True)
    status: str = Field(default="pending_collection", index=True)  # pending_collection, collected, received, rejected
    collected_by: Optional[str] = Field(default=None)
    collected_at: Optional[datetime] = Field(default=None)
    received_by: Optional[str] = Field(default=None)
    received_at: Optional[datetime] = Field(default=None)
    rejected_by: Optional[str] = Field(default=None)
    rejected_at: Optional[datetime] = Field(default=None)
    rejection_reason: Optional[str] = Field(default=None)
    recollect_requested: bool = Field(default=False)
    recollected_specimen_id: Optional[int] = Field(default=None)
    timeline_json: str = Field(default="[]")  # Audit log of specimen lifecycle events
    notes: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Result(SQLModel, table=True):
    """Individual parameter result value, flag, delta comparison, and validation state."""
    __tablename__ = "laboratory_results"

    id: Optional[int] = Field(default=None, primary_key=True)
    lab_order_id: int = Field(foreign_key="laboratory_orders.id", index=True)
    lab_order_item_id: Optional[int] = Field(default=None, foreign_key="laboratory_order_items.id", index=True)
    parameter_id: int = Field(foreign_key="laboratory_parameters.id", index=True)
    parameter_code: str = Field(index=True)
    parameter_name: str
    value_text: str = Field(default="")
    value_numeric: Optional[float] = Field(default=None, index=True)
    unit: str = Field(default="")
    reference_range_display: str = Field(default="")
    flag: str = Field(default="Normal", index=True)  # Normal, H, L, Critical_High, Critical_Low, Abnormal
    is_critical: bool = Field(default=False, index=True)
    delta_previous_value: Optional[str] = Field(default=None)
    delta_previous_date: Optional[date] = Field(default=None)
    critical_acknowledged_by: Optional[str] = Field(default=None)
    critical_acknowledged_at: Optional[datetime] = Field(default=None)
    critical_acknowledged_notes: Optional[str] = Field(default=None)
    version: int = Field(default=1)
    entered_by: Optional[str] = Field(default=None)
    entered_at: Optional[datetime] = Field(default=None)
    validated_by: Optional[str] = Field(default=None)
    validated_at: Optional[datetime] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class LabReport(SQLModel, table=True):
    """Clinical laboratory diagnostic report with versioning and pathologist authorization."""
    __tablename__ = "laboratory_reports"

    id: Optional[int] = Field(default=None, primary_key=True)
    lab_order_id: int = Field(foreign_key="laboratory_orders.id", index=True)
    report_no: str = Field(index=True, unique=True)  # e.g., "REP-2026-00001"
    version: int = Field(default=1)
    status: str = Field(default="draft", index=True)  # draft, approved, amended
    is_corrected_report: bool = Field(default=False)
    amendment_reason: Optional[str] = Field(default=None)
    summary_notes: Optional[str] = Field(default=None)
    approved_by: Optional[str] = Field(default=None)
    approved_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
