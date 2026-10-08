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
from medicore.modules.laboratory.models import (
    LabOrder,
    LabOrderItem,
    LabReport,
    Parameter,
    ReferenceRange,
    Result,
    Specimen,
    TestCatalog,
    TestPanel,
)
from medicore.modules.laboratory.router import router
from medicore.modules.laboratory.service import handle_order_placed


def laboratory_search_provider(query: str, user: User, session: Session) -> List[SearchResult]:
    clean_q = query.strip()
    if not clean_q:
        return []

    term = f"%{clean_q}%"
    results: List[SearchResult] = []

    # 1. Search Lab Orders
    orders = session.exec(
        select(LabOrder)
        .where(
            or_(
                col(LabOrder.order_no).ilike(term),
                col(LabOrder.patient_name).ilike(term),
                col(LabOrder.patient_mrn).ilike(term),
            )
        )
        .order_by(col(LabOrder.ordered_at).desc())
        .limit(4)
    ).all()

    for o in orders:
        results.append(
            SearchResult(
                title=f"{o.order_no} - {o.patient_name}",
                subtitle=f"{o.department} • Priority: {o.priority} • Status: {o.status.title()} • MRN: {o.patient_mrn}",
                url=f"/laboratory/order/{o.id}/results",
                category="Lab Orders",
                icon="flask-conical",
                badge=o.priority,
                metadata={"order_id": o.id, "order_no": o.order_no},
            )
        )

    # 2. Search Specimen Barcodes
    specimens = session.exec(
        select(Specimen)
        .where(col(Specimen.barcode).ilike(term))
        .limit(3)
    ).all()

    for s in specimens:
        results.append(
            SearchResult(
                title=f"Sample: {s.barcode}",
                subtitle=f"{s.specimen_type} • Status: {s.status.title()}",
                url=f"/laboratory/order/{s.lab_order_id}/results",
                category="Specimens",
                icon="qr-code",
                badge=s.status,
                metadata={"specimen_id": s.id, "barcode": s.barcode},
            )
        )

    # 3. Search Catalog Tests
    tests = session.exec(
        select(TestCatalog)
        .where(
            TestCatalog.is_active == True,
            or_(
                col(TestCatalog.code).ilike(term),
                col(TestCatalog.name).ilike(term),
                col(TestCatalog.department).ilike(term),
            ),
        )
        .limit(3)
    ).all()

    for t in tests:
        results.append(
            SearchResult(
                title=f"{t.name} ({t.code})",
                subtitle=f"{t.department} • Specimen: {t.specimen_type} • TAT: {t.tat_hours}h • ${t.price:.2f}",
                url=f"/laboratory/catalog/test/{t.id}",
                category="Lab Catalog",
                icon="test-tube",
                badge=t.department,
                metadata={"test_id": t.id, "code": t.code},
            )
        )

    return results


manifest = ModuleManifest(
    name="laboratory",
    title="Laboratory",
    version="1.0.0",
    description="Clinical Laboratory Management (LIS): Worklist, Barcode Specimen Flow, Result Grids, Reference Intervals, Two-step Pathologist Approval, and Branded Diagnostic Reports",
    router=router,
    nav_entries=[
        NavEntry(
            id="laboratory",
            title="Laboratory",
            url="/laboratory",
            icon="flask-conical",
            section="clinical",
            order=25,
            permission="laboratory.order.view",
            children=[
                NavEntry(
                    id="lab_worklist",
                    title="Worklist",
                    url="/laboratory",
                    icon="list-todo",
                    section="clinical",
                    order=1,
                    permission="laboratory.order.view",
                ),
                NavEntry(
                    id="lab_catalog",
                    title="Test Catalog",
                    url="/laboratory/catalog",
                    icon="book-open",
                    section="clinical",
                    order=2,
                    permission="laboratory.catalog.manage",
                ),
                NavEntry(
                    id="lab_import",
                    title="Analyzer CSV Import",
                    url="/laboratory/import",
                    icon="upload",
                    section="clinical",
                    order=3,
                    permission="laboratory.result.enter",
                ),
            ],
        )
    ],
    permissions=[
        PermissionDef(
            name="laboratory.order.view",
            description="View laboratory worklist and patient test requisitions",
            module="laboratory",
        ),
        PermissionDef(
            name="laboratory.order.create",
            description="Create walk-in or manual laboratory test orders",
            module="laboratory",
        ),
        PermissionDef(
            name="laboratory.specimen.collect",
            description="Collect biological samples and generate barcode labels",
            module="laboratory",
        ),
        PermissionDef(
            name="laboratory.specimen.receive",
            description="Receive biological specimens in central laboratory",
            module="laboratory",
        ),
        PermissionDef(
            name="laboratory.specimen.reject",
            description="Reject compromised samples and request recollect",
            module="laboratory",
        ),
        PermissionDef(
            name="laboratory.result.enter",
            description="Enter test results and import automated analyzer CSVs",
            module="laboratory",
        ),
        PermissionDef(
            name="laboratory.result.approve",
            description="Pathologist authorization and approval of diagnostic lab reports",
            module="laboratory",
        ),
        PermissionDef(
            name="laboratory.result.amend",
            description="Amend and issue corrected diagnostic reports with audit logging",
            module="laboratory",
        ),
        PermissionDef(
            name="laboratory.critical.acknowledge",
            description="Physician acknowledgement of life-threatening critical lab values",
            module="laboratory",
        ),
        PermissionDef(
            name="laboratory.catalog.manage",
            description="Manage test catalog, LOINC mappings, and biological reference intervals",
            module="laboratory",
        ),
    ],
    search_provider=laboratory_search_provider,
    command_palette_actions=[
        PaletteAction(
            id="lab_worklist",
            title="Open Laboratory Worklist",
            category="Laboratory",
            url="/laboratory",
            icon="flask-conical",
            shortcut="L W",
        ),
        PaletteAction(
            id="lab_new_order",
            title="New Lab Requisition",
            category="Laboratory",
            url="/laboratory/orders/new",
            icon="plus-circle",
            shortcut="L N",
        ),
        PaletteAction(
            id="lab_catalog_admin",
            title="Lab Test Catalog & LOINC",
            category="Laboratory",
            url="/laboratory/catalog",
            icon="book-open",
            shortcut="L C",
        ),
        PaletteAction(
            id="lab_analyzer_import",
            title="Import Analyzer CSV",
            category="Laboratory",
            url="/laboratory/import",
            icon="upload",
        ),
    ],
    dashboard_widgets=[
        DashboardWidget(
            id="lab_worklist_widget",
            title="STAT & Pending Laboratory Orders",
            template="modules/laboratory/widgets/worklist.html",
            width="col-span-1",
            permission="laboratory.order.view",
        )
    ],
    event_handlers={
        "order.placed": [handle_order_placed],
    },
)
