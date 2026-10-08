<p align="center">
  <img src="media/medicore-logo.png" alt="MediCore Logo" width="160" />
</p>

<h1 align="center">MediCore</h1>

<p align="center">
  <strong>Next-Generation Modular Hospital Information System (HIS) & Electronic Medical Records (EMR)</strong><br>
  Desktop-dense clinical workflows • Plugin-based modular monolith • Event-driven domain architecture
</p>

<p align="center">
  <a href="#-architecture--key-innovations"><img src="https://img.shields.io/badge/Architecture-Modular%20Monolith-0d9488" alt="Architecture"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white" alt="Python"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://sqlmodel.tiangolo.com/"><img src="https://img.shields.io/badge/SQLModel-0.0.16+-E10098" alt="SQLModel"></a>
  <a href="https://htmx.org/"><img src="https://img.shields.io/badge/HTMX-1.9.12-336699" alt="HTMX"></a>
  <a href="https://alpinejs.dev/"><img src="https://img.shields.io/badge/Alpine.js-3.13+-77C1D2?logo=alpinedotjs&logoColor=white" alt="Alpine.js"></a>
  <a href="https://www.docker.com/"><img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white" alt="Docker"></a>
  <a href="#-testing--verification"><img src="https://img.shields.io/badge/Tests-31%20Passed-success" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License"></a>
</p>

<p align="center">
  <img src="media/ss/doctor-queue.png" alt="MediCore OPD Doctor Queue & Clinical Workspace" width="94%" />
</p>

---

## 📋 Overview

**MediCore** is a high-performance, modular monolith Hospital Information System (HIS) and Electronic Medical Record (EMR) built for modern healthcare facilities. It pairs desktop-dense, keyboard-friendly clinical interfaces with high development velocity, dynamic schema-driven views, and strict security and domain isolation.

Designed with **FastAPI**, **SQLModel**, **Alembic**, **Jinja2**, **HTMX**, and **Alpine.js**, MediCore operates with zero heavy client-side build steps while providing lightning-fast, reactive SPA-like responsiveness.

---

## 📸 Interface Showcase

<table width="100%">
  <tr>
    <td align="center">
      <strong>🩺 Outpatient (OPD) Doctor Queue & EMR</strong><br><br>
      <img src="media/ss/doctor-queue.png" alt="Doctor Queue & EMR" width="100%" /><br>
      <em>Real-time intake queue, live status counters (Waiting, In Consultation, Completed), and 1-click consultation initiation.</em>
    </td>
  </tr>
  <tr>
    <td align="center">
      <strong>📅 Appointments & Live Queue Control Desk</strong><br><br>
      <img src="media/ss/appointments-queue.png" alt="Appointments & Scheduling Queue Desk" width="100%" /><br>
      <em>Departmental token calling (<code>PED-01</code>, <code>CARD-01</code>), waiting room TV synchronization, and intake management.</em>
    </td>
  </tr>
  <tr>
    <td align="center">
      <strong>👥 Patients Directory & Patient 360 (Dark Mode)</strong><br><br>
      <img src="media/ss/patients-dark.png" alt="Patients Directory & Patient 360 Dark Mode" width="100%" /><br>
      <em>High-density master-detail split pane, multi-field clinical filtering, and Patient 360 history tabs in dark theme.</em>
    </td>
  </tr>
</table>

---

## 🏥 Architecture & Key Innovations

### 1. Plugin-Based Modular Monolith
Each clinical department lives as an autonomous, self-contained package under `medicore/modules/` (e.g., `patients`, `appointments`, `consultations`). The core engine auto-discovers module manifests, database models, API routers, navigation tabs, permissions, command-palette actions, and background jobs upon application boot.

### 2. Zero Cross-Module Direct Imports
Modules never import each other directly. All inter-module communication occurs via asynchronous domain events on the centralized `EventBus` (`event_bus.emit(...)`, `event_bus.subscribe(...)`). For instance, completing an encounter emits `encounter.completed` for billing and pharmacy to react without tight coupling.

### 3. Metadata-Driven UI Engine
Clinical entities are defined as an `EntitySchema` (fields, validations, badges, list columns, filters). Generic Jinja2 + HTMX renderers dynamically generate:
- High-density data tables with multi-field search and status badges
- Real-time filter toolbars
- Split-pane slide-over detail drawers
- Modal creation/editing forms with validation feedback

### 4. Runtime Custom Fields
System administrators can attach custom fields to any entity at runtime (e.g., insurance policy ID, emergency contacts). Stored in schema-less JSON columns, these fields automatically appear in forms, tables, and detail panes without modifying database schemas or running migrations.

### 5. Granular Dynamic RBAC & Audit Trail
Permissions follow the hierarchical `module.entity.action` standard (e.g., `consultations.encounter.write`, `patients.patient.read`). Roles and permissions reside in the database and are enforced across API endpoints, templates, and field-level visibility. An immutable audit trail records all clinical data creation, updates, and accesses with timestamps and user identifiers.

### 6. Design Tokens & Adaptive Theming
Built on CSS variable design tokens (`tokens.css`). Supports **Light** and **Dark** modes as well as **Compact** (desktop-dense) and **Comfortable** table display densities, customizable per user.

---

## 📦 Implemented Clinical Modules

### 🩺 1. OPD Consultations & EMR (`medicore.modules.consultations`)

<p align="center">
  <img src="media/ss/doctor-queue.png" alt="Outpatient Doctor Queue & EMR" width="95%" />
</p>

- **Active Doctor Queue**: Launch consultations directly from checked-in appointments. Automatically shifts status to `In-Consultation` and initiates clinical encounters.
- **Three-Pane Clinical Workspace**:
  1. *Left Pane*: Patient 360 timeline, previous encounters, active allergy flags, and vitals trend sparklines.
  2. *Center Pane*: SOAP note editor with specialty templates (`General`, `Cardiology`, `Pediatrics`), quick text macro expansion (`.htn`, `.dm2`, `.copd`, `.asthma`), drafts autosave, and cryptographic note signing.
  3. *Right Pane*: Orders (Lab & Imaging), ICD-10 diagnoses search, and e-Prescription composer.
- **Vitals Calculation & Sparklines**: Real-time BMI and BSA (Mosteller) auto-computation, out-of-range clinical highlighting (e.g., hypertensive alerts, tachycardia), and trend sparklines across historical visits.
- **Immutable Notes & Addenda**: Signed notes are locked and immutable. Subsequent corrections are appended as audited addenda with physician timestamp and rationale.
- **ICD-10 Diagnostic Search**: Comprehensive ICD-10 clinical dataset with instant autocomplete, primary/secondary diagnosis classification, and clinician favorites.
- **E-Prescribing & Dosage Calculator**: Search by generic or brand name, automatic quantity estimation based on dose, frequency (e.g., `TID`, `QID`, `PRN`), duration, and route. Supports 1-click "Repeat Last Prescription".
- **Pluggable Clinical Safety Rules Engine**:
  - Drug-Allergy cross-checks (e.g., Penicillin allergy vs. Amoxicillin).
  - Duplicate therapy detection.
  - Drug-Drug Interaction (DDI) severity analysis (Major, Moderate, Minor) with bundled interaction datasets.
  - Hard clinical stops requiring physician override reason and audit logging.
- **Lab & Radiology Orders**: Priority ordering (`Routine`, `Urgent`, `STAT`) emitting `order.placed` domain events.
- **Printable & PDF Visit Summaries**: Standardized clinical summary document with hospital branding, physician signature lines, and one-click PDF generation via ReportLab.
- **Patient 360 Clinical Tabs**: Deeply wired **Visits**, **Vitals History**, and **Prescriptions History** tabs directly inside the patient profile.

---

### 📅 2. Appointments & Scheduling (`medicore.modules.appointments`)

<p align="center">
  <img src="media/ss/appointments-queue.png" alt="Appointments Live Queue Desk" width="95%" />
</p>

- **Doctor Schedules**: Weekly working templates, custom exceptions, slot durations, and clinician leave days.
- **Day & Week Calendar**: Drag-and-drop interactive calendar with Day view (hourly slots) and Week view (multi-clinician grid), color-coded by clinical status.
- **Concurrency & Double-Booking Guard**: Database-level unique constraint (`uq_appointment_doctor_time`) plus application-level conflict validation preventing overlapping bookings.
- **Walk-in Intake & Live Tokens**: Express intake issuing sequential departmental tokens (e.g., `CARD-01`, `PED-02`, `GEN-03`).
- **Waiting Room TV Monitor Display**: Full-screen outpatient TV screen at `/appointments/display` with 5-second HTMX auto-refresh, audio cues, and privacy-compliant display (Token + First Name only).
- **Status Lifecycle Flow**: `Booked` ➔ `Checked-in` ➔ `In-Consultation` ➔ `Completed` / `Cancelled` / `No-show`.
- **Automated Reminders**: Background APScheduler job scanning 24–48h upcoming bookings.
- **Wait-Time & No-Show Analytics**: Real-time dashboard widget tracking average wait times and attendance rates.

---

### 👥 3. Patients Directory (`medicore.modules.patients`)

<p align="center">
  <img src="media/ss/patients-dark.png" alt="Patients Directory Dark Mode" width="95%" />
</p>

- **Master-Detail Split-Pane**: Dense patient list with quick-filter toolbar and slide-out patient summary.
- **Server-Enforced Duplicate Detection**: Multi-field matching on save (phone, full name, date of birth) with audible warning, override justification requirements, and audit logging.
- **Sequential MRN Generator**: Automatic generation of hospital medical record numbers (`MC-YYYY-NNNNN`).
- **Allergy & Medical Alert Badges**: Critical allergy banners visible at all stages of care.
- **Full Theme & Density Support**: Seamless Light/Dark theme switching and Compact/Comfortable density settings.

---

## 🚀 Quickstart Guide

### Option A: Running with Docker (Recommended)

MediCore includes a production-ready `Dockerfile` and `docker-compose.yml`:

```bash
# Clone the repository
git clone https://github.com/yaratul2005/MEDICORE.git
cd MEDICORE

# Build and start container (runs migrations and starts uvicorn)
docker compose up --build
```
Open [http://localhost:8000](http://localhost:8000) in your browser.

---

### Option B: Local Setup with Python

#### 1. Prerequisites
- Python 3.12 or 3.13
- [`uv`](https://github.com/astral-sh/uv) (recommended) or standard `pip`

#### 2. Virtual Environment & Installation
```bash
# Create virtual environment
uv venv .venv

# Activate environment
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
uv pip install -e .[dev]
```

#### 3. Database Migrations
Apply Alembic migrations to construct the database schema:
```bash
alembic upgrade head
```

#### 4. Seed Clinical Data
Populate realistic hospital data (200 patients, 5 doctors across 5 departments, 100 appointments, 150 clinical encounters with SOAP notes, vitals, prescriptions, ICD-10 reference data, and default RBAC roles):
```bash
python -m medicore.seed.seeder
```

#### 5. Launch the Application
```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

---

## 🧪 Testing & Verification

MediCore includes a comprehensive test suite covering core infrastructure, scheduling concurrency, duplicate patient prevention, clinical safety rules, and encounter lifecycles:

```bash
uv run --extra dev pytest -v
```

```text
============================= test session starts =============================
medicore/tests/test_appointments.py::test_doctor_slot_generation PASSED        [ 3%]
medicore/tests/test_appointments.py::test_booking_conflict_detection PASSED   [ 6%]
medicore/tests/test_appointments.py::test_status_transitions_and_events PASSED[ 9%]
medicore/tests/test_appointments.py::test_walkin_registration_and_queue_token PASSED [12%]
medicore/tests/test_appointments.py::test_waiting_room_display_endpoints PASSED [16%]
medicore/tests/test_appointments.py::test_reschedule_with_conflict_guard PASSED [19%]
medicore/tests/test_appointments.py::test_appointment_reminders_and_stats PASSED [22%]
medicore/tests/test_appointments.py::test_concurrent_double_booking_protection PASSED [25%]
medicore/tests/test_consultations.py::test_vitals_math_and_clinical_warnings PASSED [29%]
medicore/tests/test_consultations.py::test_soap_text_shortcuts_expansion PASSED [32%]
medicore/tests/test_consultations.py::test_quantity_calculation_from_frequency PASSED [35%]
medicore/tests/test_consultations.py::test_icd10_and_drug_search PASSED    [38%]
medicore/tests/test_consultations.py::test_pluggable_safety_rules_engine PASSED [41%]
medicore/tests/test_consultations.py::test_start_consultation_lifecycle PASSED [45%]
medicore/tests/test_consultations.py::test_soap_note_autosave_signing_and_addenda PASSED [48%]
medicore/tests/test_consultations.py::test_prescription_safety_warning_and_override_enforcement PASSED [51%]
medicore/tests/test_consultations.py::test_order_placement_and_event_emission PASSED [54%]
medicore/tests/test_consultations.py::test_encounter_completion_and_billing_event PASSED [58%]
medicore/tests/test_consultations.py::test_pdf_visit_summary_generation PASSED [61%]
medicore/tests/test_consultations.py::test_patient_360_clinical_tabs_wired PASSED [64%]
medicore/tests/test_core.py::test_config_profiles PASSED                   [67%]
medicore/tests/test_core.py::test_password_hashing PASSED                  [70%]
medicore/tests/test_core.py::test_event_bus PASSED                         [74%]
medicore/tests/test_core.py::test_settings_registry PASSED                 [77%]
medicore/tests/test_core.py::test_module_registry PASSED                   [80%]
medicore/tests/test_patients.py::test_patients_index_page PASSED           [83%]
medicore/tests/test_patients.py::test_patients_table_partial PASSED        [87%]
medicore/tests/test_patient_detail_pane PASSED                           [90%]
medicore/tests/test_duplicate_patient_detection PASSED                   [93%]
medicore/tests/test_create_patient_and_audit_event PASSED                 [96%]
medicore/tests/test_server_enforced_duplicate_prevention_and_override PASSED [100%]
============================== 31 passed in 14.24s ==============================
```

---

## 🌐 Application Navigation & Endpoints

| Endpoint | Description |
| :--- | :--- |
| **`/consultations`** | Active OPD doctor consultation queue, in-progress encounters, clinical stats |
| **`/consultations/encounter/{id}`** | Three-pane OPD clinical workspace (Timeline, SOAP note, Orders & Rx) |
| **`/consultations/encounter/{id}/summary`**| Printable branded clinical visit summary & Rx |
| **`/consultations/encounter/{id}/pdf`** | Downloadable clinical visit summary PDF with hospital header & branding |
| **`/appointments`** | Day/Week calendar, appointments table, queue management desk |
| **`/appointments/display`** | Full-screen waiting room TV display with live 5s auto-refresh |
| **`/patients`** | Master patient directory, Patient 360 (Visits, Vitals, Prescriptions) |
| **`/settings`** | System settings, hospital profile, departments, MRN prefixes |
| **`/audit`** | Comprehensive clinical and administrative audit trail |

---

## 🔑 Default User Accounts

| Username | Password | Role | Description |
| :--- | :--- | :--- | :--- |
| `admin` | `admin123` | **SuperAdmin** | Full system administration, RBAC, system settings, audit logs |
| `dr.sarah` | `doctor123` | **Doctor** | Clinical OPD encounters, SOAP notes, e-prescribing, order placement |
| `nurse.john` | `nurse123` | **Nurse** | Vitals recording, triage assessment, waiting room queue control |
| `receptionist.mary` | `reception123` | **Receptionist** | Front-desk intake, appointment scheduling, walk-in tokens |

---

## 🛠️ Adding New Modules

To add a new clinical or administrative module (e.g., `laboratory`, `pharmacy`, `billing`) in 5 easy steps without modifying core source code, refer to [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 📄 License
This project is licensed under the [MIT License](LICENSE).
