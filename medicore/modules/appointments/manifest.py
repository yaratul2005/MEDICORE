from typing import List
from sqlmodel import Session, col, or_, select
from medicore.core.models import User
from medicore.core.registry import (
    DashboardWidget,
    ModuleManifest,
    NavEntry,
    PaletteAction,
    PermissionDef,
    SearchResult,
)
from medicore.modules.appointments.models import Appointment, Doctor, QueueToken
from medicore.modules.appointments.router import router


def appointment_search_provider(query: str, user: User, session: Session) -> List[SearchResult]:
    clean_q = query.strip()
    if not clean_q:
        return []

    term = f"%{clean_q}%"
    stmt = (
        select(Appointment)
        .where(
            or_(
                Appointment.patient_name.ilike(term),
                Appointment.patient_mrn.ilike(term),
                Appointment.doctor_name.ilike(term),
                Appointment.department.ilike(term),
            )
        )
        .order_by(col(Appointment.id).desc())
        .limit(5)
    )
    appts = session.exec(stmt).all()

    results: List[SearchResult] = []
    for a in appts:
        results.append(
            SearchResult(
                title=f"{a.patient_name} - {a.doctor_name}",
                subtitle=f"{a.appointment_date} {a.start_time} • {a.department} • {a.type}",
                url=f"/appointments?selected_id={a.id}",
                category="Appointments",
                icon="calendar",
                badge=a.status,
                metadata={"appointment_id": a.id, "doctor_id": a.doctor_id},
            )
        )
    return results


manifest = ModuleManifest(
    name="appointments",
    label="Appointments",
    icon="calendar",
    order=20,
    nav_entries=[
        NavEntry(
            label="Appointments & Calendar",
            url="/appointments",
            icon="calendar",
            permission="appointments.appointment.read",
        )
    ],
    permissions=[
        PermissionDef(
            code="appointments.appointment.read",
            entity="appointment",
            action="read",
            description="View appointments, doctor schedules, and clinic calendar",
        ),
        PermissionDef(
            code="appointments.appointment.create",
            entity="appointment",
            action="create",
            description="Book new clinical appointments and issue tokens",
        ),
        PermissionDef(
            code="appointments.appointment.update",
            entity="appointment",
            action="update",
            description="Reschedule, check-in, and update appointment lifecycle status",
        ),
        PermissionDef(
            code="appointments.appointment.delete",
            entity="appointment",
            action="delete",
            description="Cancel and remove appointments",
        ),
        PermissionDef(
            code="appointments.queue.manage",
            entity="queue",
            action="manage",
            description="Call tokens, serve patients, and manage clinic queue",
        ),
        PermissionDef(
            code="appointments.doctor.configure",
            entity="doctor",
            action="configure",
            description="Configure doctor schedules, slot lengths, exceptions, and leaves",
        ),
    ],
    router=router,
    models=[Doctor, Appointment, QueueToken],
    dashboard_widgets=[
        DashboardWidget(
            id="appointments_stats",
            title="Appointments & Wait Time",
            template="modules/appointments/widgets/stats.html",
            width="col-span-2",
            permission="appointments.appointment.read",
        )
    ],
    search_provider=appointment_search_provider,
    command_palette_actions=[
        PaletteAction(
            id="appointments_book",
            title="Book Appointment",
            category="Appointments",
            url="/appointments?action=new",
            icon="calendar-plus",
            shortcut="A",
            permission="appointments.appointment.create",
        ),
        PaletteAction(
            id="appointments_walkin",
            title="Walk-In Registration & Token",
            category="Appointments",
            url="/appointments/walkin",
            icon="ticket",
            shortcut="W",
            permission="appointments.appointment.create",
        ),
        PaletteAction(
            id="appointments_display",
            title="Open Waiting Room Display",
            category="Appointments",
            url="/appointments/display",
            icon="tv",
            shortcut="D",
            permission="appointments.appointment.read",
        ),
        PaletteAction(
            id="appointments_calendar",
            title="Open Clinical Calendar",
            category="Appointments",
            url="/appointments",
            icon="calendar",
            shortcut="C",
            permission="appointments.appointment.read",
        ),
    ],
    event_handlers={},
)
