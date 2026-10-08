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
from medicore.modules.pharmacy.router import router
from medicore.modules.pharmacy.service import handle_prescription_signed


def pharmacy_search_provider(query: str, user: User, session: Session) -> List[SearchResult]:
    clean_q = query.strip()
    if not clean_q:
        return []

    term = f"%{clean_q}%"
    results: List[SearchResult] = []

    # 1. Search items by code, name, generic name, brand name
    items = session.exec(
        select(Item)
        .where(
            Item.is_active == True,
            or_(
                Item.code.ilike(term),
                Item.name.ilike(term),
                Item.generic_name.ilike(term),
                Item.brand_name.ilike(term),
            ),
        )
        .limit(4)
    ).all()

    for it in items:
        results.append(
            SearchResult(
                title=f"{it.name} ({it.code})",
                subtitle=f"{it.category} • Generic: {it.generic_name} • MRP: ${it.unit_price:.2f}",
                url=f"/pharmacy/inventory/item/{it.id}",
                category="Pharmacy Items",
                icon="pill",
                badge=it.category,
                metadata={"item_id": it.id, "code": it.code},
            )
        )

    # 2. Search dispenses by dispense_no or patient_name
    dispenses = session.exec(
        select(Dispense)
        .where(
            or_(
                Dispense.dispense_no.ilike(term),
                Dispense.patient_name.ilike(term),
                Dispense.patient_mrn.ilike(term),
            )
        )
        .order_by(col(Dispense.created_at).desc())
        .limit(3)
    ).all()

    for d in dispenses:
        results.append(
            SearchResult(
                title=f"Dispense #{d.dispense_no}: {d.patient_name}",
                subtitle=f"{d.dispense_type.upper()} • Status: {d.status} • Total: ${d.total_amount:.2f}",
                url=f"/pharmacy/dispense/{d.id}",
                category="Pharmacy Dispenses",
                icon="clipboard-list",
                badge=d.status,
                metadata={"dispense_id": d.id, "dispense_no": d.dispense_no},
            )
        )

    return results


manifest = ModuleManifest(
    name="pharmacy",
    label="Pharmacy & Dispensing",
    icon="pill",
    order=40,
    nav_entries=[
        NavEntry(
            label="Pharmacy & Inventory",
            url="/pharmacy",
            icon="pill",
            permission="pharmacy.dispense.read",
        )
    ],
    permissions=[
        PermissionDef(
            code="pharmacy.dispense.read",
            entity="dispense",
            action="read",
            description="View pharmacy queue, prescriptions, and stock levels",
        ),
        PermissionDef(
            code="pharmacy.dispense.process",
            entity="dispense",
            action="process",
            description="Dispense prescriptions and process counter OTC transactions",
        ),
        PermissionDef(
            code="pharmacy.stock.read",
            entity="stock",
            action="read",
            description="View inventory directory, batches, and movement ledger",
        ),
        PermissionDef(
            code="pharmacy.stock.manage",
            entity="stock",
            action="manage",
            description="Receive shipments, initiate store transfers, and quarantine stock",
        ),
        PermissionDef(
            code="pharmacy.stock.approve",
            entity="stock",
            action="approve",
            description="Authorize stock adjustments, write-offs, and returned medications",
        ),
        PermissionDef(
            code="pharmacy.po.create",
            entity="purchase_order",
            action="create",
            description="Create and draft purchase orders to suppliers",
        ),
        PermissionDef(
            code="pharmacy.po.manage",
            entity="purchase_order",
            action="manage",
            description="Manage purchase orders and supplier goods receipt records",
        ),
        PermissionDef(
            code="pharmacy.reports.read",
            entity="reports",
            action="read",
            description="Access valuation, expiry, and medication consumption reports",
        ),
    ],
    router=router,
    models=[
        PharmacyStore,
        Item,
        Batch,
        Supplier,
        PurchaseOrder,
        PurchaseOrderLine,
        GoodsReceipt,
        GoodsReceiptLine,
        StockMovement,
        Dispense,
        DispenseLine,
        PharmacyReturn,
        PharmacyReturnLine,
        StockTransferRequest,
        StockTransferLine,
    ],
    search_provider=pharmacy_search_provider,
    command_palette_actions=[
        PaletteAction(
            id="pharmacy_queue",
            title="Open Dispensing Queue",
            category="Pharmacy",
            url="/pharmacy",
            icon="pill",
            shortcut="P Q",
        ),
        PaletteAction(
            id="pharmacy_otc",
            title="Counter Sales (OTC POS)",
            category="Pharmacy",
            url="/pharmacy/otc",
            icon="shopping-cart",
            shortcut="P C",
        ),
        PaletteAction(
            id="pharmacy_inventory",
            title="Pharmacy Inventory & Batches",
            category="Pharmacy",
            url="/pharmacy/inventory",
            icon="boxes",
            shortcut="P I",
        ),
        PaletteAction(
            id="pharmacy_purchasing",
            title="Purchase Orders & Reorders",
            category="Pharmacy",
            url="/pharmacy/purchasing",
            icon="truck",
        ),
        PaletteAction(
            id="pharmacy_reports",
            title="Pharmacy Reports & Valuation",
            category="Pharmacy",
            url="/pharmacy/reports",
            icon="bar-chart-3",
        ),
    ],
    dashboard_widgets=[
        DashboardWidget(
            id="pharmacy_alerts",
            title="Pharmacy Alerts & Stock Status",
            template="modules/pharmacy/widgets/alerts.html",
            width="col-span-1",
            permission="pharmacy.stock.read",
        )
    ],
    event_handlers={
        "prescription.signed": [handle_prescription_signed],
    },
)
