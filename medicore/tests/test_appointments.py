from datetime import date, datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from medicore.core.database import engine
from medicore.core.events import event_bus
from medicore.core.settings import settings_registry
from medicore.modules.appointments.models import Appointment, Doctor, QueueToken, utc_now
from medicore.modules.appointments.service import (
    check_and_send_upcoming_reminders,
    check_booking_conflict,
    generate_doctor_slots,
    generate_queue_token,
    calculate_appointment_stats,
)
from medicore.modules.patients.models import Patient


def test_doctor_slot_generation():
    """Verify slots are generated according to weekly template and respect leave."""
    with Session(engine) as session:
        doctor = session.exec(select(Doctor).where(Doctor.department == "Cardiology")).first()
        assert doctor is not None

        # Monday slot generation (Doctor has 09:00-12:00, 14:00-17:00, 20 min slots)
        # Next Monday
        today = date.today()
        days_ahead = 0 - today.weekday() if today.weekday() <= 0 else 7 - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        next_monday = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        slots = generate_doctor_slots(doctor, next_monday, session)
        assert len(slots) > 0
        assert slots[0]["start_time"] == "09:00"
        assert slots[0]["end_time"] == "09:20"

        # Check leave date: if doctor is on leave, returns 0 slots
        leave_date = "2026-12-25"
        doctor.leave = [leave_date]
        session.add(doctor)
        session.commit()

        leave_slots = generate_doctor_slots(doctor, leave_date, session)
        assert len(leave_slots) == 0


def test_booking_conflict_detection():
    """Verify conflict detector flags overlapping appointments."""
    with Session(engine) as session:
        doctor = session.exec(select(Doctor).where(Doctor.department == "Neurology")).first()
        patient = session.exec(select(Patient)).first()
        assert doctor is not None
        assert patient is not None

        test_date = "2026-11-20"
        # Clean up test appointments on test_date for test isolation
        for old in session.exec(select(Appointment).where(Appointment.doctor_id == doctor.id, Appointment.appointment_date == test_date)).all():
            session.delete(old)
        session.commit()

        # Temporarily disallow overbooking to test strict conflict detection
        settings_registry.set("appointments", "rules", {"allow_overbooking": False})
        session.commit()

        # Create first appointment: 10:00 to 10:30
        start_dt = datetime.strptime(f"{test_date} 10:00", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(f"{test_date} 10:30", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        appt1 = Appointment(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            department=doctor.department,
            appointment_date=test_date,
            start_time="10:00",
            end_time="10:30",
            start_datetime=start_dt,
            end_datetime=end_dt,
            status="Booked",
        )
        session.add(appt1)
        session.commit()

        # Check conflict overlapping 10:15 - 10:45
        conflict = check_booking_conflict(doctor.id, test_date, "10:15", "10:45", session)
        assert conflict is not None
        assert conflict.id == appt1.id

        # Non-overlapping time slot 11:00 - 11:30 has no conflict
        no_conflict = check_booking_conflict(doctor.id, test_date, "11:00", "11:30", session)
        assert no_conflict is None


def test_status_transitions_and_events(client: TestClient):
    """Verify status flow: Booked -> Checked-in -> In-Consultation -> Completed."""
    emitted_events = []

    def capture_event(event):
        emitted_events.append(event.name)

    event_bus.subscribe("*", capture_event)

    with Session(engine) as session:
        doctor = session.exec(select(Doctor)).first()
        patient = session.exec(select(Patient)).first()

        start_dt = utc_now()
        end_dt = start_dt + timedelta(minutes=20)
        appt = Appointment(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            department=doctor.department,
            appointment_date=start_dt.strftime("%Y-%m-%d"),
            start_time="14:00",
            end_time="14:20",
            start_datetime=start_dt,
            end_datetime=end_dt,
            status="Booked",
        )
        session.add(appt)
        session.commit()
        session.refresh(appt)
        appt_id = appt.id

    # 1. Transition to Checked-in
    res = client.post(f"/appointments/{appt_id}/status?new_status=Checked-in")
    assert res.status_code == 200
    assert "appointment.checked_in" in emitted_events

    with Session(engine) as session:
        updated = session.get(Appointment, appt_id)
        assert updated.status == "Checked-in"
        assert updated.checked_in_at is not None
        # Queue token should have been auto-generated
        token = session.exec(select(QueueToken).where(QueueToken.appointment_id == appt_id)).first()
        assert token is not None
        assert token.status == "Waiting"

    # 2. Transition to In-Consultation
    res = client.post(f"/appointments/{appt_id}/status?new_status=In-Consultation")
    assert res.status_code == 200
    assert "appointment.consultation_started" in emitted_events

    with Session(engine) as session:
        updated = session.get(Appointment, appt_id)
        assert updated.status == "In-Consultation"
        assert updated.consultation_started_at is not None
        token = session.exec(select(QueueToken).where(QueueToken.appointment_id == appt_id)).first()
        assert token.status == "Serving"

    # 3. Transition to Completed
    res = client.post(f"/appointments/{appt_id}/status?new_status=Completed")
    assert res.status_code == 200
    assert "appointment.completed" in emitted_events

    with Session(engine) as session:
        updated = session.get(Appointment, appt_id)
        assert updated.status == "Completed"
        assert updated.completed_at is not None
        token = session.exec(select(QueueToken).where(QueueToken.appointment_id == appt_id)).first()
        assert token.status == "Done"


def test_walkin_registration_and_queue_token(client: TestClient):
    """Verify quick walk-in issues a sequential token and registers appointment."""
    with Session(engine) as session:
        doctor = session.exec(select(Doctor).where(Doctor.department == "Pediatrics")).first()
        assert doctor is not None

    res = client.post(
        "/appointments/walkin",
        data={
            "doctor_id": doctor.id,
            "first_name": "Oliver",
            "last_name": "Twist",
            "phone": "+1 555-888-9999",
            "gender": "Male",
            "type": "Walk-in",
            "notes": "Mild fever and ear pain",
        },
    )
    assert res.status_code == 200
    assert "Token Issued:" in res.text

    with Session(engine) as session:
        token = session.exec(
            select(QueueToken)
            .where(QueueToken.patient_name == "Oliver Twist")
            .order_by(QueueToken.id.desc())
        ).first()
        assert token is not None
        assert token.token_number.startswith("PED-")
        assert token.is_walk_in == True
        assert token.status == "Waiting"


def test_waiting_room_display_endpoints(client: TestClient):
    """Verify full-screen display and HTMX partial polling endpoints return 200."""
    res_display = client.get("/appointments/display")
    assert res_display.status_code == 200
    assert "Waiting Room Patient Display" in res_display.text
    assert "display-content" in res_display.text

    res_content = client.get("/appointments/display/content")
    assert res_content.status_code == 200
    assert "Waiting Room Queue" in res_content.text


def test_reschedule_with_conflict_guard(client: TestClient):
    """Verify rescheduling checks for conflicts and updates timestamps."""
    with Session(engine) as session:
        doctor = session.exec(select(Doctor)).first()
        patient = session.exec(select(Patient)).first()

        for d_str in ["2026-11-28", "2026-11-29"]:
            for old in session.exec(select(Appointment).where(Appointment.appointment_date == d_str)).all():
                session.delete(old)
        session.commit()

        start_dt = utc_now()
        appt = Appointment(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            department=doctor.department,
            appointment_date="2026-11-28",
            start_time="09:00",
            end_time="09:20",
            start_datetime=start_dt,
            end_datetime=start_dt + timedelta(minutes=20),
            status="Booked",
        )
        session.add(appt)
        session.commit()
        session.refresh(appt)
        appt_id = appt.id

    # Reschedule to a new time
    res = client.post(
        f"/appointments/{appt_id}/reschedule",
        data={
            "appointment_date": "2026-11-29",
            "start_time": "14:00",
            "end_time": "14:20",
            "reschedule_reason": "Patient conflict",
        },
    )
    assert res.status_code == 200
    with Session(engine) as session:
        updated = session.get(Appointment, appt_id)
        assert updated.appointment_date == "2026-11-29"
        assert updated.start_time == "14:00"
        assert "[Rescheduled: Patient conflict]" in updated.notes


def test_appointment_reminders_and_stats():
    """Verify notification reminder dispatch and statistics widget calculations."""
    with Session(engine) as session:
        doctor = session.exec(select(Doctor)).first()
        patient = session.exec(select(Patient)).first()

        tomorrow_str = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        start_dt = utc_now() + timedelta(days=1)
        appt = Appointment(
            patient_id=patient.id,
            patient_name=patient.full_name,
            patient_mrn=patient.mrn,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            department=doctor.department,
            appointment_date=tomorrow_str,
            start_time="10:00",
            end_time="10:20",
            start_datetime=start_dt,
            end_datetime=start_dt + timedelta(minutes=20),
            status="Booked",
            reminder_sent=False,
        )
        session.add(appt)
        session.commit()

        # Run reminder checker
        sent = check_and_send_upcoming_reminders(session)
        assert sent >= 1

        session.refresh(appt)
        assert appt.reminder_sent == True

        # Calculate statistics
        today_str = date.today().strftime("%Y-%m-%d")
        stats = calculate_appointment_stats(today_str, session)
        assert "total" in stats
        assert "no_show_rate" in stats
        assert "avg_wait_minutes" in stats
