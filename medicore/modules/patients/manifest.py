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
from medicore.modules.patients.models import Patient
from medicore.modules.patients.router import router


def patient_search_provider(query: str, user: User, session: Session) -> List[SearchResult]:
    clean_q = query.strip()
    if not clean_q:
        return []

    term = f"%{clean_q}%"
    stmt = (
        select(Patient)
        .where(
            or_(
                Patient.mrn.ilike(term),
                Patient.first_name.ilike(term),
                Patient.last_name.ilike(term),
                Patient.phone.ilike(term),
            )
        )
        .order_by(col(Patient.id).desc())
        .limit(5)
    )
    patients = session.exec(stmt).all()

    results: List[SearchResult] = []
    for p in patients:
        results.append(
            SearchResult(
                title=f"{p.full_name} ({p.mrn})",
                subtitle=f"DOB: {p.date_of_birth} • Phone: {p.phone} • Status: {p.status}",
                url=f"/patients?selected_id={p.id}",
                category="Patients",
                icon="user",
                badge=p.status,
                metadata={"patient_id": p.id, "mrn": p.mrn},
            )
        )
    return results


manifest = ModuleManifest(
    name="patients",
    label="Patients",
    icon="users",
    order=10,
    nav_entries=[
        NavEntry(
            label="Patients Directory",
            url="/patients",
            icon="users",
            permission="patients.patient.read",
        )
    ],
    permissions=[
        PermissionDef(
            code="patients.patient.read",
            entity="patient",
            action="read",
            description="View patient records, medical record numbers, and clinical details",
        ),
        PermissionDef(
            code="patients.patient.create",
            entity="patient",
            action="create",
            description="Register new patients and assign MRNs",
        ),
        PermissionDef(
            code="patients.patient.update",
            entity="patient",
            action="update",
            description="Edit patient demographics, contact info, and status",
        ),
        PermissionDef(
            code="patients.patient.delete",
            entity="patient",
            action="delete",
            description="Archive or deactivate patient files",
        ),
    ],
    router=router,
    models=[Patient],
    dashboard_widgets=[
        DashboardWidget(
            id="patients_overview_stat",
            title="Total Registered Patients",
            template="modules/patients/widgets/stat_total.html",
            width="col-span-1",
            permission="patients.patient.read",
        )
    ],
    search_provider=patient_search_provider,
    command_palette_actions=[
        PaletteAction(
            id="patients_new",
            title="Register New Patient",
            category="Patients",
            url="/patients?action=new",
            icon="user-plus",
            shortcut="N",
            permission="patients.patient.create",
        ),
        PaletteAction(
            id="patients_directory",
            title="Open Patients Directory",
            category="Patients",
            url="/patients",
            icon="users",
            shortcut="P",
            permission="patients.patient.read",
        ),
    ],
    event_handlers={},
)
