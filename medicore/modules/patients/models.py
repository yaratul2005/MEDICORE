from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Patient(SQLModel, table=True):
    __tablename__ = "patients"

    id: Optional[int] = Field(default=None, primary_key=True)
    mrn: str = Field(index=True, unique=True)  # e.g. "MC-2026-00001"
    first_name: str = Field(index=True)
    last_name: str = Field(index=True)
    date_of_birth: str = Field(index=True)     # YYYY-MM-DD
    gender: str = Field(default="Other")       # "Male", "Female", "Other"
    blood_group: Optional[str] = Field(default=None)  # "A+", "O+", etc.
    phone: str = Field(index=True)
    email: Optional[str] = None
    address: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    allergies: Optional[str] = None
    status: str = Field(default="Active", index=True)  # "Active", "Inpatient", "Discharged", "Critical"

    # Dynamic custom fields stored as JSON
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"
