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
from medicore.modules.consultations.router import router


def consultation_search_provider(query: str, user: User, session: Session) -> List[SearchResult]:
    clean_q = query.strip()
    if not clean_q:
        return []

    term = f"%{clean_q}%"
    stmt = (
        select(Encounter)
        .where(
            or_(
                Encounter.patient_name.ilike(term),
                Encounter.patient_mrn.ilike(term),
                Encounter.doctor_name.ilike(term),
                Encounter.chief_complaint.ilike(term),
            )
        )
        .order_by(col(Encounter.started_at).desc())
        .limit(5)
    )
    encounters = session.exec(stmt).all()

    results: List[SearchResult] = []
    for enc in encounters:
        results.append(
            SearchResult(
                title=f"Encounter #{enc.id}: {enc.patient_name}",
                subtitle=f"{enc.doctor_name} • {enc.chief_complaint or 'Consultation'} • {enc.started_at.strftime('%Y-%m-%d')}",
                url=f"/consultations/encounter/{enc.id}",
                category="Consultations",
                icon="stethoscope",
                badge=enc.status,
                metadata={"encounter_id": enc.id, "patient_id": enc.patient_id},
            )
        )
    return results


manifest = ModuleManifest(
    name="consultations",
    label="OPD Consultation",
    icon="stethoscope",
    order=30,
    nav_entries=[
        NavEntry(
            label="Doctor Queue & EMR",
            url="/consultations",
            icon="stethoscope",
            permission="consultations.encounter.read",
        )
    ],
    permissions=[
        PermissionDef(
            code="consultations.encounter.read",
            entity="encounter",
            action="read",
            description="View consultation queue, clinical history, and patient notes",
        ),
        PermissionDef(
            code="consultations.encounter.start",
            entity="encounter",
            action="start",
            description="Initiate OPD clinical encounter from queue or patient record",
        ),
        PermissionDef(
            code="consultations.vitals.create",
            entity="vitals",
            action="create",
            description="Record patient vital signs, BMI/BSA, and clinical triage values",
        ),
        PermissionDef(
            code="consultations.notes.write",
            entity="note",
            action="write",
            description="Author, draft, and modify SOAP clinical notes and addenda",
        ),
        PermissionDef(
            code="consultations.notes.sign",
            entity="note",
            action="sign",
            description="Sign and lock clinical notes (making them immutable legal records)",
        ),
        PermissionDef(
            code="consultations.prescription.write",
            entity="prescription",
            action="write",
            description="Issue e-prescriptions and override safety decision warnings",
        ),
        PermissionDef(
            code="consultations.orders.create",
            entity="orders",
            action="create",
            description="Place laboratory and diagnostic imaging orders",
        ),
        PermissionDef(
            code="consultations.encounter.complete",
            entity="encounter",
            action="complete",
            description="Complete clinical encounter and discharge outpatient",
        ),
    ],
    router=router,
    models=[Encounter, Vitals, ClinicalNote, Diagnosis, Prescription, PrescriptionItem, Order, FollowUp, Referral],
    dashboard_widgets=[
        DashboardWidget(
            id="consultations_stats",
            title="OPD Clinical Consultation Activity",
            template="modules/consultations/widgets/stats.html",
            width="col-span-2",
            permission="consultations.encounter.read",
        )
    ],
    search_provider=consultation_search_provider,
    command_palette_actions=[
        PaletteAction(
            id="consultation_queue",
            title="Doctor Consultation Queue",
            category="Consultations",
            url="/consultations",
            icon="stethoscope",
            shortcut="Q",
            permission="consultations.encounter.read",
        ),
        PaletteAction(
            id="consultation_active",
            title="Open Active Consultations",
            category="Consultations",
            url="/consultations?tab=in_progress",
            icon="activity",
            shortcut="E",
            permission="consultations.encounter.read",
        ),
    ],
    event_handlers={},
)
