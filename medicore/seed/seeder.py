import random
from datetime import date, datetime, timedelta, timezone
from faker import Faker
from sqlmodel import Session, select
from medicore.core.database import engine
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
from medicore.modules.patients.models import Patient

fake = Faker()
Faker.seed(42)
random.seed(42)


def seed_database():
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
            ("Doctor", "Clinical practitioner with patient management and schedule access", [
                "patients.patient.read", "patients.patient.create", "patients.patient.update",
                "appointments.appointment.read", "appointments.appointment.update", "core.audit.view"
            ]),
            ("Nurse", "Inpatient and outpatient care nursing staff", [
                "patients.patient.read", "patients.patient.update",
                "appointments.appointment.read", "appointments.queue.manage"
            ]),
            ("Receptionist", "Patient registration, booking and front-desk intake desk", [
                "patients.patient.read", "patients.patient.create",
                "appointments.appointment.read", "appointments.appointment.create",
                "appointments.appointment.update", "appointments.queue.manage"
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

        total_appts = len(session.exec(select(Appointment)).all())
        total_tokens = len(session.exec(select(QueueToken)).all())
        print(f"Seeding completed successfully! 200 patients, 5 doctors, {total_appts} appointments, and {total_tokens} queue tokens ready.")


if __name__ == "__main__":
    seed_database()
