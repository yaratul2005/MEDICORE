# MediCore - Hospital Information System (HIS)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com)
[![SQLModel](https://img.shields.io/badge/SQLModel-0.0.16+-red.svg)](https://sqlmodel.tiangolo.com)
[![HTMX](https://img.shields.io/badge/HTMX-1.9.12-blueviolet.svg)](https://htmx.org)
[![Alpine.js](https://img.shields.io/badge/Alpine.js-3.13+-77C1D2.svg)](https://alpinejs.dev)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

MediCore is a high-performance, modular monolith Hospital Information System designed for outpatient and inpatient clinical workflows. Built with FastAPI, SQLModel, Jinja2, HTMX, and Alpine.js, it pairs desktop-dense clinical interfaces with high development velocity.

---

## 🏥 Architecture & Key Principles

- **Modular Monolith & Plugin Architecture**: Each clinical department lives as an autonomous package in `medicore/modules/`. Core auto-discovers manifests, routes, navigation entries, permissions, and command palette actions on startup.
- **Zero Cross-Module Direct Imports**: Modules communicate strictly through asynchronous domain events on the `EventBus` (`event_bus.emit()`, `event_bus.subscribe()`).
- **Metadata-Driven UI**: Define an entity once as an `EntitySchema` (fields, validations, badges, list columns, filters). Generic Jinja2 + HTMX renderers generate the dense table, filter bar, split-pane detail drawer, and forms automatically.
- **Dynamic Custom Fields**: System administrators can attach ad-hoc fields to any entity at runtime. These are stored in schema-less JSON columns and dynamically appear in forms, tables, and filters.
- **Dynamic RBAC & Audit Trail**: Granular permissions formatted as `module.entity.action` (e.g., `appointments.appointment.create`, `patients.patient.read`). Roles and permissions reside in the database and are enforced at route, template, and field levels. Comprehensive audit log tracks all clinical changes.
- **Design Tokens & Density**: Design tokens driven by CSS variables in `tokens.css`; light/dark themes and compact/comfortable densities are saved per user.

---

## 📦 Implemented Modules

### 1. Patients Directory
- Master-detail split-pane view with dense data table and multi-field filters (status, gender, blood group).
- Clinical allergy alerts banner prominently visible on patient files.
- Automated sequential MRN generation (`MC-YYYY-NNNNN`) configurable in settings.
- Real-time client-side duplicate detection by phone, name, and date of birth.
- Direct booking action from patient detail drawer.

### 2. Appointments & Scheduling
- **Doctor Schedules**: Weekly schedule templates, date-specific exceptions, and leave days. Slot generation respects clinician slot length (`slot_length`) and active status.
- **Day & Week Calendar**: Interactive calendar with Day view (hourly slots) and Week view (7-day multi-doctor grid), color-coded by status with HTML5 drag-and-drop rescheduling.
- **Conflict & Double-Booking Guard**: Prevents overlapping bookings with overbooking threshold rules configurable in hospital settings.
- **Walk-in Intake & Live Tokens**: Quick walk-in registration with instant departmental sequence tokens (e.g. `CARD-01`, `PED-02`, `GEN-03`).
- **Waiting Room TV Display Screen**: Dedicated full-screen monitor view at `/appointments/display` with 5-second HTMX auto-refreshing polling, "Now Calling" flashing alerts, and live queue status.
- **Queue Desk Control Board**: Nurse/reception desk interface to call, start serving, mark completed, or skip patient tokens.
- **Status Lifecycle Flow**: `Booked` ➔ `Checked-in` ➔ `In-Consultation` ➔ `Completed` / `Cancelled` / `No-show`, emitting events on the domain bus.
- **Pluggable Reminders**: Background notification scheduler scanning 24-48h upcoming bookings.
- **Clinical Activity Widget**: Dashboard metrics tracking today's visits, active statuses, no-show rate %, and average waiting time in minutes.

---

## 🚀 Quickstart Guide

### 1. Prerequisites
- Python 3.10+ (Python 3.11, 3.12, 3.13, 3.14 supported)
- [uv](https://github.com/astral-sh/uv) (recommended) or standard `pip`

### 2. Setup Virtual Environment & Install Dependencies
```bash
# Clone the repository
git clone https://github.com/yaratul2005/MEDICORE.git
cd MEDICORE

# Create virtual environment and install packages
uv venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
```

### 3. Database Migrations
MediCore uses Alembic for database migrations from day one:
```bash
alembic upgrade head
```

### 4. Seed Clinical Data
Populates 200 realistic patient records, 5 doctors across 5 departments, 100 appointments, active queue tokens, and default RBAC roles:
```bash
python -m medicore.seed.seeder
```

### 5. Run Automated Tests
```bash
pytest -v
```
All 17 tests verify slot generation, booking conflicts, status transitions, token issuance, patient duplicates, and display endpoints.

### 6. Start the Server
```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

---

## 🌐 Application Access & Routes

| Section | URL | Description |
| :--- | :--- | :--- |
| **Appointments & Calendar** | [http://127.0.0.1:8000/appointments](http://127.0.0.1:8000/appointments) | Day/Week calendar, dense table, and queue desk |
| **Waiting Room TV Display** | [http://127.0.0.1:8000/appointments/display](http://127.0.0.1:8000/appointments/display) | Full-screen outpatient TV monitor with 5s auto-refresh |
| **Patients Directory** | [http://127.0.0.1:8000/patients](http://127.0.0.1:8000/patients) | Patient records, MRN intake, clinical allergy alerts |
| **System Settings** | [http://127.0.0.1:8000/settings](http://127.0.0.1:8000/settings) | Hospital profile, MRN pattern, departments, overbooking rules |
| **RBAC & Audit Trail** | [http://127.0.0.1:8000/audit](http://127.0.0.1:8000/audit) | Live immutable clinical audit logs |

---

## 🔑 Default User Accounts

| Username | Password | Role | Description |
| :--- | :--- | :--- | :--- |
| `admin` | `admin123` | **SuperAdmin** | Full system access, settings, and audit logs |
| `dr.sarah` | `doctor123` | **Doctor** | Clinical practitioner (Cardiology), patient and schedule management |
| `receptionist.mary` | `reception123` | **Receptionist** | Front desk, patient intake, booking, walk-ins, and queue calls |
| `nurse.john` | `nurse123` | **Nurse** | Outpatient triage and queue management |

---

## 📖 Extension Guide

To add a new clinical module (e.g. `billing`, `pharmacy`, `laboratory`) in 5 simple steps without touching existing core code, refer to [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 📄 License
This project is licensed under the MIT License.
