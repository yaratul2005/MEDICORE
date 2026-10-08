import json
from pathlib import Path
import random
from datetime import date, datetime, timedelta, timezone
from faker import Faker
from sqlmodel import Session, col, select
from medicore.core.database import engine, init_db
from medicore.core.models import (
    AuditLog,
    CustomFieldDefinition,
    Permission,
    Role,
    RolePermissionLink,
    User,
    UserRoleLink,
)
from medicore.core.security import hash_password
from medicore.core.settings import settings_registry
from medicore.modules.appointments.models import Appointment, Doctor, QueueToken
from medicore.modules.appointments.service import generate_queue_token
from medicore.modules.consultations.models import (
    ClinicalNote,
    Diagnosis,
    Encounter,
    FollowUp,
    Order,
    Prescription,
    PrescriptionItem,
    Referral,
    Vitals,
)
from medicore.modules.consultations.service import calculate_bmi, calculate_bsa
from medicore.modules.patients.models import Patient
from medicore.modules.pharmacy.models import (
    Batch,
    Dispense,
    DispenseLine,
    GoodsReceipt,
    GoodsReceiptLine,
    Item,
    PharmacyReturn,
    PharmacyReturnLine,
    PharmacyStore,
    PurchaseOrder,
    PurchaseOrderLine,
    StockMovement,
    StockTransferLine,
    StockTransferRequest,
    Supplier,
)
from medicore.modules.pharmacy.service import record_stock_movement

fake = Faker()
Faker.seed(42)
random.seed(42)



def seed_database():
    init_db()
    with Session(engine) as session:
        print("[1/7] Seeding core settings...")
        settings_registry.init_defaults(session)

        # Configure appointment overbooking rules in settings
        settings_registry.set("appointments", "rules", {
            "allow_overbooking": True,
            "max_overbook_per_slot": 1,
            "buffer_minutes_between_slots": 0,
        })
        session.commit()

        print("[2/7] Seeding RBAC roles and permissions...")
        permissions_data = [
            ("patients.patient.read", "patients", "patient", "read", "View patient demographics and records"),
            ("patients.patient.create", "patients", "patient", "create", "Register new patients"),
            ("patients.patient.update", "patients", "patient", "update", "Modify patient details and status"),
            ("patients.patient.delete", "patients", "patient", "delete", "Archive patient records"),
            ("appointments.appointment.read", "appointments", "appointment", "read", "View appointments, doctor schedules, and clinic calendar"),
            ("appointments.appointment.create", "appointments", "appointment", "create", "Book new clinical appointments and issue tokens"),
            ("appointments.appointment.update", "appointments", "appointment", "update", "Reschedule, check-in, and update appointment lifecycle status"),
            ("appointments.appointment.delete", "appointments", "appointment", "delete", "Cancel and remove appointments"),
            ("appointments.queue.manage", "appointments", "queue", "manage", "Call tokens, serve patients, and manage clinic queue"),
            ("appointments.doctor.configure", "appointments", "doctor", "configure", "Configure doctor schedules, slot lengths, exceptions, and leaves"),
            ("consultations.encounter.read", "consultations", "encounter", "read", "View consultation queue and clinical history"),
            ("consultations.encounter.start", "consultations", "encounter", "start", "Initiate OPD clinical encounter from queue"),
            ("consultations.vitals.create", "consultations", "vitals", "create", "Record vital signs and triage parameters"),
            ("consultations.notes.write", "consultations", "note", "write", "Author and edit SOAP clinical notes and addenda"),
            ("consultations.notes.sign", "consultations", "note", "sign", "Sign and lock clinical notes (immutable record)"),
            ("consultations.prescription.write", "consultations", "prescription", "write", "Issue e-prescriptions and override safety alerts"),
            ("consultations.orders.create", "consultations", "orders", "create", "Place laboratory and diagnostic imaging orders"),
            ("consultations.encounter.complete", "consultations", "encounter", "complete", "Finalize clinical encounter and discharge"),
            ("pharmacy.dispense.read", "pharmacy", "dispense", "read", "View pharmacy queue, prescriptions, and stock levels"),
            ("pharmacy.dispense.process", "pharmacy", "dispense", "process", "Dispense prescriptions and counter OTC sales"),
            ("pharmacy.stock.read", "pharmacy", "stock", "read", "View pharmacy inventory and stock ledger"),
            ("pharmacy.stock.manage", "pharmacy", "stock", "manage", "Receive shipments, initiate store transfers, and quarantine stock"),
            ("pharmacy.stock.approve", "pharmacy", "stock", "approve", "Authorize stock adjustments, write-offs, and returns"),
            ("pharmacy.po.create", "pharmacy", "purchase_order", "create", "Draft and submit purchase orders"),
            ("pharmacy.po.manage", "pharmacy", "purchase_order", "manage", "Manage supplier purchase orders and goods receipts"),
            ("pharmacy.reports.read", "pharmacy", "reports", "read", "Access pharmacy reports and analytics"),
            ("core.settings.view", "core", "settings", "view", "View system configuration"),
            ("core.settings.edit", "core", "settings", "edit", "Modify hospital and module settings"),
            ("core.audit.view", "core", "audit", "view", "Inspect clinical audit trail logs"),
        ]

        db_perms = {}
        for code, mod, ent, act, desc in permissions_data:
            p = session.exec(select(Permission).where(Permission.code == code)).first()
            if not p:
                p = Permission(code=code, module=mod, entity=ent, action=act, description=desc)
                session.add(p)
                session.commit()
                session.refresh(p)
            db_perms[code] = p

        roles_config = [
            ("SuperAdmin", "Full access to all modules, settings and audit logs", list(db_perms.keys())),
            ("Doctor", "Clinical practitioner with patient management, notes, prescriptions and schedule access", [
                "patients.patient.read", "patients.patient.create", "patients.patient.update",
                "appointments.appointment.read", "appointments.appointment.update", "core.audit.view",
                "consultations.encounter.read", "consultations.encounter.start", "consultations.vitals.create",
                "consultations.notes.write", "consultations.notes.sign", "consultations.prescription.write",
                "consultations.orders.create", "consultations.encounter.complete"
            ]),
            ("Nurse", "Inpatient and outpatient care nursing staff", [
                "patients.patient.read", "patients.patient.update",
                "appointments.appointment.read", "appointments.queue.manage",
                "consultations.encounter.read", "consultations.vitals.create"
            ]),
            ("Receptionist", "Patient registration, booking and front-desk intake desk", [
                "patients.patient.read", "patients.patient.create",
                "appointments.appointment.read", "appointments.appointment.create",
                "appointments.appointment.update", "appointments.queue.manage",
                "consultations.encounter.read"
            ]),
            ("Pharmacist", "Licensed pharmacy staff dispensing medications and reviewing prescriptions", [
                "pharmacy.dispense.read", "pharmacy.dispense.process", "pharmacy.stock.read", "pharmacy.stock.manage",
                "pharmacy.reports.read", "patients.patient.read", "core.audit.view",
            ]),
            ("StoreKeeper", "Inventory storekeeper managing purchasing, goods receipt, and warehouse transfers", [
                "pharmacy.stock.read", "pharmacy.stock.manage", "pharmacy.po.create", "pharmacy.po.manage",
                "pharmacy.reports.read", "core.audit.view",
            ]),
        ]

        db_roles = {}
        for role_name, desc, perm_codes in roles_config:
            r = session.exec(select(Role).where(Role.name == role_name)).first()
            if not r:
                r = Role(name=role_name, description=desc, is_system=True)
                session.add(r)
                session.commit()
                session.refresh(r)
            db_roles[role_name] = r

            for pcode in perm_codes:
                p = db_perms.get(pcode)
                if p:
                    link = session.exec(
                        select(RolePermissionLink).where(
                            RolePermissionLink.role_id == r.id,
                            RolePermissionLink.permission_id == p.id,
                        )
                    ).first()
                    if not link:
                        session.add(RolePermissionLink(role_id=r.id, permission_id=p.id))
        session.commit()

        print("[3/7] Seeding clinical users...")
        users_data = [
            ("admin", "admin@medicore.health", "System Administrator", "admin123", True, "SuperAdmin"),
            ("dr.sarah", "sarah.chen@medicore.health", "Dr. Sarah Chen, MD", "doctor123", False, "Doctor"),
            ("receptionist.mary", "mary.jones@medicore.health", "Mary Jones (Receptionist)", "reception123", False, "Receptionist"),
            ("nurse.john", "john.miller@medicore.health", "Nurse John Miller, RN", "nurse123", False, "Nurse"),
            ("pharmacist.lisa", "lisa.wong@medicore.health", "Lisa Wong, PharmD", "pharmacy123", False, "Pharmacist"),
            ("storekeeper.dan", "dan.miller@medicore.health", "Dan Miller (Storekeeper)", "store123", False, "StoreKeeper"),
        ]

        seeded_users = {}
        for uname, email, fname, pwd, is_super, rname in users_data:
            u = session.exec(select(User).where(User.username == uname)).first()
            if not u:
                u = User(
                    username=uname,
                    email=email,
                    full_name=fname,
                    hashed_password=hash_password(pwd),
                    is_superuser=is_super,
                    preferences={"theme": "light", "density": "compact"},
                )
                session.add(u)
                session.commit()
                session.refresh(u)

                role = db_roles.get(rname)
                if role:
                    session.add(UserRoleLink(user_id=u.id, role_id=role.id))
                    session.commit()
            seeded_users[uname] = u

        print("[4/7] Seeding custom field definitions...")
        custom_fields = [
            ("patient", "insurance_provider", "Insurance Provider", "select", ["Blue Cross Blue Shield", "Aetna", "Medicare", "Cigna", "UnitedHealth", "Self-Pay"], False, 1),
            ("patient", "primary_language", "Primary Language", "select", ["English", "Spanish", "French", "Mandarin", "Arabic", "Bengali"], False, 2),
            ("patient", "vip_status", "VIP / High Priority", "boolean", [], False, 3),
        ]

        for ent, fname, lbl, ftype, opts, req, order in custom_fields:
            existing = session.exec(
                select(CustomFieldDefinition).where(
                    CustomFieldDefinition.entity_name == ent,
                    CustomFieldDefinition.field_name == fname,
                )
            ).first()
            if not existing:
                cf = CustomFieldDefinition(
                    entity_name=ent,
                    field_name=fname,
                    label=lbl,
                    field_type=ftype,
                    options=opts,
                    is_required=req,
                    display_order=order,
                )
                session.add(cf)
        session.commit()

        print("[5/7] Seeding 200 realistic patient records...")
        current_patients = session.exec(select(Patient)).all()
        current_patients_count = len(current_patients)
        needed = 200 - current_patients_count

        if needed > 0:
            blood_groups = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
            statuses = ["Active", "Active", "Active", "Inpatient", "Critical", "Discharged"]
            allergies_list = [
                None, None, None,
                "Penicillin", "Sulfa drugs", "Latex", "Aspirin & NSAIDs", "Peanuts",
                "Contrast Dye / Iodine", "Amoxicillin", "Ciprofloxacin"
            ]
            insurances = ["Blue Cross Blue Shield", "Aetna", "Medicare", "Cigna", "UnitedHealth", "Self-Pay"]
            languages = ["English", "Spanish", "French", "Mandarin", "Arabic", "Bengali"]

            year = datetime.now(timezone.utc).year
            admin_user = seeded_users.get("admin")

            for i in range(1, needed + 1):
                seq = current_patients_count + i
                mrn = f"MC-{year}-{seq:05d}"
                gender = random.choice(["Male", "Female", "Other"])
                first_name = fake.first_name_male() if gender == "Male" else fake.first_name_female()
                last_name = fake.last_name()
                
                age_days = random.randint(365 * 3, 365 * 80)
                dob = (date.today() - timedelta(days=age_days)).strftime("%Y-%m-%d")

                p = Patient(
                    mrn=mrn,
                    first_name=first_name,
                    last_name=last_name,
                    date_of_birth=dob,
                    gender=gender,
                    blood_group=random.choice(blood_groups),
                    phone=fake.phone_number(),
                    email=f"{first_name.lower()}.{last_name.lower()}@{fake.free_email_domain()}",
                    address=f"{fake.street_address()}, {fake.city()}, {fake.state_abbr()} {fake.zipcode()}",
                    emergency_contact_name=f"{fake.first_name()} {last_name}",
                    emergency_contact_phone=fake.phone_number(),
                    allergies=random.choice(allergies_list),
                    status=random.choice(statuses),
                    custom_fields={
                        "insurance_provider": random.choice(insurances),
                        "primary_language": random.choice(languages),
                        "vip_status": random.random() < 0.1,
                    }
                )
                session.add(p)
                session.commit()
                session.refresh(p)

                audit = AuditLog(
                    timestamp=datetime.now(timezone.utc) - timedelta(days=random.randint(1, 60)),
                    user_id=admin_user.id if admin_user else 1,
                    username="admin",
                    module="patients",
                    entity="patient",
                    entity_id=str(p.id),
                    action="CREATE",
                    changes={"mrn": p.mrn, "name": p.full_name, "status": p.status},
                )
                session.add(audit)

            mrn_data = dict(settings_registry.get("numbering", "mrn", {}) or {})
            mrn_data["current_sequence"] = 200
            settings_registry.set("numbering", "mrn", mrn_data)
            session.commit()

        # Re-fetch all patients for appointment linking
        all_patients = session.exec(select(Patient)).all()

        print("[6/7] Seeding 5 doctors with schedules & exceptions...")
        standard_schedule = {
            "Monday": [["09:00", "12:00"], ["14:00", "17:00"]],
            "Tuesday": [["09:00", "12:00"], ["14:00", "17:00"]],
            "Wednesday": [["09:00", "12:00"], ["14:00", "17:00"]],
            "Thursday": [["09:00", "12:00"], ["14:00", "17:00"]],
            "Friday": [["09:00", "12:00"], ["14:00", "16:00"]],
            "Saturday": [["10:00", "13:00"]],
        }

        today_dt = datetime.now(timezone.utc).date()
        leave_day = (today_dt + timedelta(days=12)).strftime("%Y-%m-%d")

        doctors_data = [
            ("Dr. Sarah Chen, MD", "Cardiology", "Interventional Cardiology", "sarah.chen@medicore.health", "+1 (555) 234-5678", "Room 101", 20, seeded_users.get("dr.sarah")),
            ("Dr. Marcus Vance, MD", "Neurology", "Cognitive & Stroke Neurology", "marcus.vance@medicore.health", "+1 (555) 345-6789", "Room 204", 30, None),
            ("Dr. Emily Rodriguez, MD", "Pediatrics", "Pediatric Critical Care", "emily.rodriguez@medicore.health", "+1 (555) 456-7890", "Room 108", 15, None),
            ("Dr. James Wilson, MD", "Orthopedics", "Joint Replacement & Trauma", "james.wilson@medicore.health", "+1 (555) 567-8901", "Room 302", 20, None),
            ("Dr. Aisha Patel, MD", "General Medicine", "Internal Medicine & Prevention", "aisha.patel@medicore.health", "+1 (555) 678-9012", "Room 105", 20, None),
        ]

        seeded_doctors = []
        for name, dept, spec, email, phone, room, slot_len, u_link in doctors_data:
            doc = session.exec(select(Doctor).where(Doctor.name == name)).first()
            if not doc:
                doc = Doctor(
                    name=name,
                    department=dept,
                    specialty=spec,
                    email=email,
                    phone=phone,
                    room_number=room,
                    slot_length=slot_len,
                    schedule_templates=standard_schedule,
                    exceptions={},
                    leave=[leave_day] if dept == "Orthopedics" else [],
                    user_id=u_link.id if u_link else None,
                    is_active=True,
                )
                session.add(doc)
                session.commit()
                session.refresh(doc)
            seeded_doctors.append(doc)

        print("[7/7] Seeding 100 realistic appointments across past, today, and future...")
        current_appts_count = len(session.exec(select(Appointment)).all())
        needed_appts = 100 - current_appts_count

        if needed_appts > 0:
            types = ["Consultation", "Follow-up", "Routine Checkup", "Walk-in", "Emergency"]
            sources = ["Reception", "Phone", "Online", "Walk-in", "Referral"]

            # 1. Past appointments (35 appointments): Completed, No-show, Cancelled
            past_statuses = ["Completed", "Completed", "Completed", "Completed", "No-show", "Cancelled"]
            for i in range(35):
                patient = random.choice(all_patients)
                doctor = random.choice(seeded_doctors)
                days_ago = random.randint(1, 14)
                appt_date = today_dt - timedelta(days=days_ago)
                date_str = appt_date.strftime("%Y-%m-%d")

                h = random.choice([9, 10, 11, 14, 15, 16])
                m = random.choice([0, 20, 40]) if doctor.slot_length == 20 else random.choice([0, 30])
                s_time = f"{h:02d}:{m:02d}"
                tot = h * 60 + m + doctor.slot_length
                e_time = f"{(tot // 60):02d}:{(tot % 60):02d}"

                st = random.choice(past_statuses)
                start_dt = datetime.strptime(f"{date_str} {s_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                end_dt = datetime.strptime(f"{date_str} {e_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

                checked_in_at = None
                consult_at = None
                completed_at = None

                if st == "Completed":
                    checked_in_at = start_dt - timedelta(minutes=random.randint(5, 25))
                    consult_at = start_dt + timedelta(minutes=random.randint(0, 15))
                    completed_at = consult_at + timedelta(minutes=doctor.slot_length)
                elif st == "No-show":
                    pass

                appt = Appointment(
                    patient_id=patient.id,
                    patient_name=patient.full_name,
                    patient_mrn=patient.mrn,
                    patient_phone=patient.phone,
                    doctor_id=doctor.id,
                    doctor_name=doctor.name,
                    department=doctor.department,
                    appointment_date=date_str,
                    start_time=s_time,
                    end_time=e_time,
                    start_datetime=start_dt,
                    end_datetime=end_dt,
                    type=random.choice(types),
                    status=st,
                    source=random.choice(sources),
                    notes="Patient presented with standard clinical indications.",
                    checked_in_at=checked_in_at,
                    consultation_started_at=consult_at,
                    completed_at=completed_at,
                    reminder_sent=True,
                )
                session.add(appt)

            session.commit()

            # 2. Today's appointments (30 appointments): Booked, Checked-in, In-Consultation, Completed
            today_str = today_dt.strftime("%Y-%m-%d")
            today_statuses = ["Booked", "Checked-in", "In-Consultation", "Completed"]

            # Distribute 6 appointments per doctor today across different time slots
            time_offsets = [
                ("09:00", "09:20"), ("09:40", "10:00"), ("10:20", "10:40"),
                ("11:00", "11:20"), ("14:00", "14:20"), ("15:00", "15:20")
            ]

            for doc in seeded_doctors:
                for idx, (s_time, e_time) in enumerate(time_offsets):
                    patient = random.choice(all_patients)
                    st = today_statuses[idx % len(today_statuses)]

                    start_dt = datetime.strptime(f"{today_str} {s_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                    end_dt = datetime.strptime(f"{today_str} {e_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

                    checked_in_at = None
                    consult_at = None
                    completed_at = None

                    if st in ["Checked-in", "In-Consultation", "Completed"]:
                        checked_in_at = start_dt - timedelta(minutes=random.randint(5, 20))
                    if st in ["In-Consultation", "Completed"]:
                        consult_at = start_dt + timedelta(minutes=random.randint(0, 10))
                    if st == "Completed":
                        completed_at = consult_at + timedelta(minutes=doc.slot_length)

                    appt = Appointment(
                        patient_id=patient.id,
                        patient_name=patient.full_name,
                        patient_mrn=patient.mrn,
                        patient_phone=patient.phone,
                        doctor_id=doc.id,
                        doctor_name=doc.name,
                        department=doc.department,
                        appointment_date=today_str,
                        start_time=s_time,
                        end_time=e_time,
                        start_datetime=start_dt,
                        end_datetime=end_dt,
                        type="Consultation" if idx % 2 == 0 else "Walk-in",
                        status=st,
                        source="Reception" if idx % 2 == 0 else "Walk-in",
                        notes=f"Today consultation with {doc.name}.",
                        checked_in_at=checked_in_at,
                        consultation_started_at=consult_at,
                        completed_at=completed_at,
                        reminder_sent=True,
                    )
                    session.add(appt)
                    session.commit()
                    session.refresh(appt)

                    # Create QueueToken for today's active appointments
                    if st in ["Checked-in", "In-Consultation", "Completed"]:
                        t_status = "Waiting"
                        if st == "In-Consultation":
                            t_status = "Serving"
                        elif st == "Completed":
                            t_status = "Done"

                        token = generate_queue_token(
                            doctor=doc,
                            patient_id=patient.id,
                            patient_name=patient.full_name,
                            patient_mrn=patient.mrn,
                            appointment_id=appt.id,
                            session=session,
                            is_walk_in=(appt.type == "Walk-in"),
                        )
                        token.status = t_status
                        session.add(token)

            session.commit()

            # 3. Future appointments (35 appointments): Booked for upcoming 1-7 days
            for i in range(35):
                patient = random.choice(all_patients)
                doctor = random.choice(seeded_doctors)
                days_ahead = random.randint(1, 7)
                appt_date = today_dt + timedelta(days=days_ahead)
                date_str = appt_date.strftime("%Y-%m-%d")

                h = random.choice([9, 10, 11, 14, 15, 16])
                m = random.choice([0, 20, 40]) if doctor.slot_length == 20 else random.choice([0, 30])
                s_time = f"{h:02d}:{m:02d}"
                tot = h * 60 + m + doctor.slot_length
                e_time = f"{(tot // 60):02d}:{(tot % 60):02d}"

                start_dt = datetime.strptime(f"{date_str} {s_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                end_dt = datetime.strptime(f"{date_str} {e_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

                appt = Appointment(
                    patient_id=patient.id,
                    patient_name=patient.full_name,
                    patient_mrn=patient.mrn,
                    patient_phone=patient.phone,
                    doctor_id=doctor.id,
                    doctor_name=doctor.name,
                    department=doctor.department,
                    appointment_date=date_str,
                    start_time=s_time,
                    end_time=e_time,
                    start_datetime=start_dt,
                    end_datetime=end_dt,
                    type=random.choice(types),
                    status="Booked",
                    source=random.choice(sources),
                    notes="Upcoming scheduled consultation.",
                    reminder_sent=False,
                )
                session.add(appt)

            session.commit()

        print("[8/8] Seeding 150 realistic clinical encounters with vitals, SOAP notes, and prescriptions...")
        current_encs = session.exec(select(Encounter)).all()
        needed_encs = 150 - len(current_encs)

        if needed_encs > 0:
            clinical_profiles = [
                {
                    "department": "General Medicine",
                    "complaint": "Persistent productive cough, nasal congestion, and low-grade fever for 4 days.",
                    "primary_dx": ("J06.9", "Acute upper respiratory infection, unspecified"),
                    "secondary_dx": [("R50.9", "Fever, unspecified")],
                    "subjective": "Patient presents with a 4-day history of rhinorrhea, scratchy throat, and productive cough with clear phlegm. Reports low-grade fever responsive to paracetamol. Denies shortness of breath, pleuritic chest pain, or hemoptysis.",
                    "objective": "T: 37.8°C, HR: 82, BP: 122/78, SpO2: 98% room air. Pharynx: Mild erythematous mucosa without exudates. Neck: Supple, non-tender cervical lymphadenopathy. Lungs: Clear to auscultation bilaterally, no crackles or wheezes. Heart: Normal S1/S2.",
                    "assessment": "1. Acute viral upper respiratory tract infection.\n2. Hydration and clinical status adequate.",
                    "plan": "1. Supportive oral hydration and rest.\n2. Paracetamol 500mg TID PRN for fever/body aches.\n3. Cetirizine 10mg OD QHS for 7 days.\n4. Advised return if high fevers (>39°C) or respiratory distress develop.",
                    "drugs": [
                        {"name": "Tylenol / Panadol", "generic": "Paracetamol", "dosage": "500 mg", "freq": "TID", "route": "Oral", "days": 5, "qty": 15, "inst": "Take after food as needed for fever or pain."},
                        {"name": "Zyrtec", "generic": "Cetirizine", "dosage": "10 mg", "freq": "OD", "route": "Oral", "days": 7, "qty": 7, "inst": "Take once daily at bedtime."}
                    ],
                    "orders": [("Lab", "Complete Blood Count (CBC)", "Routine")],
                },
                {
                    "department": "Cardiology",
                    "complaint": "Routine follow-up of hypertension and dyslipidemia; mild exertional dyspnea.",
                    "primary_dx": ("I10", "Essential (primary) hypertension"),
                    "secondary_dx": [("E78.5", "Hyperlipidemia, unspecified")],
                    "subjective": "56-year-old patient presents for quarterly cardiovascular checkup. Reports compliant medication intake. Home BP logs range between 135-148 systolic. Denies rest chest pain, orthopnea, or ankle swelling.",
                    "objective": "BP: 142/88 mmHg, HR: 74 regular, SpO2: 98%. JVP normal. Heart: Regular rate and rhythm, normal S1 and S2, no third heart sound or systolic murmur. Lungs: CTA bilaterally. Extremities: Peripheral pulses intact, no edema.",
                    "assessment": "1. Essential hypertension, stage 1 - suboptimally controlled on monotherapy.\n2. Hyperlipidemia on statin therapy.",
                    "plan": "1. Optimize antihypertensive regimen with Amlodipine addition.\n2. Continue Atorvastatin 20mg at bedtime.\n3. Low sodium DASH diet (<2g/day) and daily 30-minute aerobic walk.\n4. Repeat lipid panel and comprehensive metabolic panel in 4 weeks.",
                    "drugs": [
                        {"name": "Zestril", "generic": "Lisinopril", "dosage": "10 mg", "freq": "OD", "route": "Oral", "days": 30, "qty": 30, "inst": "Take once daily in the morning."},
                        {"name": "Norvasc", "generic": "Amlodipine", "dosage": "5 mg", "freq": "OD", "route": "Oral", "days": 30, "qty": 30, "inst": "Take once daily in the morning."},
                        {"name": "Lipitor", "generic": "Atorvastatin", "dosage": "20 mg", "freq": "QHS", "route": "Oral", "days": 30, "qty": 30, "inst": "Take at bedtime."}
                    ],
                    "orders": [("Lab", "Fasting Lipid Panel", "Routine"), ("Lab", "Comprehensive Metabolic Panel (CMP)", "Routine"), ("Imaging", "12-Lead Electrocardiogram (ECG)", "Routine")],
                },
                {
                    "department": "Endocrinology",
                    "complaint": "Type 2 Diabetes Mellitus review; elevated fasting blood sugars.",
                    "primary_dx": ("E11.9", "Type 2 diabetes mellitus without complications"),
                    "secondary_dx": [("E66.9", "Obesity, unspecified")],
                    "subjective": "Presents for diabetic metabolic review. Reports fasting blood sugars averaging 140-165 mg/dL. Compliant with Metformin. Denies polyuria, polydipsia, blurred vision, or foot numbness.",
                    "objective": "BMI: 31.4 kg/m². BP: 128/80 mmHg. Eyes: Visual acuity preserved. Feet: Monofilament testing intact bilaterally, pedal pulses palpable, no active ulcerations or calluses.",
                    "assessment": "1. Type 2 diabetes mellitus with suboptimal glycemic control.\n2. Grade 1 obesity.",
                    "plan": "1. Titrate Metformin to 850mg BID with meals.\n2. Order HbA1c, urine microalbumin-to-creatinine ratio.\n3. Referral to certified diabetes educator / clinical nutritionist.\n4. Routine eye examination scheduled.",
                    "drugs": [
                        {"name": "Glucophage", "generic": "Metformin", "dosage": "850 mg", "freq": "BID", "route": "Oral", "days": 30, "qty": 60, "inst": "Take with breakfast and dinner."}
                    ],
                    "orders": [("Lab", "Glycated Hemoglobin (HbA1c)", "Routine"), ("Lab", "Microalbumin / Creatinine Ratio", "Routine")],
                },
                {
                    "department": "Gastroenterology",
                    "complaint": "Postprandial heartburn, acid regurgitation, and epigastric discomfort.",
                    "primary_dx": ("K21.9", "Gastro-esophageal reflux disease without esophagitis"),
                    "secondary_dx": [("K29.70", "Gastritis, unspecified, without bleeding")],
                    "subjective": "Reports burning retrosternal sensation worsening after spicy meals and when lying down flat. No dysphagia, odynophagia, unintentional weight loss, or black tarry stools.",
                    "objective": "Abdomen: Flat, soft, mild tenderness to palpation in epigastric region without guarding or rebound. Bowel sounds normoactive in all four quadrants. Liver and spleen non-palpable.",
                    "assessment": "1. Gastroesophageal reflux disease (GERD) with mild non-ulcer dyspepsia.",
                    "plan": "1. Omeprazole 20mg delayed-release capsule 30 min before breakfast for 30 days.\n2. Elevate head of bed, avoid late-night eating, caffeine, and acidic foods.\n3. Follow up if symptoms refractory to PPI therapy.",
                    "drugs": [
                        {"name": "Prilosec", "generic": "Omeprazole", "dosage": "20 mg", "freq": "OD", "route": "Oral", "days": 30, "qty": 30, "inst": "Take 30 minutes before the first meal of the day."}
                    ],
                    "orders": [("Lab", "H. pylori Stool Antigen Test", "Routine")],
                },
                {
                    "department": "Orthopedics",
                    "complaint": "Acute lower back strain after lifting heavy furniture; stiffness upon waking.",
                    "primary_dx": ("M54.5", "Low back pain, unspecified"),
                    "secondary_dx": [("M79.1", "Myalgia")],
                    "subjective": "Patient developed sudden lower back ache 2 days ago after lifting heavy boxes. Pain is dull and aching, rated 6/10. Aggravated by bending. Denies radiating sciatica down legs, numbness, tingling, or urinary incontinence.",
                    "objective": "Gait: Normal, slightly guarded. Spine: Lumbar lordosis maintained, paraspinal muscle spasm noted over L3-L5 region. Straight Leg Raise negative bilaterally at 80 degrees. Deep tendon reflexes 2+ and symmetric.",
                    "assessment": "1. Acute mechanical lumbar muscle strain.\n2. Absence of radiculopathy or red flag symptoms.",
                    "plan": "1. Short-course Ibuprofen 400mg TID with food for 7 days.\n2. Heat therapy, gentle mobilization, avoid heavy lifting.\n3. Physical therapy if pain persists beyond 2 weeks.",
                    "drugs": [
                        {"name": "Advil / Motrin", "generic": "Ibuprofen", "dosage": "400 mg", "freq": "TID", "route": "Oral", "days": 7, "qty": 21, "inst": "Take after meals with food."}
                    ],
                    "orders": [("Imaging", "Lumbar Spine X-Ray (AP & Lateral)", "Routine")],
                },
                {
                    "department": "Pulmonology",
                    "complaint": "Episodic wheezing and night cough triggered by seasonal changes.",
                    "primary_dx": ("J45.909", "Unspecified asthma, uncomplicated"),
                    "secondary_dx": [],
                    "subjective": "Known asthmatic presents with increasing daytime cough and nocturnal wheezing 3 times this week. Uses rescue inhaler intermittently. Denies fever or purulent sputum.",
                    "objective": "Respiratory rate 18/min, SpO2 97% on room air. Chest: Bilateral expiratory wheezes, no prolonged expiratory phase or accessory muscle use. Heart: Normal heart sounds.",
                    "assessment": "1. Mild persistent bronchial asthma with seasonal exacerbation.",
                    "plan": "1. Continue Salbutamol inhaler as needed for acute relief.\n2. Technique review for metered dose inhaler.\n3. Peak flow monitoring at home.",
                    "drugs": [
                        {"name": "Ventolin", "generic": "Salbutamol", "dosage": "100 mcg/actuation", "freq": "PRN", "route": "Inhalation", "days": 30, "qty": 1, "inst": "1-2 puffs inhaled as needed for wheezing or tightness."}
                    ],
                    "orders": [("Imaging", "Chest Radiograph (PA View)", "Routine")],
                }
            ]

            completed_appts = session.exec(
                select(Appointment).where(Appointment.status == "Completed").order_by(Appointment.id)
            ).all()

            for i in range(needed_encs):
                profile = clinical_profiles[i % len(clinical_profiles)]
                patient = all_patients[i % len(all_patients)]
                doctor = seeded_doctors[i % len(seeded_doctors)]

                linked_appt = completed_appts[i] if i < len(completed_appts) else None

                days_ago = random.randint(0, 30)
                enc_date = today_dt - timedelta(days=days_ago)
                hour = random.choice([9, 10, 11, 14, 15, 16])
                minute = random.choice([0, 15, 30, 45])
                started_dt = datetime.combine(enc_date, datetime.min.time()).replace(hour=hour, minute=minute, tzinfo=timezone.utc)

                if i < 8:
                    enc_status = "In-Progress"
                    started_dt = datetime.combine(today_dt, datetime.min.time()).replace(hour=random.choice([10, 11, 14]), minute=random.choice([10, 25, 40]), tzinfo=timezone.utc)
                    completed_dt = None
                elif i < 10:
                    enc_status = "Cancelled"
                    completed_dt = None
                else:
                    enc_status = "Completed"
                    completed_dt = started_dt + timedelta(minutes=random.randint(15, 30))

                encounter = Encounter(
                    patient_id=patient.id,
                    patient_name=patient.full_name,
                    patient_mrn=patient.mrn,
                    doctor_id=doctor.id,
                    doctor_name=doctor.name,
                    appointment_id=linked_appt.id if linked_appt else None,
                    status=enc_status,
                    department=doctor.department or profile["department"],
                    chief_complaint=profile["complaint"],
                    started_at=started_dt,
                    completed_at=completed_dt,
                )
                session.add(encounter)
                session.commit()
                session.refresh(encounter)

                # 1. Vitals
                h_cm = random.choice([155.0, 162.0, 168.0, 174.0, 180.0, 185.0])
                w_kg = random.choice([54.0, 63.0, 71.0, 78.0, 84.0, 92.0])
                bmi = calculate_bmi(w_kg, h_cm)
                bsa = calculate_bsa(w_kg, h_cm)
                sys = random.choice([116, 122, 128, 134, 142, 148])
                dia = random.choice([74, 78, 82, 86, 90, 94])
                pulse = random.choice([66, 72, 76, 82, 88])
                temp = random.choice([36.6, 36.8, 37.1, 37.5, 37.9])
                spo2 = random.choice([96.0, 97.0, 98.0, 99.0])
                rr = random.choice([14, 16, 18])

                v = Vitals(
                    encounter_id=encounter.id,
                    patient_id=patient.id,
                    recorded_at=started_dt,
                    recorded_by="Nurse John Miller, RN",
                    height_cm=h_cm,
                    weight_kg=w_kg,
                    bmi=bmi,
                    bsa=bsa,
                    systolic=sys,
                    diastolic=dia,
                    pulse=pulse,
                    temp_c=temp,
                    spo2=spo2,
                    resp_rate=rr,
                    notes="Baseline intake vital signs.",
                )
                session.add(v)

                # 2. Clinical Note (SOAP)
                is_signed = (enc_status == "Completed")
                note_addenda = []
                if is_signed and i % 5 == 0:
                    note_addenda.append({
                        "author": doctor.name,
                        "timestamp": (completed_dt + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S") if completed_dt else "",
                        "text": "Addendum: Diagnostic lab work reviewed. Patient tolerating initial therapy well."
                    })

                note = ClinicalNote(
                    encounter_id=encounter.id,
                    specialty=doctor.department or profile["department"],
                    subjective=profile["subjective"],
                    objective=profile["objective"],
                    assessment=profile["assessment"],
                    plan=profile["plan"],
                    is_signed=is_signed,
                    signed_at=completed_dt if is_signed else None,
                    signed_by=doctor.name if is_signed else None,
                    addenda=note_addenda,
                )
                session.add(note)

                # 3. Diagnoses
                p_code, p_desc = profile["primary_dx"]
                dx_p = Diagnosis(
                    encounter_id=encounter.id,
                    patient_id=patient.id,
                    icd10_code=p_code,
                    description=p_desc,
                    is_primary=True,
                )
                session.add(dx_p)

                for s_code, s_desc in profile["secondary_dx"]:
                    dx_s = Diagnosis(
                        encounter_id=encounter.id,
                        patient_id=patient.id,
                        icd10_code=s_code,
                        description=s_desc,
                        is_primary=False,
                    )
                    session.add(dx_s)

                # 4. Prescription & Items
                rx = Prescription(
                    encounter_id=encounter.id,
                    patient_id=patient.id,
                    doctor_id=doctor.id,
                    doctor_name=doctor.name,
                    prescribed_at=started_dt,
                    status="Active",
                )
                session.add(rx)
                session.commit()
                session.refresh(rx)

                for med in profile["drugs"]:
                    item = PrescriptionItem(
                        prescription_id=rx.id,
                        drug_name=med["name"],
                        generic_name=med["generic"],
                        dosage=med["dosage"],
                        frequency=med["freq"],
                        route=med["route"],
                        duration_days=med["days"],
                        quantity=med["qty"],
                        instructions=med["inst"],
                    )
                    session.add(item)

                # 5. Diagnostic Orders
                if i % 2 == 0 and profile.get("orders"):
                    for o_type, o_test, o_prio in profile["orders"]:
                        ord_record = Order(
                            encounter_id=encounter.id,
                            patient_id=patient.id,
                            doctor_id=doctor.id,
                            type=o_type,
                            test_name=o_test,
                            priority=o_prio,
                            status="Completed" if enc_status == "Completed" else "Placed",
                            placed_at=started_dt,
                        )
                        session.add(ord_record)

                # 6. Follow-up
                if i % 3 != 0:
                    fu = FollowUp(
                        encounter_id=encounter.id,
                        patient_id=patient.id,
                        doctor_id=doctor.id,
                        recommended_date=(enc_date + timedelta(days=14)),
                        notes="Routine follow-up for symptom response and medication tolerance.",
                    )
                    session.add(fu)

            session.commit()

        # Step 9: Seed Pharmacy
        seed_pharmacy_data(session)

        total_appts = len(session.exec(select(Appointment)).all())
        total_tokens = len(session.exec(select(QueueToken)).all())
        total_encounters = len(session.exec(select(Encounter)).all())
        total_items = len(session.exec(select(Item)).all())
        total_batches = len(session.exec(select(Batch)).all())
        total_dispenses = len(session.exec(select(Dispense)).all())
        print(f"Seeding completed successfully! 200 patients, 5 doctors, {total_appts} appointments, {total_tokens} queue tokens, {total_encounters} encounters, {total_items} pharmacy items, {total_batches} batches, and {total_dispenses} dispenses ready.")


def seed_pharmacy_data(session: Session):
    print("[9/9] Seeding Pharmacy module (stores, suppliers, 300 items, batches, ledger, dispenses)...")

    # 1. Stores
    store_defs = [
        ("MAIN", "Central Main Pharmacy", True, "Ground Floor, Central Wing"),
        ("WARD", "Inpatient Ward Sub-Store", False, "Level 2, Nursing Station A"),
        ("OT", "Operating Theater Satellite Store", False, "Level 3, Surgical Suite"),
        ("ER", "Emergency Trauma Sub-Store", False, "Ground Floor, Acute Care"),
    ]
    db_stores = {}
    for code, name, is_main, loc in store_defs:
        st = session.exec(select(PharmacyStore).where(PharmacyStore.code == code)).first()
        if not st:
            st = PharmacyStore(code=code, name=name, is_main=is_main, location=loc)
            session.add(st)
            session.commit()
            session.refresh(st)
        db_stores[code] = st

    main_store = db_stores["MAIN"]

    # 2. Suppliers
    supplier_defs = [
        ("SUP-001", "Apex BioPharm Distributors", "John Apex", "orders@apexbiopharm.com", "+1-555-0101", "742 Evergreen Terrace, Springfield", "Net 30", 4.9),
        ("SUP-002", "Novis Healthcare Supplies", "Sarah Novis", "contact@novishealth.com", "+1-555-0102", "100 Medical Center Blvd, Chicago", "Net 45", 4.7),
        ("SUP-003", "Global MedTech Pharma", "Marcus Vance", "supply@globalmedtech.com", "+1-555-0103", "500 Pharma Way, Boston", "Net 30", 4.8),
        ("SUP-004", "Lifeline Generics Ltd.", "Elena Rostova", "sales@lifelinegenerics.com", "+1-555-0104", "12 BioPark Ave, Durham", "Net 60", 4.5),
        ("SUP-005", "Vanguard Therapeutics", "David Chen", "b2b@vanguardrx.com", "+1-555-0105", "88 Horizon Blvd, Cambridge", "Net 30", 4.9),
    ]
    db_suppliers = {}
    for scode, sname, cpers, email, phone, addr, terms, rating in supplier_defs:
        sup = session.exec(select(Supplier).where(Supplier.code == scode)).first()
        if not sup:
            sup = Supplier(
                code=scode, name=sname, contact_person=cpers, email=email,
                phone=phone, address=addr, payment_terms=terms, rating=rating
            )
            session.add(sup)
            session.commit()
            session.refresh(sup)
        db_suppliers[scode] = sup

    # 3. 300 Items from items.json
    items_path = Path(__file__).parent.parent / "modules" / "pharmacy" / "data" / "items.json"
    if items_path.exists():
        with open(items_path, "r", encoding="utf-8") as f:
            items_raw = json.load(f)
    else:
        items_raw = []

    db_items = {it.code: it for it in session.exec(select(Item)).all()}
    for row in items_raw:
        code = row["code"]
        if code not in db_items:
            item = Item(
                code=code,
                name=row["name"],
                generic_name=row["generic_name"],
                brand_name=row.get("brand_name"),
                category=row["category"],
                form=row.get("form", "Tablet"),
                strength=row.get("strength", ""),
                unit=row.get("unit", "Tablet"),
                cost_price=float(row.get("cost_price", 0.0)),
                unit_price=float(row.get("unit_price", 0.0)),
                reorder_level=int(row.get("reorder_level", 50)),
                reorder_quantity=int(row.get("reorder_quantity", 150)),
                is_prescription_required=bool(row.get("is_prescription_required", True)),
                is_active=bool(row.get("is_active", True)),
            )
            session.add(item)
            session.flush()
            db_items[code] = item
    session.commit()

    all_items = list(db_items.values())

    # 4. Batches & Stock Movements (Ledger)
    today = date.today()
    existing_batches = session.exec(select(Batch)).all()
    if not existing_batches:
        print("  - Seeding initial batches and immutable stock movements...")
        for idx, it in enumerate(all_items):
            # Batch in Main Store
            lot_main = f"LOT-2026-{idx+1:04d}A"
            if idx in [25, 45, 65, 85, 105]:
                # Expired batches
                exp_date = today - timedelta(days=random.randint(15, 60))
                qty_rec = random.randint(30, 80)
                quarantined = (idx in [25, 65])
                q_reason = f"Automated quarantine: Expired on {exp_date}" if quarantined else None
            elif idx in [10, 20, 30, 40, 50, 70, 90, 110, 130, 150]:
                # Near-expiry batches (< 60 days)
                exp_date = today + timedelta(days=random.randint(10, 45))
                qty_rec = random.randint(40, 100)
                quarantined = False
                q_reason = None
            elif idx % 23 == 0:
                # Low stock item
                exp_date = today + timedelta(days=random.randint(200, 500))
                qty_rec = random.randint(5, 15)  # Below reorder level
                quarantined = False
                q_reason = None
            else:
                # Normal healthy batch
                exp_date = today + timedelta(days=random.randint(180, 700))
                qty_rec = random.randint(150, 400)
                quarantined = False
                q_reason = None

            b_main = Batch(
                item_id=it.id,
                store_id=main_store.id,
                lot_no=lot_main,
                expiry_date=exp_date,
                cost_price=it.cost_price,
                mrp=it.unit_price,
                unit_price=it.unit_price,
                quantity_received=qty_rec,
                quantity_remaining=qty_rec,
                is_quarantined=quarantined,
                quarantine_reason=q_reason,
            )
            session.add(b_main)
            session.flush()

            # Record initial receipt in immutable ledger
            sm = StockMovement(
                timestamp=datetime.now(timezone.utc) - timedelta(days=random.randint(30, 90)),
                movement_type="receipt",
                item_id=it.id,
                batch_id=b_main.id,
                store_id=main_store.id,
                quantity=qty_rec,
                balance_after=qty_rec,
                unit_cost=it.cost_price,
                reason_code="PO_RECEIPT",
                reference_type="goods_receipt",
                reference_id=f"INIT-{lot_main}",
                notes="Initial stock intake from procurement receipt.",
                created_by="storekeeper.dan",
            )
            session.add(sm)

            # Extra batches in satellite stores for select items
            if idx % 5 == 0 and "ER" in db_stores:
                lot_er = f"LOT-2026-{idx+1:04d}E"
                qty_er = random.randint(20, 50)
                b_er = Batch(
                    item_id=it.id,
                    store_id=db_stores["ER"].id,
                    lot_no=lot_er,
                    expiry_date=today + timedelta(days=random.randint(150, 400)),
                    cost_price=it.cost_price,
                    mrp=it.unit_price,
                    unit_price=it.unit_price,
                    quantity_received=qty_er,
                    quantity_remaining=qty_er,
                )
                session.add(b_er)
                session.flush()
                session.add(
                    StockMovement(
                        timestamp=datetime.now(timezone.utc) - timedelta(days=20),
                        movement_type="receipt",
                        item_id=it.id,
                        batch_id=b_er.id,
                        store_id=db_stores["ER"].id,
                        quantity=qty_er,
                        balance_after=qty_er,
                        unit_cost=it.cost_price,
                        reason_code="STORE_TRANSFER",
                        reference_type="transfer",
                        reference_id=f"INIT-{lot_er}",
                        notes="Initial emergency satellite provisioning.",
                        created_by="storekeeper.dan",
                    )
                )

            if idx % 7 == 0 and "WARD" in db_stores:
                lot_ward = f"LOT-2026-{idx+1:04d}W"
                qty_ward = random.randint(25, 60)
                b_ward = Batch(
                    item_id=it.id,
                    store_id=db_stores["WARD"].id,
                    lot_no=lot_ward,
                    expiry_date=today + timedelta(days=random.randint(150, 400)),
                    cost_price=it.cost_price,
                    mrp=it.unit_price,
                    unit_price=it.unit_price,
                    quantity_received=qty_ward,
                    quantity_remaining=qty_ward,
                )
                session.add(b_ward)
                session.flush()
                session.add(
                    StockMovement(
                        timestamp=datetime.now(timezone.utc) - timedelta(days=20),
                        movement_type="receipt",
                        item_id=it.id,
                        batch_id=b_ward.id,
                        store_id=db_stores["WARD"].id,
                        quantity=qty_ward,
                        balance_after=qty_ward,
                        unit_cost=it.cost_price,
                        reason_code="STORE_TRANSFER",
                        reference_type="transfer",
                        reference_id=f"INIT-{lot_ward}",
                        notes="Initial inpatient ward sub-store provisioning.",
                        created_by="storekeeper.dan",
                    )
                )

        session.commit()

    # 5. Purchase Orders & Goods Receipts
    existing_pos = session.exec(select(PurchaseOrder)).all()
    if not existing_pos:
        print("  - Seeding purchase orders and goods receipts...")
        suppliers_list = list(db_suppliers.values())
        po_statuses = [
            ("PO-2026-00001", suppliers_list[0], "received", date.today() - timedelta(days=25), date.today() - timedelta(days=20)),
            ("PO-2026-00002", suppliers_list[1], "received", date.today() - timedelta(days=15), date.today() - timedelta(days=10)),
            ("PO-2026-00003", suppliers_list[2], "partial_received", date.today() - timedelta(days=7), date.today() + timedelta(days=2)),
            ("PO-2026-00004", suppliers_list[3], "ordered", date.today() - timedelta(days=3), date.today() + timedelta(days=5)),
            ("PO-2026-00005", suppliers_list[4], "ordered", date.today() - timedelta(days=1), date.today() + timedelta(days=7)),
            ("PO-2026-00006", suppliers_list[0], "draft", date.today(), date.today() + timedelta(days=10)),
        ]
        for po_no, sup, status_po, odate, edate in po_statuses:
            po = PurchaseOrder(
                po_no=po_no,
                supplier_id=sup.id,
                store_id=main_store.id,
                order_date=odate,
                expected_date=edate,
                status=status_po,
                notes=f"Procurement order for quarterly replenishment with {sup.name}.",
                created_by="storekeeper.dan",
                approved_by="admin" if status_po != "draft" else None,
            )
            session.add(po)
            session.flush()

            sample_items = random.sample(all_items, 4)
            po_total = 0.0
            po_lines = []
            for s_it in sample_items:
                req_qty = random.randint(50, 150)
                rec_qty = req_qty if status_po == "received" else (req_qty // 2 if status_po == "partial_received" else 0)
                line_tot = round(req_qty * s_it.cost_price, 2)
                po_total += line_tot
                pline = PurchaseOrderLine(
                    po_id=po.id,
                    item_id=s_it.id,
                    item_name=s_it.name,
                    requested_qty=req_qty,
                    received_qty=rec_qty,
                    unit_cost=s_it.cost_price,
                    line_total=line_tot,
                )
                session.add(pline)
                po_lines.append((pline, s_it))
            po.total_amount = po_total
            session.add(po)
            session.flush()

            # For received POs, generate GRN
            if status_po in ["received", "partial_received"]:
                grn_no = f"GRN-2026-{po.id:05d}"
                grn = GoodsReceipt(
                    grn_no=grn_no,
                    po_id=po.id,
                    supplier_id=sup.id,
                    store_id=main_store.id,
                    received_date=edate if edate <= date.today() else date.today(),
                    invoice_no=f"INV-{random.randint(10000, 99999)}",
                    received_by="storekeeper.dan",
                    status="received",
                    total_amount=po_total if status_po == "received" else round(po_total / 2, 2),
                    notes="Inspected and verified batch seals intact.",
                )
                session.add(grn)
                session.flush()

                for pline, s_it in po_lines:
                    if pline.received_qty > 0:
                        session.add(
                            GoodsReceiptLine(
                                goods_receipt_id=grn.id,
                                item_id=s_it.id,
                                lot_no=f"LOT-GRN-{pline.id:04d}",
                                expiry_date=date.today() + timedelta(days=365),
                                quantity=pline.received_qty,
                                unit_cost=s_it.cost_price,
                                mrp=s_it.unit_price,
                                line_total=round(pline.received_qty * s_it.cost_price, 2),
                            )
                        )
        session.commit()

    # 6. Seed 80 Dispenses
    current_dispenses_count = len(session.exec(select(Dispense)).all())
    needed_dispenses = 80 - current_dispenses_count
    if needed_dispenses > 0:
        print(f"  - Seeding {needed_dispenses} dispenses (target: 80 total)...")
        all_patients = session.exec(select(Patient)).all()
        all_prescriptions = session.exec(select(Prescription)).all()

        statuses = (
            ["dispensed"] * 40 +
            ["pending"] * 15 +
            ["in_progress"] * 10 +
            ["partial"] * 10 +
            ["counter_otc"] * 5
        )
        if len(statuses) > needed_dispenses:
            statuses = statuses[:needed_dispenses]
        elif len(statuses) < needed_dispenses:
            statuses.extend(["dispensed"] * (needed_dispenses - len(statuses)))

        doctors_names = ["Dr. Sarah Chen, MD", "Dr. James Wilson, MD", "Dr. Elena Rostova, MD", "Dr. David Kim, MD"]

        for idx, status_type in enumerate(statuses):
            d_seq = current_dispenses_count + idx + 1
            d_no = f"DSP-2026-{d_seq:05d}"
            pt = all_patients[idx % len(all_patients)] if all_patients else None
            rx = all_prescriptions[idx % len(all_prescriptions)] if all_prescriptions else None

            is_otc = (status_type == "counter_otc")
            real_status = "dispensed" if is_otc else status_type
            dispense_type = "counter_otc" if is_otc else "prescription"

            created_time = datetime.now(timezone.utc) - timedelta(days=random.randint(0, 14), hours=random.randint(1, 10))
            is_completed = real_status in ["dispensed", "partial"]

            allergy_warn = None
            safety_override = None
            if idx % 7 == 0 and not is_otc:
                allergy_warn = "Warning: Patient allergic to Penicillin."
                safety_override = json.dumps([{"interaction": "Mild potential drug-food interaction", "override_reason": "Patient advised to space doses 2 hours apart from calcium supplements."}])

            disp = Dispense(
                dispense_no=d_no,
                dispense_type=dispense_type,
                prescription_id=None if is_otc else (rx.id if rx else None),
                encounter_id=None if is_otc else (rx.encounter_id if rx else None),
                patient_id=None if is_otc else (pt.id if pt else None),
                patient_name="Counter Walk-in Patient" if is_otc else (pt.first_name + " " + pt.last_name if pt else f"Patient #{idx+1}"),
                patient_mrn=None if is_otc else (pt.mrn if pt else f"MRN-{idx+1:04d}"),
                doctor_id=None if is_otc else 2,
                doctor_name=None if is_otc else doctors_names[idx % len(doctors_names)],
                store_id=main_store.id,
                status=real_status,
                allergy_warnings=allergy_warn,
                safety_overrides=safety_override,
                notes="Processed by outpatient pharmacy." if is_completed else ("Pending pharmacist review" if real_status == "pending" else "Under active review"),
                dispensed_by="pharmacist.lisa" if is_completed else None,
                created_at=created_time,
                dispensed_at=created_time + timedelta(minutes=15) if is_completed else None,
            )
            session.add(disp)
            session.flush()

            disp_total = 0.0
            selected_items = random.sample(all_items, random.randint(1, 3))

            for s_idx, sel_item in enumerate(selected_items):
                p_qty = random.randint(10, 30)
                batch = session.exec(
                    select(Batch)
                    .where(
                        Batch.item_id == sel_item.id,
                        Batch.store_id == main_store.id,
                        Batch.is_quarantined == False,
                        Batch.expiry_date >= today,
                    )
                    .order_by(col(Batch.expiry_date).asc())
                ).first()

                is_sub = (real_status == "partial" and s_idx == 0)
                sub_reason = "Generic substitution due to brand stockout; prescriber notified." if is_sub else None
                orig_drug = f"Brand-{sel_item.generic_name}" if is_sub else None

                if real_status == "partial":
                    d_qty = max(1, p_qty // 2)
                elif is_completed:
                    d_qty = p_qty
                else:
                    d_qty = 0

                unit_price = batch.unit_price if batch else sel_item.unit_price
                line_total = round(unit_price * d_qty, 2) if is_completed else 0.0
                disp_total += line_total

                d_line = DispenseLine(
                    dispense_id=disp.id,
                    item_id=sel_item.id,
                    item_name=sel_item.name,
                    prescribed_item_name=sel_item.name,
                    prescribed_qty=p_qty,
                    dispensed_qty=d_qty,
                    batch_id=batch.id if (batch and is_completed) else None,
                    lot_no=batch.lot_no if (batch and is_completed) else None,
                    expiry_date=batch.expiry_date if (batch and is_completed) else None,
                    unit_price=unit_price,
                    line_total=line_total,
                    is_generic_substitution=is_sub,
                    substitution_reason=sub_reason,
                    original_drug_name=orig_drug,
                )
                session.add(d_line)
                session.flush()

                if is_completed and batch and d_qty > 0:
                    batch.quantity_remaining = max(0, batch.quantity_remaining - d_qty)
                    session.add(batch)

                    session.add(
                        StockMovement(
                            timestamp=disp.dispensed_at or created_time,
                            movement_type="dispense",
                            item_id=sel_item.id,
                            batch_id=batch.id,
                            store_id=main_store.id,
                            quantity=-d_qty,
                            balance_after=batch.quantity_remaining,
                            unit_cost=batch.cost_price,
                            reason_code="OTC_SALE" if is_otc else "RX_DISPENSE",
                            reference_type="dispense",
                            reference_id=disp.dispense_no,
                            notes=f"Dispensed to {disp.patient_name} ({'OTC' if is_otc else 'Rx'})",
                            created_by="pharmacist.lisa",
                        )
                    )

            disp.total_amount = disp_total
            session.add(disp)

        session.commit()

    # 7. Stock Transfer Requests
    existing_transfers = session.exec(select(StockTransferRequest)).all()
    if not existing_transfers and "WARD" in db_stores and "ER" in db_stores:
        print("  - Seeding multi-store stock transfers...")
        transfers_data = [
            ("TRF-2026-00001", main_store.id, db_stores["WARD"].id, "transferred", "pharmacist.lisa", "admin", "Urgent inpatient ward replenishment."),
            ("TRF-2026-00002", main_store.id, db_stores["ER"].id, "transferred", "pharmacist.lisa", "admin", "Emergency trauma bay daily top-up."),
            ("TRF-2026-00003", main_store.id, db_stores["OT"].id, "approved", "storekeeper.dan", "pharmacist.lisa", "Anesthesia and surgical pack stock transfer."),
            ("TRF-2026-00004", main_store.id, db_stores["WARD"].id, "pending", "storekeeper.dan", None, "Weekly ward floor stock routine request."),
        ]
        for trf_no, from_id, to_id, trf_status, req_by, app_by, note in transfers_data:
            trf = StockTransferRequest(
                transfer_no=trf_no,
                from_store_id=from_id,
                to_store_id=to_id,
                status=trf_status,
                requested_by=req_by,
                approved_by=app_by,
                notes=note,
                created_at=datetime.now(timezone.utc) - timedelta(days=2),
                transferred_at=datetime.now(timezone.utc) - timedelta(days=1) if trf_status == "transferred" else None,
            )
            session.add(trf)
            session.flush()

            sample_t_items = random.sample(all_items, 2)
            for t_it in sample_t_items:
                t_qty = random.randint(10, 30)
                session.add(
                    StockTransferLine(
                        transfer_id=trf.id,
                        item_id=t_it.id,
                        requested_qty=t_qty,
                        transferred_qty=t_qty if trf_status == "transferred" else 0,
                    )
                )
        session.commit()

    # 8. Pharmacy Returns
    existing_returns = session.exec(select(PharmacyReturn)).all()
    if not existing_returns:
        print("  - Seeding pharmacy returns with approvals...")
        ret_data = [
            ("RET-2026-00001", "patient_return", "approved", "pharmacist.lisa", "pharmacist.lisa", 45.0, "Patient discharged early; unopened medication returned in sealed packaging."),
            ("RET-2026-00002", "patient_return", "pending", "pharmacist.lisa", None, 28.5, "Physician changed dose from 500mg to 250mg."),
            ("RET-2026-00003", "supplier_return", "approved", "storekeeper.dan", "admin", 120.0, "Damaged secondary packaging delivered by supplier; replacement requested."),
            ("RET-2026-00004", "patient_return", "rejected", "pharmacist.lisa", "pharmacist.lisa", 15.0, "Opened bottle; cold-chain temperature excursion violated return safety policy."),
        ]
        for r_no, r_type, r_status, c_by, a_by, tot, r_reason in ret_data:
            ret = PharmacyReturn(
                return_no=r_no,
                return_type=r_type,
                store_id=main_store.id,
                status=r_status,
                total_amount=tot,
                reason=r_reason,
                created_by=c_by,
                approved_by=a_by,
                created_at=datetime.now(timezone.utc) - timedelta(days=3),
                approved_at=datetime.now(timezone.utc) - timedelta(days=2) if r_status == "approved" else None,
            )
            session.add(ret)
            session.flush()

            r_it = random.choice(all_items)
            session.add(
                PharmacyReturnLine(
                    return_id=ret.id,
                    item_id=r_it.id,
                    quantity=random.randint(1, 5),
                    unit_price=r_it.unit_price,
                    line_total=tot,
                    reason=r_reason,
                )
            )
        session.commit()

    total_items = len(session.exec(select(Item)).all())
    total_batches = len(session.exec(select(Batch)).all())
    total_movements = len(session.exec(select(StockMovement)).all())
    total_dispenses = len(session.exec(select(Dispense)).all())
    print(f"  [OK] Pharmacy seeded: {total_items} items, {total_batches} batches, {total_movements} stock ledger entries, {total_dispenses} dispenses.")


if __name__ == "__main__":
    seed_database()
