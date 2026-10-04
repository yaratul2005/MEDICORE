# MediCore Architecture & Module Extension Guide

MediCore is an enterprise-grade Hospital Management System built as an **extensible modular monolith**. It combines high developer velocity with rigorous clinical safety, dynamic RBAC, domain decoupling, and metadata-driven interfaces.

---

## 1. Architectural Highlights

- **Modular Monolith**: Every clinical domain (e.g., `patients`, `appointments`, `billing`) is encapsulated in its own isolated subpackage under `medicore/modules/`.
- **Zero Cross-Module Direct Imports**: Modules communicate strictly through domain events on the asynchronous `EventBus` (`event_bus.emit()`, `event_bus.subscribe()`).
- **Metadata-Driven UI**: Define an entity once as an `EntitySchema` (fields, validations, badges, list columns, filters). Generic Jinja2 + HTMX renderers construct the dense table, filter bar, split-pane detail view, and modals automatically. Custom templates serve as escape hatches.
- **Dynamic Custom Fields**: System administrators can attach ad-hoc fields to any entity at runtime. These are stored in a schema-less JSON column and dynamically appear in forms, tables, and filters.
- **Dynamic RBAC**: Granular permissions formatted as `module.entity.action` (e.g., `patients.patient.read`, `patients.patient.create`). Roles and permissions reside in the database and are enforced at route, template, and field levels.
- **Hospital Settings Registry**: Organization profile, emergency hotline, clinical departments, and MRN/invoice numbering formats are stored in the database with caching and an administrative UI.
- **Clinical Design System**: Theming (light/dark) and density (compact/comfortable) are driven by CSS variables in `tokens.css` and saved per user.

---

## 2. Directory Layout

```
medicore/
  core/           # Configuration, Database engine, Security/RBAC, EventBus, Registry, UI Schema, Settings
  modules/        # Isolated clinical modules (auto-discovered on boot)
    patients/     # Demo reference module
      manifest.py # Module declaration (nav, permissions, search, palette, widgets)
      models.py   # SQLModel database definitions
      schema.py   # EntitySchema definition
      router.py   # HTMX-powered routes & partials
  ui/
    static/       # tokens.css (theming/density), app.js (palette, toasts, shortcuts)
    templates/    # base.html, macros (table, form, filters), core views
  seed/           # Realistic clinical data generators (200 patients, roles, settings)
  tests/          # Pytest test suite for core & modules
alembic/          # Database migrations from day one
bootstrap.py      # Idempotent setup for Colab & production
```

---

## 3. How to Add a New Module in 5 Steps

Adding a new clinical domain (e.g., `appointments`, `prescriptions`, `labs`) requires **zero modifications** to core files.

### Step 1: Create the Module Folder
Create a folder inside `medicore/modules/`:
```bash
mkdir -p medicore/modules/appointments
touch medicore/modules/appointments/__init__.py
```

### Step 2: Define Models (`models.py`)
Define your SQLModel tables. Always include a `custom_fields` JSON column to enable dynamic admin fields:
```python
# medicore/modules/appointments/models.py
from sqlmodel import SQLModel, Field, Column, JSON
from typing import Optional, Dict, Any
from datetime import datetime

class Appointment(SQLModel, table=True):
    __tablename__ = "appointments"
    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(index=True)
    doctor_name: str
    scheduled_time: datetime = Field(index=True)
    status: str = Field(default="Scheduled") # Scheduled, In-Progress, Completed, Cancelled
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
```

### Step 3: Define the Entity Schema (`schema.py`)
Define fields, list columns, badges, and filters:
```python
# medicore/modules/appointments/schema.py
from medicore.core.ui_schema import EntitySchema, FieldDef, ColumnDef, FilterDef

appointment_schema = EntitySchema(
    entity_name="appointment",
    title="Appointment",
    plural_title="Appointments",
    endpoint_prefix="/appointments",
    fields=[
        FieldDef(name="doctor_name", label="Doctor", field_type="string", required=True),
        FieldDef(name="scheduled_time", label="Scheduled At", field_type="datetime", required=True),
        FieldDef(name="status", label="Status", field_type="select", options=["Scheduled", "In-Progress", "Completed", "Cancelled"]),
    ],
    list_columns=[
        ColumnDef(field_name="doctor_name", label="Doctor"),
        ColumnDef(field_name="scheduled_time", label="Time", formatter="datetime"),
        ColumnDef(field_name="status", label="Status", formatter="badge"),
    ],
    filters=[
        FilterDef(field_name="status", label="Status", filter_type="select", options=["Scheduled", "Completed"]),
    ]
)
```

### Step 4: Implement Router & Emit Domain Events (`router.py`)
Implement the endpoints using HTMX partials. Whenever mutating state, emit a domain event instead of calling another module:
```python
# medicore/modules/appointments/router.py
from fastapi import APIRouter, Depends, Request
from medicore.core.events import event_bus
from medicore.core.security import require_permission

router = APIRouter(prefix="/appointments", tags=["Appointments"])

@router.post("")
async def create_appointment(request: Request, user = Depends(require_permission("appointments.appointment.create"))):
    # Save record...
    event_bus.emit("appointment.scheduled", {"appointment_id": 1, "patient_id": 42}, user_id=user.id)
    return {"status": "created"}
```

### Step 5: Declare the Module Manifest (`manifest.py`)
Expose the `manifest` object in `manifest.py`. Core will automatically discover it on boot, mounting its routes, navigation items, command palette actions, and global search:
```python
# medicore/modules/appointments/manifest.py
from medicore.core.registry import ModuleManifest, NavEntry, PermissionDef, PaletteAction
from medicore.modules.appointments.router import router
from medicore.modules.appointments.models import Appointment

manifest = ModuleManifest(
    name="appointments",
    label="Appointments",
    icon="calendar",
    order=20,
    nav_entries=[
        NavEntry(label="Appointments", url="/appointments", icon="calendar", permission="appointments.appointment.read")
    ],
    permissions=[
        PermissionDef(code="appointments.appointment.read", entity="appointment", action="read"),
        PermissionDef(code="appointments.appointment.create", entity="appointment", action="create"),
    ],
    router=router,
    models=[Appointment],
    command_palette_actions=[
        PaletteAction(id="appt_new", title="Schedule Appointment", category="Appointments", url="/appointments/new", icon="calendar-plus")
    ]
)
```

Restart or bootstrap your application. The new module seamlessly integrates into the sidebar, permission matrix, global search, and command palette!
