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
from medicore.modules.billing.models import (
    CashSession,
    Claim,
    CreditNote,
    Deposit,
    Discount,
    InsurancePolicy,
    InsuranceProvider,
    Invoice,
    InvoiceLine,
    Package,
    Payment,
    PendingCharge,
    PriceList,
    PriceListItem,
    Refund,
    ServiceCatalog,
)
from medicore.modules.billing.router import router
from medicore.modules.billing.service import (
    handle_dispense_completed,
    handle_encounter_completed,
    handle_lab_charge,
    handle_order_placed,
)


def billing_search_provider(query: str, user: User, session: Session) -> List[SearchResult]:
    clean_q = query.strip()
    if not clean_q:
        return []

    term = f"%{clean_q}%"
    results: List[SearchResult] = []

    # 1. Search Invoices
    invoices = session.exec(
        select(Invoice)
        .where(
            or_(
                col(Invoice.invoice_no).ilike(term),
                col(Invoice.patient_name).ilike(term),
                col(Invoice.patient_mrn).ilike(term),
            )
        )
        .order_by(col(Invoice.created_at).desc())
        .limit(4)
    ).all()

    for inv in invoices:
        results.append(
            SearchResult(
                title=f"{inv.invoice_no} - {inv.patient_name}",
                subtitle=f"Total: ${inv.total_amount:.2f} • Due: ${inv.balance_due:.2f} • Status: {inv.status.upper()} • MRN: {inv.patient_mrn}",
                url=f"/billing/invoice/{inv.id}",
                category="Invoices",
                icon="file-text",
                badge=inv.status,
                metadata={"invoice_id": inv.id, "invoice_no": inv.invoice_no},
            )
        )

    # 2. Search Receipts
    payments = session.exec(
        select(Payment)
        .where(
            or_(
                col(Payment.receipt_no).ilike(term),
                col(Payment.payment_no).ilike(term),
                col(Payment.patient_mrn).ilike(term),
            )
        )
        .order_by(col(Payment.received_at).desc())
        .limit(3)
    ).all()

    for p in payments:
        results.append(
            SearchResult(
                title=f"{p.receipt_no} - ${p.amount:.2f}",
                subtitle=f"Method: {p.payment_method.title()} • MRN: {p.patient_mrn} • Cashier: {p.collected_by}",
                url=f"/billing/receipt/{p.id}/print",
                category="Receipts",
                icon="receipt",
                metadata={"payment_id": p.id, "receipt_no": p.receipt_no},
            )
        )

    # 3. Search Claims
    claims = session.exec(
        select(Claim)
        .where(
            or_(
                col(Claim.claim_no).ilike(term),
                col(Claim.provider_name).ilike(term),
                col(Claim.patient_mrn).ilike(term),
            )
        )
        .order_by(col(Claim.submitted_at).desc())
        .limit(3)
    ).all()

    for c in claims:
        results.append(
            SearchResult(
                title=f"{c.claim_no} - {c.provider_name}",
                subtitle=f"Claim: ${c.claim_amount:.2f} • Status: {c.status.upper()} • MRN: {c.patient_mrn}",
                url="/billing/insurance",
                category="Insurance Claims",
                icon="shield",
                badge=c.status,
                metadata={"claim_id": c.id, "claim_no": c.claim_no},
            )
        )

    return results


manifest = ModuleManifest(
    name="billing",
    label="Billing & Cashier",
    icon="receipt",
    order=45,
    nav_entries=[
        NavEntry(
            label="Billing Desk",
            url="/billing",
            icon="receipt",
            permission="billing.payment.collect",
        ),
        NavEntry(
            label="Invoices",
            url="/billing/invoices",
            icon="file-text",
            permission="billing.invoice.create",
        ),
        NavEntry(
            label="Deposits",
            url="/billing/deposits",
            icon="wallet",
            permission="billing.payment.collect",
        ),
        NavEntry(
            label="Cash Sessions",
            url="/billing/sessions",
            icon="banknote",
            permission="billing.session.manage",
        ),
        NavEntry(
            label="Tariffs & Prices",
            url="/billing/pricelists",
            icon="tags",
            permission="billing.pricing.manage",
        ),
        NavEntry(
            label="Packages",
            url="/billing/packages",
            icon="package",
            permission="billing.pricing.manage",
        ),
        NavEntry(
            label="Insurance & Claims",
            url="/billing/insurance",
            icon="shield",
            permission="billing.insurance.manage",
        ),
        NavEntry(
            label="Financial Reports",
            url="/billing/reports",
            icon="bar-chart",
            permission="billing.report.view",
        ),
    ],
    permissions=[
        PermissionDef(
            code="billing.payment.collect",
            entity="payment",
            action="collect",
            description="Collect patient payments and advance deposits at cashier till",
        ),
        PermissionDef(
            code="billing.invoice.create",
            entity="invoice",
            action="create",
            description="Generate draft invoices from pending charges or manual lines",
        ),
        PermissionDef(
            code="billing.invoice.finalize",
            entity="invoice",
            action="finalize",
            description="Finalize patient invoices locking them as immutable",
        ),
        PermissionDef(
            code="billing.discount.apply",
            entity="discount",
            action="apply",
            description="Apply standard discounts and promotional concessions",
        ),
        PermissionDef(
            code="billing.discount.approve",
            entity="discount",
            action="approve",
            description="Manager authorization for high-value discounts and waivers",
        ),
        PermissionDef(
            code="billing.refund.request",
            entity="refund",
            action="request",
            description="Initiate patient payment or deposit refund requests",
        ),
        PermissionDef(
            code="billing.refund.approve",
            entity="refund",
            action="approve",
            description="Manager authorization and disbursement of refunds",
        ),
        PermissionDef(
            code="billing.credit_note.issue",
            entity="credit_note",
            action="issue",
            description="Issue official credit notes for invoice adjustments",
        ),
        PermissionDef(
            code="billing.session.manage",
            entity="session",
            action="manage",
            description="Open and close cashier cash sessions with till reconciliation",
        ),
        PermissionDef(
            code="billing.pricing.manage",
            entity="pricing",
            action="manage",
            description="Configure price lists, tariffs, and clinical package bundles",
        ),
        PermissionDef(
            code="billing.insurance.manage",
            entity="insurance",
            action="manage",
            description="Manage insurance policies, coverage rules, and claims",
        ),
        PermissionDef(
            code="billing.report.view",
            entity="report",
            action="view",
            description="Access revenue analytics, collection breakdowns, and insurance aging",
        ),
    ],
    router=router,
    models=[
        PriceList,
        ServiceCatalog,
        PriceListItem,
        Package,
        InsuranceProvider,
        InsurancePolicy,
        PendingCharge,
        Invoice,
        InvoiceLine,
        Payment,
        Deposit,
        Refund,
        Discount,
        CreditNote,
        Claim,
        CashSession,
    ],
    dashboard_widgets=[
        DashboardWidget(
            id="billing_collection_widget",
            title="Daily Collections & Till Status",
            template="modules/billing/widgets/collection.html",
            width="col-span-1",
            permission="billing.payment.collect",
        )
    ],
    search_provider=billing_search_provider,
    command_palette_actions=[
        PaletteAction(
            id="billing_desk",
            title="Open Billing Desk",
            category="Billing",
            url="/billing",
            icon="receipt",
            shortcut="B D",
        ),
        PaletteAction(
            id="billing_invoices",
            title="Patient Invoices Directory",
            category="Billing",
            url="/billing/invoices",
            icon="file-text",
            shortcut="B I",
        ),
        PaletteAction(
            id="billing_sessions",
            title="Cash Drawer & Till Sessions",
            category="Billing",
            url="/billing/sessions",
            icon="banknote",
            shortcut="B S",
        ),
        PaletteAction(
            id="billing_deposits",
            title="Collect Advance Deposit",
            category="Billing",
            url="/billing/deposits",
            icon="wallet",
            shortcut="B A",
        ),
        PaletteAction(
            id="billing_insurance",
            title="Insurance & Claims Tracker",
            category="Billing",
            url="/billing/insurance",
            icon="shield",
            shortcut="B C",
        ),
        PaletteAction(
            id="billing_pricelists",
            title="Tariffs & Price Lists",
            category="Billing",
            url="/billing/pricelists",
            icon="tags",
            shortcut="B P",
        ),
        PaletteAction(
            id="billing_reports",
            title="Revenue & Financial Reports",
            category="Billing",
            url="/billing/reports",
            icon="bar-chart",
            shortcut="B R",
        ),
    ],
    event_handlers={
        "encounter.completed": [handle_encounter_completed],
        "dispense.completed": [handle_dispense_completed],
        "lab.charge": [handle_lab_charge],
        "order.placed": [handle_order_placed],
    },
)
