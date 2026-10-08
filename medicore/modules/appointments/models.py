from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import Column, JSON, Index, text
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Doctor(SQLModel, table=True):
    __tablename__ = "doctors"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    department: str = Field(index=True)  # e.g. Cardiology, Neurology, Pediatrics, Orthopedics, General Medicine
    specialty: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    room_number: Optional[str] = None
    slot_length: int = Field(default=20)  # Slot length in minutes

    # Weekly schedule template: {"Monday": [["09:00", "12:00"], ["14:00", "17:00"]], ...}
    schedule_templates: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Date specific exceptions: {"2026-10-15": [["10:00", "13:00"]]}
    exceptions: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Leave dates: ["2026-10-25", "2026-10-26"]
    leave: List[str] = Field(default_factory=list, sa_column=Column(JSON))

    user_id: Optional[int] = Field(default=None, index=True)  # Optional link to User account
    is_active: bool = Field(default=True, index=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Appointment(SQLModel, table=True):
    __tablename__ = "appointments"
    __table_args__ = (
        Index(
            "uq_appointment_doctor_slot",
            "doctor_id", "appointment_date", "start_time",
            unique=True,
            sqlite_where=text("status != 'Cancelled'"),
            postgresql_where=text("status != 'Cancelled'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(index=True)
    patient_name: str = Field(index=True)
    patient_mrn: str = Field(index=True)
    patient_phone: Optional[str] = None

    doctor_id: int = Field(index=True)
    doctor_name: str = Field(index=True)
    department: str = Field(index=True)

    appointment_date: str = Field(index=True)  # YYYY-MM-DD
    start_time: str = Field(index=True)        # HH:MM
    end_time: str                              # HH:MM
    start_datetime: datetime = Field(index=True)
    end_datetime: datetime = Field(index=True)

    type: str = Field(default="Consultation", index=True)  # Consultation, Follow-up, Emergency, Routine Checkup, Walk-in
    status: str = Field(default="Booked", index=True)      # Booked, Checked-in, In-Consultation, Completed, Cancelled, No-show
    source: str = Field(default="Reception", index=True)   # Online, Phone, Reception, Walk-in, Referral
    notes: Optional[str] = None

    checked_in_at: Optional[datetime] = None
    consultation_started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    reminder_sent: bool = Field(default=False, index=True)

    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class QueueToken(SQLModel, table=True):
    __tablename__ = "queue_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    token_number: str = Field(index=True)      # e.g. "CARD-01", "PED-03"
    appointment_id: Optional[int] = Field(default=None, index=True)
    patient_id: int = Field(index=True)
    patient_name: str
    patient_mrn: str

    doctor_id: int = Field(index=True)
    doctor_name: str
    department: str = Field(index=True)
    room_number: Optional[str] = None

    date: str = Field(index=True)              # YYYY-MM-DD
    status: str = Field(default="Waiting", index=True)  # Waiting, Called, Serving, Done, Skipped
    sequence: int = Field(default=1)
    is_walk_in: bool = Field(default=False)

    called_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
