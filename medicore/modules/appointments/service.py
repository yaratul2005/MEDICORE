import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from sqlmodel import Session, col, or_, select
from medicore.core.events import event_bus
from medicore.core.settings import settings_registry
from medicore.modules.appointments.models import Appointment, Doctor, QueueToken

logger = logging.getLogger("medicore.appointments.service")


def parse_time_str(time_str: str) -> tuple[int, int]:
    parts = time_str.strip().split(":")
    return int(parts[0]), int(parts[1])


def format_time(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def add_minutes(time_str: str, minutes: int) -> str:
    h, m = parse_time_str(time_str)
    total = h * 60 + m + minutes
    new_h = (total // 60) % 24
    new_m = total % 60
    return format_time(new_h, new_m)


def time_to_minutes(time_str: str) -> int:
    h, m = parse_time_str(time_str)
    return h * 60 + m


# ---------------------------------------------------------------------------
# Slot Generation Service
# ---------------------------------------------------------------------------

def generate_doctor_slots(doctor: Doctor, date_str: str, session: Session) -> List[Dict[str, Any]]:
    """
    Generates available and booked time slots for a given doctor on a specific date,
    respecting weekly templates, date exceptions, leaves, and overbooking rules.
    """
    # 1. Check if doctor is on leave
    if date_str in (doctor.leave or []):
        return []

    # 2. Check if doctor is active
    if not doctor.is_active:
        return []

    # 3. Determine schedule windows (exceptions take precedence over weekly template)
    windows: List[List[str]] = []
    if doctor.exceptions and date_str in doctor.exceptions:
        windows = doctor.exceptions[date_str]
    else:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekday = dt.strftime("%A")
        templates = doctor.schedule_templates or {}
        windows = templates.get(weekday, [])

    if not windows:
        return []

    # 4. Fetch existing non-cancelled appointments for this doctor on this date
    existing_appts = session.exec(
        select(Appointment).where(
            Appointment.doctor_id == doctor.id,
            Appointment.appointment_date == date_str,
            Appointment.status != "Cancelled",
        )
    ).all()

    # 5. Overbooking rules from settings
    appt_rules = settings_registry.get("appointments", "rules", {}) or {}
    allow_overbooking = appt_rules.get("allow_overbooking", False)
    max_overbook = int(appt_rules.get("max_overbook_per_slot", 1))

    slot_length = doctor.slot_length or 20
    slots: List[Dict[str, Any]] = []

    for window in windows:
        if len(window) < 2:
            continue
        start_min = time_to_minutes(window[0])
        end_min = time_to_minutes(window[1])

        current_min = start_min
        while current_min + slot_length <= end_min:
            s_time = format_time(current_min // 60, current_min % 60)
            e_time = format_time((current_min + slot_length) // 60, (current_min + slot_length) % 60)

            # Match overlapping appointments
            matched = [
                a for a in existing_appts
                if time_to_minutes(a.start_time) < (current_min + slot_length)
                and time_to_minutes(a.end_time) > current_min
            ]

            appt_count = len(matched)
            if appt_count == 0:
                slot_status = "available"
            elif allow_overbooking and appt_count <= max_overbook:
                slot_status = "overbook_available"
            else:
                slot_status = "booked"

            slots.append({
                "start_time": s_time,
                "end_time": e_time,
                "status": slot_status,
                "appointment_count": appt_count,
                "appointments": [
                    {
                        "id": a.id,
                        "patient_name": a.patient_name,
                        "status": a.status,
                        "type": a.type,
                    }
                    for a in matched
                ],
            })

            current_min += slot_length

    return slots


# ---------------------------------------------------------------------------
# Conflict & Double-Booking Detection
# ---------------------------------------------------------------------------

def check_booking_conflict(
    doctor_id: int,
    date_str: str,
    start_time: str,
    end_time: str,
    session: Session,
    exclude_id: Optional[int] = None,
) -> Optional[Appointment]:
    """
    Checks if booking overlaps with an existing appointment for the doctor.
    Returns the conflicting Appointment if conflict exists, None otherwise.
    """
    appt_rules = settings_registry.get("appointments", "rules", {}) or {}
    allow_overbooking = appt_rules.get("allow_overbooking", False)
    max_overbook = int(appt_rules.get("max_overbook_per_slot", 1))

    req_start = time_to_minutes(start_time)
    req_end = time_to_minutes(end_time)

    query = select(Appointment).where(
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_date == date_str,
        Appointment.status != "Cancelled",
    )
    if exclude_id:
        query = query.where(Appointment.id != exclude_id)

    existing = session.exec(query).all()

    overlapping = [
        a for a in existing
        if time_to_minutes(a.start_time) < req_end and time_to_minutes(a.end_time) > req_start
    ]

    if not allow_overbooking and len(overlapping) > 0:
        return overlapping[0]

    if allow_overbooking and len(overlapping) > max_overbook:
        return overlapping[0]

    return None


# ---------------------------------------------------------------------------
# Queue Token Management
# ---------------------------------------------------------------------------

DEPT_PREFIX_MAP = {
    "Cardiology": "CARD",
    "Neurology": "NEUR",
    "Pediatrics": "PED",
    "Orthopedics": "ORTH",
    "General Medicine": "GEN",
    "Emergency": "EMER",
}


def generate_queue_token(
    doctor: Doctor,
    patient_id: int,
    patient_name: str,
    patient_mrn: str,
    appointment_id: Optional[int],
    session: Session,
    is_walk_in: bool = False,
) -> QueueToken:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Sequence for this department today
    existing_tokens = session.exec(
        select(QueueToken).where(
            QueueToken.department == doctor.department,
            QueueToken.date == today_str,
        )
    ).all()

    seq = len(existing_tokens) + 1
    prefix = DEPT_PREFIX_MAP.get(doctor.department, "MED")
    token_str = f"{prefix}-{seq:02d}"

    token = QueueToken(
        token_number=token_str,
        appointment_id=appointment_id,
        patient_id=patient_id,
        patient_name=patient_name,
        patient_mrn=patient_mrn,
        doctor_id=doctor.id,
        doctor_name=doctor.name,
        department=doctor.department,
        room_number=doctor.room_number or "Exam 1",
        date=today_str,
        status="Waiting",
        sequence=seq,
        is_walk_in=is_walk_in,
    )
    session.add(token)
    session.commit()
    session.refresh(token)

    event_bus.emit(
        "token.generated",
        {
            "token_id": token.id,
            "token_number": token.token_number,
            "patient_name": token.patient_name,
            "doctor_name": token.doctor_name,
            "department": token.department,
            "room_number": token.room_number,
        },
    )

    return token


# ---------------------------------------------------------------------------
# Pluggable Notification Providers (Reminders)
# ---------------------------------------------------------------------------

class NotificationProvider(ABC):
    @abstractmethod
    def send_appointment_reminder(self, appointment: Appointment) -> bool:
        pass


class ConsoleNotificationProvider(NotificationProvider):
    def send_appointment_reminder(self, appointment: Appointment) -> bool:
        msg = (
            f"[APPOINTMENT REMINDER] Sent to {appointment.patient_name} ({appointment.patient_phone or 'No phone'}): "
            f"Upcoming {appointment.type} with {appointment.doctor_name} in {appointment.department} "
            f"on {appointment.appointment_date} at {appointment.start_time}."
        )
        logger.info(msg)
        print(f"\033[1;35m{msg}\033[0m", flush=True)
        return True


_current_notification_provider: NotificationProvider = ConsoleNotificationProvider()


def get_notification_provider() -> NotificationProvider:
    return _current_notification_provider


def set_notification_provider(provider: NotificationProvider):
    global _current_notification_provider
    _current_notification_provider = provider


def check_and_send_upcoming_reminders(session: Session) -> int:
    """
    Scans for booked appointments in the next 24-48 hours that haven't received
    a reminder yet, and sends reminders through the active provider.
    """
    today = datetime.now(timezone.utc).date()
    tomorrow_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    unreminded = session.exec(
        select(Appointment).where(
            Appointment.appointment_date == tomorrow_str,
            Appointment.status == "Booked",
            Appointment.reminder_sent == False,
        )
    ).all()

    sent_count = 0
    provider = get_notification_provider()
    for appt in unreminded:
        if provider.send_appointment_reminder(appt):
            appt.reminder_sent = True
            session.add(appt)
            sent_count += 1

    if sent_count > 0:
        session.commit()
        logger.info(f"Dispatched {sent_count} appointment reminders.")

    return sent_count


# ---------------------------------------------------------------------------
# Statistics & Analytics Widget
# ---------------------------------------------------------------------------

def calculate_appointment_stats(date_str: str, session: Session) -> Dict[str, Any]:
    appts = session.exec(
        select(Appointment).where(Appointment.appointment_date == date_str)
    ).all()

    total = len(appts)
    booked = sum(1 for a in appts if a.status == "Booked")
    checked_in = sum(1 for a in appts if a.status == "Checked-in")
    in_consultation = sum(1 for a in appts if a.status == "In-Consultation")
    completed = sum(1 for a in appts if a.status == "Completed")
    no_show = sum(1 for a in appts if a.status == "No-show")
    cancelled = sum(1 for a in appts if a.status == "Cancelled")

    resolved = completed + no_show
    no_show_rate = round((no_show / resolved * 100), 1) if resolved > 0 else 0.0

    # Calculate average wait time (minutes from checked_in_at to consultation_started_at)
    wait_times: List[float] = []
    for a in appts:
        if a.checked_in_at and a.consultation_started_at:
            wait_m = (a.consultation_started_at - a.checked_in_at).total_seconds() / 60
            if wait_m >= 0:
                wait_times.append(wait_m)

    avg_wait = round(sum(wait_times) / len(wait_times), 1) if wait_times else 12.5

    return {
        "date": date_str,
        "total": total,
        "booked": booked,
        "checked_in": checked_in,
        "in_consultation": in_consultation,
        "completed": completed,
        "no_show": no_show,
        "cancelled": cancelled,
        "no_show_rate": no_show_rate,
        "avg_wait_minutes": avg_wait,
    }
