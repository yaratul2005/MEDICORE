from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlmodel import Session, select
from medicore.core.database import engine
from medicore.core.models import Setting

DEFAULT_SETTINGS = [
    # Hospital Profile
    {
        "namespace": "hospital",
        "key": "profile",
        "label": "Hospital Information",
        "description": "Hospital organization name, contacts, and license details",
        "value": {
            "name": "MediCore Central Hospital",
            "tagline": "Tertiary Care & Clinical Research Center",
            "phone": "+1 (555) 019-2834",
            "emergency_hotline": "+1 (555) 911-0000",
            "email": "admissions@medicore-health.org",
            "address": "450 Medical Center Boulevard, Metro City, MC 90210",
            "license_number": "HOSP-MED-2026-9811",
            "currency": "USD",
            "currency_symbol": "$",
        },
    },
    # Departments
    {
        "namespace": "departments",
        "key": "list",
        "label": "Clinical Departments",
        "description": "List of active hospital departments and clinical units",
        "value": [
            {"code": "EMERG", "name": "Emergency & Trauma", "active": True},
            {"code": "CARDIO", "name": "Cardiology", "active": True},
            {"code": "PED", "name": "Pediatrics & Neonatal", "active": True},
            {"code": "GENMED", "name": "General Internal Medicine", "active": True},
            {"code": "SURG", "name": "General Surgery", "active": True},
            {"code": "ORTHO", "name": "Orthopedics & Sports Medicine", "active": True},
            {"code": "NEURO", "name": "Neurology", "active": True},
            {"code": "ICU", "name": "Intensive Care Unit (ICU)", "active": True},
        ],
    },
    # Numbering patterns
    {
        "namespace": "numbering",
        "key": "mrn",
        "label": "Medical Record Number (MRN) Pattern",
        "description": "Template format for new patient MRN assignment",
        "value": {
            "prefix": "MC",
            "pattern": "MC-{YEAR}-{SEQ:5}",
            "current_sequence": 200,
        },
    },
    # Working hours
    {
        "namespace": "operations",
        "key": "working_hours",
        "label": "Operating Hours",
        "description": "Outpatient department and visiting hours",
        "value": {
            "opd_start": "08:00",
            "opd_end": "20:00",
            "visiting_hours": "14:00 - 18:00",
            "emergency_24_7": True,
        },
    },
    # Modules enabled
    {
        "namespace": "modules",
        "key": "enabled",
        "label": "Enabled Modules",
        "description": "Dynamic toggle for active modular monolith modules",
        "value": {
            "patients": True,
        },
    },
]


class SettingsRegistry:
    def __init__(self):
        self._cache: Dict[str, Any] = {}

    def init_defaults(self, session: Optional[Session] = None):
        """Seed default settings into DB if not present"""
        def _apply(s: Session):
            for default in DEFAULT_SETTINGS:
                existing = s.exec(
                    select(Setting).where(
                        Setting.namespace == default["namespace"],
                        Setting.key == default["key"],
                    )
                ).first()
                if not existing:
                    entry = Setting(
                        namespace=default["namespace"],
                        key=default["key"],
                        label=default["label"],
                        description=default.get("description"),
                        value=default["value"],
                    )
                    s.add(entry)
            s.commit()

        if session:
            _apply(session)
        else:
            with Session(engine) as s:
                _apply(s)

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        cache_key = f"{namespace}:{key}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        with Session(engine) as session:
            entry = session.exec(
                select(Setting).where(Setting.namespace == namespace, Setting.key == key)
            ).first()
            if entry is not None:
                self._cache[cache_key] = entry.value
                return entry.value

        return default

    def set(self, namespace: str, key: str, value: Any, label: Optional[str] = None, description: Optional[str] = None):
        cache_key = f"{namespace}:{key}"
        self._cache[cache_key] = value

        with Session(engine) as session:
            entry = session.exec(
                select(Setting).where(Setting.namespace == namespace, Setting.key == key)
            ).first()
            if entry:
                entry.value = value
                if label:
                    entry.label = label
                if description:
                    entry.description = description
                entry.updated_at = datetime.now(timezone.utc)
            else:
                entry = Setting(
                    namespace=namespace,
                    key=key,
                    label=label or key.replace("_", " ").title(),
                    description=description,
                    value=value,
                )
            session.add(entry)
            session.commit()

    def get_namespace(self, namespace: str) -> Dict[str, Any]:
        with Session(engine) as session:
            entries = session.exec(
                select(Setting).where(Setting.namespace == namespace)
            ).all()
            return {e.key: e.value for e in entries}

    def generate_mrn(self) -> str:
        """Atomically generate the next MRN using the configured pattern"""
        with Session(engine) as session:
            entry = session.exec(
                select(Setting).where(Setting.namespace == "numbering", Setting.key == "mrn")
            ).first()
            if not entry:
                seq = 1
                pattern = "MC-{YEAR}-{SEQ:5}"
            else:
                data = dict(entry.value or {})
                seq = data.get("current_sequence", 0) + 1
                pattern = data.get("pattern", "MC-{YEAR}-{SEQ:5}")
                data["current_sequence"] = seq
                entry.value = data
                session.add(entry)
                session.commit()
                cache_key = "numbering:mrn"
                self._cache[cache_key] = data

            year = datetime.now(timezone.utc).year
            # Format pattern e.g. MC-2026-00001
            mrn = pattern.replace("{YEAR}", str(year)).replace("{SEQ:5}", f"{seq:05d}")
            return mrn


settings_registry = SettingsRegistry()
