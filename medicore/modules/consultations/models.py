from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from sqlmodel import Column, Field, JSON, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Encounter(SQLModel, table=True):
    __tablename__ = "encounters"

    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(index=True)
    patient_name: str
    patient_mrn: str
    doctor_id: int = Field(index=True)
    doctor_name: str
    appointment_id: Optional[int] = Field(default=None, index=True)
    status: str = Field(default="In-Progress", index=True)  # "In-Progress", "Completed", "Cancelled"
    department: str = Field(default="General Medicine")
    chief_complaint: Optional[str] = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Vitals(SQLModel, table=True):
    __tablename__ = "vitals"

    id: Optional[int] = Field(default=None, primary_key=True)
    encounter_id: Optional[int] = Field(default=None, index=True)
    patient_id: int = Field(index=True)
    recorded_at: datetime = Field(default_factory=utc_now)
    recorded_by: Optional[str] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    bmi: Optional[float] = None
    bsa: Optional[float] = None
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    pulse: Optional[int] = None
    temp_c: Optional[float] = None
    spo2: Optional[float] = None
    resp_rate: Optional[int] = None
    notes: Optional[str] = None
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class ClinicalNote(SQLModel, table=True):
    __tablename__ = "clinical_notes"

    id: Optional[int] = Field(default=None, primary_key=True)
    encounter_id: int = Field(index=True)
    specialty: str = Field(default="General Medicine")
    subjective: Optional[str] = None
    objective: Optional[str] = None
    assessment: Optional[str] = None
    plan: Optional[str] = None
    is_signed: bool = Field(default=False, index=True)
    signed_at: Optional[datetime] = None
    signed_by: Optional[str] = None
    signed_by_user_id: Optional[int] = None
    addenda: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Diagnosis(SQLModel, table=True):
    __tablename__ = "diagnoses"

    id: Optional[int] = Field(default=None, primary_key=True)
    encounter_id: int = Field(index=True)
    patient_id: int = Field(index=True)
    icd10_code: str = Field(index=True)
    description: str
    is_primary: bool = Field(default=True)
    notes: Optional[str] = None
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Prescription(SQLModel, table=True):
    __tablename__ = "prescriptions"

    id: Optional[int] = Field(default=None, primary_key=True)
    encounter_id: int = Field(index=True)
    patient_id: int = Field(index=True)
    doctor_id: int = Field(index=True)
    doctor_name: Optional[str] = None
    prescribed_at: datetime = Field(default_factory=utc_now)
    status: str = Field(default="Active")  # "Active", "Dispensed", "Discontinued"
    safety_override_reason: Optional[str] = None
    notes: Optional[str] = None
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class PrescriptionItem(SQLModel, table=True):
    __tablename__ = "prescription_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    prescription_id: int = Field(index=True)
    drug_name: str
    generic_name: Optional[str] = None
    dosage: str
    frequency: str
    route: str = Field(default="Oral")
    duration_days: int = Field(default=5)
    quantity: int = Field(default=10)
    instructions: Optional[str] = None
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Order(SQLModel, table=True):
    __tablename__ = "orders"

    id: Optional[int] = Field(default=None, primary_key=True)
    encounter_id: int = Field(index=True)
    patient_id: int = Field(index=True)
    doctor_id: int = Field(index=True)
    type: str = Field(default="Lab")  # "Lab", "Imaging"
    test_name: str
    priority: str = Field(default="Routine")  # "Routine", "Urgent", "Stat"
    clinical_notes: Optional[str] = None
    status: str = Field(default="Placed")  # "Placed", "In-Progress", "Completed", "Cancelled"
    placed_at: datetime = Field(default_factory=utc_now)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class FollowUp(SQLModel, table=True):
    __tablename__ = "follow_ups"

    id: Optional[int] = Field(default=None, primary_key=True)
    encounter_id: int = Field(index=True)
    patient_id: int = Field(index=True)
    doctor_id: int = Field(index=True)
    recommended_date: date
    appointment_id: Optional[int] = None
    notes: Optional[str] = None
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Referral(SQLModel, table=True):
    __tablename__ = "referrals"

    id: Optional[int] = Field(default=None, primary_key=True)
    encounter_id: int = Field(index=True)
    patient_id: int = Field(index=True)
    referring_doctor_id: int
    referring_doctor_name: str
    referred_to_specialty: str
    referred_to_doctor: Optional[str] = None
    reason: str
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
