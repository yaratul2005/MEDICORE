import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlmodel import Session, col, desc, func, or_, select

from medicore.core.database import get_session
from medicore.core.models import User
from medicore.core.security import get_current_user, require_permission, user_has_permission
from medicore.core.settings import settings_registry
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
    utc_now,
)
from medicore.modules.billing.service import (
    amount_to_words,
    apply_deposit_to_invoice,
    apply_discount,
    apply_package_to_invoice,
    approve_discount,
    approve_refund,
    close_cash_session,
    create_insurance_claim,
    create_invoice_from_pending_charges,
    export_collection_summary_csv,
    export_insurance_aging_csv,
    export_outstanding_dues_csv,
    export_revenue_by_department_csv,
    finalize_invoice,
    generate_credit_note_pdf,
    generate_invoice_pdf,
    generate_qr_code_data_uri,
    generate_receipt_pdf,
    get_active_cash_session,
    issue_credit_note,
    open_cash_session,
    recalculate_invoice,
    record_payment,
    refund_deposit_balance,
    request_refund,
    update_claim_status,
)
from medicore.modules.patients.models import Patient
from medicore.ui.templating import get_ui_context, templates

router = APIRouter(prefix="/billing", tags=["Billing"])


# =========================================================================
# 1. CASHIER DESK & BILLING HOME
# =========================================================================

@router.get("", response_class=HTMLResponse)
def billing_desk(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Main cashier & billing desk overview."""
    active_session = get_active_cash_session(session, user.username)

    # Summary metrics
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_payments = session.exec(
        select(Payment).where(Payment.received_at >= today_start, Payment.status == "completed")
    ).all()
    today_collections = sum(p.amount for p in today_payments)

    pending_charges_count = session.exec(
        select(func.count(PendingCharge.id)).where(PendingCharge.status == "pending")
    ).one()

    outstanding_dues = session.exec(
        select(func.sum(Invoice.balance_due)).where(Invoice.is_finalized == True, Invoice.balance_due > 0)
    ).one() or 0.0

    active_claims_count = session.exec(
        select(func.count(Claim.id)).where(Claim.status.in_(["submitted", "in_review"]))
    ).one()

    # Patients with pending charges
    patients_with_charges = session.exec(
        select(PendingCharge.patient_id, PendingCharge.patient_name, PendingCharge.patient_mrn, func.count(PendingCharge.id), func.sum(PendingCharge.total_price))
        .where(PendingCharge.status == "pending")
        .group_by(PendingCharge.patient_id, PendingCharge.patient_name, PendingCharge.patient_mrn)
        .order_by(desc(func.count(PendingCharge.id)))
        .limit(10)
    ).all()

    # Recent finalized invoices
    recent_invoices = session.exec(
        select(Invoice).order_by(col(Invoice.created_at).desc()).limit(8)
    ).all()

    ctx = get_ui_context(
        request,
        title="Billing & Cashier Desk",
        user=user,
        active_session=active_session,
        today_collections=today_collections,
        pending_charges_count=pending_charges_count,
        outstanding_dues=outstanding_dues,
        active_claims_count=active_claims_count,
        patients_with_charges=patients_with_charges,
        recent_invoices=recent_invoices,
    )
    return templates.TemplateResponse("modules/billing/index.html", ctx)


# =========================================================================
# 2. INVOICES DIRECTORY & HTMX TABLE
# =========================================================================

@router.get("/invoices", response_class=HTMLResponse)
def list_invoices(
    request: Request,
    status_filter: Optional[str] = Query(None),
    category_filter: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    query = select(Invoice)
    if status_filter:
        query = query.where(Invoice.status == status_filter)
    if category_filter:
        query = query.where(Invoice.patient_category == category_filter)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.where(
            or_(
                col(Invoice.invoice_no).ilike(term),
                col(Invoice.patient_name).ilike(term),
                col(Invoice.patient_mrn).ilike(term),
            )
        )

    invoices = session.exec(query.order_by(col(Invoice.created_at).desc()).limit(50)).all()

    ctx = get_ui_context(
        request,
        title="Patient Invoices",
        user=user,
        invoices=invoices,
        status_filter=status_filter,
        category_filter=category_filter,
        q=q,
    )
    return templates.TemplateResponse("modules/billing/invoices.html", ctx)


# =========================================================================
# 3. PATIENT BILLING WORKSPACE (PENDING CHARGES, DISCOUNT, SPLIT PAYMENT)
# =========================================================================

@router.get("/patient/{patient_id}", response_class=HTMLResponse)
def patient_billing_screen(
    request: Request,
    patient_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Interactive patient billing workspace with pending charges and payment options."""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    pending_charges = session.exec(
        select(PendingCharge).where(
            PendingCharge.patient_id == patient_id,
            PendingCharge.status == "pending",
        ).order_by(col(PendingCharge.created_at).desc())
    ).all()

    running_total = sum(ch.total_price for ch in pending_charges)

    # Active insurance policy
    policy = session.exec(
        select(InsurancePolicy).where(
            InsurancePolicy.patient_id == patient_id,
            InsurancePolicy.status == "active",
        )
    ).first()

    # Available deposits
    deposits = session.exec(
        select(Deposit).where(
            Deposit.patient_id == patient_id,
            Deposit.status.in_(["available", "partially_used"]),
        )
    ).all()
    available_deposit = sum(d.balance_amount for d in deposits)

    # Past invoices for this patient
    past_invoices = session.exec(
        select(Invoice).where(Invoice.patient_id == patient_id).order_by(col(Invoice.created_at).desc()).limit(10)
    ).all()

    # Active cash session
    active_session = get_active_cash_session(session, user.username)

    # Price lists & service catalog for manual addition
    price_lists = session.exec(select(PriceList).where(PriceList.is_active == True)).all()
    services = session.exec(select(ServiceCatalog).where(ServiceCatalog.is_active == True)).all()
    packages = session.exec(select(Package).where(Package.is_active == True)).all()

    ctx = get_ui_context(
        request,
        title=f"Billing - {patient.first_name} {patient.last_name}",
        user=user,
        patient=patient,
        pending_charges=pending_charges,
        running_total=running_total,
        policy=policy,
        deposits=deposits,
        available_deposit=available_deposit,
        past_invoices=past_invoices,
        active_session=active_session,
        price_lists=price_lists,
        services=services,
        packages=packages,
    )
    return templates.TemplateResponse("modules/billing/patient_billing.html", ctx)


@router.post("/patient/{patient_id}/invoice/create")
def create_invoice_endpoint(
    patient_id: int,
    charge_ids: List[int] = Form(...),
    price_list_id: Optional[int] = Form(None),
    patient_category: str = Form("general"),
    notes: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Converts selected pending charges into a new invoice."""
    try:
        invoice = create_invoice_from_pending_charges(
            session=session,
            patient_id=patient_id,
            pending_charge_ids=charge_ids,
            price_list_id=price_list_id,
            patient_category=patient_category,
            user_name=user.username,
            notes=notes,
        )
        return RedirectResponse(url=f"/billing/invoice/{invoice.id}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# =========================================================================
# 4. INVOICE DETAIL, MANUAL CHARGES, DISCOUNTS & FINALIZATION
# =========================================================================

@router.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def view_invoice(
    request: Request,
    invoice_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """View full invoice details, lines, and payment history."""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    lines = session.exec(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).all()
    payments = session.exec(select(Payment).where(Payment.invoice_id == invoice.id)).all()
    discounts = session.exec(select(Discount).where(Discount.invoice_id == invoice.id)).all()
    credit_note = session.get(CreditNote, invoice.credit_note_id) if invoice.credit_note_id else None

    # Available deposits for this patient
    deposits = session.exec(
        select(Deposit).where(
            Deposit.patient_id == invoice.patient_id,
            Deposit.status.in_(["available", "partially_used"]),
        )
    ).all()

    active_session = get_active_cash_session(session, user.username)
    services = session.exec(select(ServiceCatalog).where(ServiceCatalog.is_active == True)).all()
    packages = session.exec(select(Package).where(Package.is_active == True)).all()

    can_approve_discount = user.is_superuser or any(r.name in ("Manager", "BillingManager", "Admin") for r in user.roles)

    ctx = get_ui_context(
        request,
        title=f"Invoice {invoice.invoice_no}",
        user=user,
        invoice=invoice,
        lines=lines,
        payments=payments,
        discounts=discounts,
        credit_note=credit_note,
        deposits=deposits,
        active_session=active_session,
        services=services,
        packages=packages,
        can_approve_discount=can_approve_discount,
    )
    return templates.TemplateResponse("modules/billing/invoice_detail.html", ctx)


@router.post("/invoice/{invoice_id}/add-line")
def add_invoice_line_endpoint(
    invoice_id: int,
    service_code: str = Form(...),
    description: str = Form(...),
    category: str = Form("procedure"),
    quantity: float = Form(1.0),
    unit_price: float = Form(...),
    price_override_reason: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        add_manual_line(
            session=session,
            invoice_id=invoice_id,
            service_code=service_code,
            description=description,
            category=category,
            quantity=quantity,
            unit_price=unit_price,
            price_override_reason=price_override_reason,
            user_name=user.username,
        )
        return RedirectResponse(url=f"/billing/invoice/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/invoice/{invoice_id}/package/apply")
def apply_package_endpoint(
    invoice_id: int,
    package_id: int = Form(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        apply_package_to_invoice(session, invoice_id, package_id, user.username)
        return RedirectResponse(url=f"/billing/invoice/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/invoice/{invoice_id}/discount")
def apply_discount_endpoint(
    invoice_id: int,
    discount_type: str = Form(...),
    value: float = Form(...),
    reason: str = Form(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        apply_discount(session, invoice_id, discount_type, value, reason, user)
        return RedirectResponse(url=f"/billing/invoice/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/discounts/{discount_id}/approve")
def approve_discount_endpoint(
    discount_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Manager approves a pending discount."""
    if not (user.is_superuser or any(r.name in ("Manager", "BillingManager", "Admin") for r in user.roles)):
        raise HTTPException(status_code=403, detail="Manager approval permission required")
    try:
        discount = approve_discount(session, discount_id, user)
        return RedirectResponse(url=f"/billing/invoice/{discount.invoice_id}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/invoice/{invoice_id}/finalize")
def finalize_invoice_endpoint(
    invoice_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        finalize_invoice(session, invoice_id, user.username)
        return RedirectResponse(url=f"/billing/invoice/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/invoice/{invoice_id}/pay")
def process_payment_endpoint(
    invoice_id: int,
    amount: float = Form(...),
    payment_method: str = Form("cash"),
    cash_session_id: Optional[int] = Form(None),
    reference_number: Optional[str] = Form(None),
    is_split: bool = Form(False),
    cash_amount: float = Form(0.0),
    card_amount: float = Form(0.0),
    mobile_amount: float = Form(0.0),
    notes: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Records payment (single or split payment)."""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    split_details = None
    if is_split:
        split_details = []
        if cash_amount > 0:
            split_details.append({"method": "cash", "amount": cash_amount})
        if card_amount > 0:
            split_details.append({"method": "card", "amount": card_amount})
        if mobile_amount > 0:
            split_details.append({"method": "mobile_banking", "amount": mobile_amount})
        payment_method = "split"

    try:
        payment = record_payment(
            session=session,
            invoice_id=invoice.id,
            patient_id=invoice.patient_id,
            amount=amount,
            payment_method=payment_method,
            split_details=split_details,
            reference_number=reference_number,
            user_name=user.username,
            cash_session_id=cash_session_id,
            notes=notes,
        )
        return RedirectResponse(url=f"/billing/receipt/{payment.id}/print", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/invoice/{invoice_id}/deposit/apply")
def apply_deposit_endpoint(
    invoice_id: int,
    deposit_id: int = Form(...),
    amount: float = Form(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        apply_deposit_to_invoice(session, deposit_id, invoice_id, amount, user.username)
        return RedirectResponse(url=f"/billing/invoice/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/invoice/{invoice_id}/credit-note")
def issue_credit_note_endpoint(
    invoice_id: int,
    amount: float = Form(...),
    reason: str = Form(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        cn = issue_credit_note(session, invoice_id, amount, reason, user.username)
        return RedirectResponse(url=f"/billing/credit-note/{cn.id}/print", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# =========================================================================
# 5. PRINTABLES & DOWNLOADABLE PDFS (INVOICE, RECEIPT, CREDIT NOTE)
# =========================================================================

@router.get("/invoice/{invoice_id}/print", response_class=HTMLResponse)
def print_invoice_html(
    request: Request,
    invoice_id: int,
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    lines = session.exec(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).all()
    h_profile = settings_registry.get("hospital", "profile", {}) or {}
    billing_cfg = settings_registry.get("billing", "config", {}) or {}

    qr_data = f"INVOICE:{invoice.invoice_no}|MRN:{invoice.patient_mrn}|TOTAL:{invoice.total_amount}|STATUS:{invoice.status}"
    qr_uri = generate_qr_code_data_uri(qr_data)
    words = amount_to_words(invoice.total_amount, billing_cfg.get("currency_unit", "Dollars"), billing_cfg.get("currency_subunit", "Cents"))

    ctx = {
        "request": request,
        "invoice": invoice,
        "lines": lines,
        "hospital": h_profile,
        "qr_uri": qr_uri,
        "amount_in_words": words,
        "now": utc_now(),
    }
    return templates.TemplateResponse("modules/billing/invoice_print.html", ctx)


@router.get("/invoice/{invoice_id}/pdf")
def download_invoice_pdf(
    invoice_id: int,
    session: Session = Depends(get_session),
):
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    pdf_bytes = generate_invoice_pdf(session, invoice.id)
    filename = f"Invoice_{invoice.invoice_no}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/receipt/{payment_id}/print", response_class=HTMLResponse)
def print_receipt_html(
    request: Request,
    payment_id: int,
    session: Session = Depends(get_session),
):
    payment = session.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    patient = session.get(Patient, payment.patient_id)
    h_profile = settings_registry.get("hospital", "profile", {}) or {}
    billing_cfg = settings_registry.get("billing", "config", {}) or {}

    qr_data = f"RECEIPT:{payment.receipt_no}|PAYMENT:{payment.payment_no}|AMOUNT:{payment.amount}"
    qr_uri = generate_qr_code_data_uri(qr_data)
    words = amount_to_words(payment.amount, billing_cfg.get("currency_unit", "Dollars"), billing_cfg.get("currency_subunit", "Cents"))

    ctx = {
        "request": request,
        "payment": payment,
        "patient": patient,
        "hospital": h_profile,
        "qr_uri": qr_uri,
        "amount_in_words": words,
        "now": utc_now(),
    }
    return templates.TemplateResponse("modules/billing/receipt_print.html", ctx)


@router.get("/receipt/{payment_id}/pdf")
def download_receipt_pdf(
    payment_id: int,
    session: Session = Depends(get_session),
):
    pdf_bytes = generate_receipt_pdf(session, payment_id)
    payment = session.get(Payment, payment_id)
    filename = f"Receipt_{payment.receipt_no if payment else payment_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/credit-note/{credit_note_id}/print", response_class=HTMLResponse)
def print_credit_note_html(
    request: Request,
    credit_note_id: int,
    session: Session = Depends(get_session),
):
    cn = session.get(CreditNote, credit_note_id)
    if not cn:
        raise HTTPException(status_code=404, detail="Credit note not found")

    patient = session.get(Patient, cn.patient_id)
    h_profile = settings_registry.get("hospital", "profile", {}) or {}
    billing_cfg = settings_registry.get("billing", "config", {}) or {}

    qr_data = f"CREDIT_NOTE:{cn.credit_note_no}|ORIG_INV:{cn.original_invoice_no}|AMOUNT:{cn.amount}"
    qr_uri = generate_qr_code_data_uri(qr_data)
    words = amount_to_words(cn.amount, billing_cfg.get("currency_unit", "Dollars"), billing_cfg.get("currency_subunit", "Cents"))

    ctx = {
        "request": request,
        "cn": cn,
        "patient": patient,
        "hospital": h_profile,
        "qr_uri": qr_uri,
        "amount_in_words": words,
        "now": utc_now(),
    }
    return templates.TemplateResponse("modules/billing/credit_note_print.html", ctx)


@router.get("/credit-note/{credit_note_id}/pdf")
def download_credit_note_pdf(
    credit_note_id: int,
    session: Session = Depends(get_session),
):
    pdf_bytes = generate_credit_note_pdf(session, credit_note_id)
    cn = session.get(CreditNote, credit_note_id)
    filename = f"CreditNote_{cn.credit_note_no if cn else credit_note_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# =========================================================================
# 6. DEPOSITS DESK
# =========================================================================

@router.get("/deposits", response_class=HTMLResponse)
def deposits_desk(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    deposits = session.exec(select(Deposit).order_by(col(Deposit.created_at).desc()).limit(50)).all()
    active_session = get_active_cash_session(session, user.username)

    ctx = get_ui_context(
        request,
        title="Patient Deposits & Advances",
        user=user,
        deposits=deposits,
        active_session=active_session,
    )
    return templates.TemplateResponse("modules/billing/deposits.html", ctx)


@router.post("/deposits/collect")
def collect_deposit_endpoint(
    patient_id: int = Form(...),
    amount: float = Form(...),
    payment_method: str = Form("cash"),
    cash_session_id: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        collect_deposit(session, patient_id, amount, payment_method, user.username, cash_session_id, notes)
        return RedirectResponse(url="/billing/deposits", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# =========================================================================
# 7. CASH SESSIONS & TILL RECONCILIATION
# =========================================================================

@router.get("/sessions", response_class=HTMLResponse)
def cash_sessions_desk(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    active_session = get_active_cash_session(session, user.username)
    past_sessions = session.exec(select(CashSession).order_by(col(CashSession.opened_at).desc()).limit(20)).all()

    ctx = get_ui_context(
        request,
        title="Cash Sessions & Till Reconciliation",
        user=user,
        active_session=active_session,
        past_sessions=past_sessions,
    )
    return templates.TemplateResponse("modules/billing/sessions.html", ctx)


@router.post("/sessions/open")
def open_cash_session_endpoint(
    opening_balance: float = Form(0.0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        open_cash_session(session, user.username, opening_balance)
        return RedirectResponse(url="/billing/sessions", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/close")
def close_cash_session_endpoint(
    session_id: int = Form(...),
    actual_counted_cash: float = Form(...),
    variance_reason: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        close_cash_session(session, session_id, actual_counted_cash, variance_reason, user.username)
        return RedirectResponse(url="/billing/sessions", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# =========================================================================
# 8. PACKAGES & CLINICAL BUNDLES
# =========================================================================

@router.get("/packages", response_class=HTMLResponse)
def packages_directory(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    packages = session.exec(select(Package).order_by(col(Package.name))).all()
    ctx = get_ui_context(
        request,
        title="Packages & Clinical Bundles",
        user=user,
        packages=packages,
    )
    return templates.TemplateResponse("modules/billing/packages.html", ctx)


# =========================================================================
# 9. INSURANCE & CLAIMS MANAGEMENT
# =========================================================================

@router.get("/insurance", response_class=HTMLResponse)
def insurance_desk(
    request: Request,
    claim_status: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    providers = session.exec(select(InsuranceProvider).where(InsuranceProvider.is_active == True)).all()
    policies = session.exec(select(InsurancePolicy).order_by(col(InsurancePolicy.valid_to).desc()).limit(30)).all()

    claim_query = select(Claim)
    if claim_status:
        claim_query = claim_query.where(Claim.status == claim_status)
    claims = session.exec(claim_query.order_by(col(Claim.submitted_at).desc()).limit(50)).all()

    ctx = get_ui_context(
        request,
        title="Insurance & Claims Tracker",
        user=user,
        providers=providers,
        policies=policies,
        claims=claims,
        claim_status=claim_status,
    )
    return templates.TemplateResponse("modules/billing/insurance.html", ctx)


@router.post("/insurance/claims/{claim_id}/status")
def update_claim_status_endpoint(
    claim_id: int,
    claim_status: str = Form(...),
    approved_amount: float = Form(0.0),
    rejection_reason: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        update_claim_status(session, claim_id, claim_status, approved_amount, rejection_reason, user.username)
        return RedirectResponse(url="/billing/insurance", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# =========================================================================
# 10. PRICE LISTS DIRECTORY & EDITOR
# =========================================================================

@router.get("/pricelists", response_class=HTMLResponse)
def price_lists_directory(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    price_lists = session.exec(select(PriceList).order_by(col(PriceList.name))).all()
    services = session.exec(select(ServiceCatalog).where(ServiceCatalog.is_active == True)).all()

    ctx = get_ui_context(
        request,
        title="Tariffs & Price Lists",
        user=user,
        price_lists=price_lists,
        services=services,
    )
    return templates.TemplateResponse("modules/billing/pricelists.html", ctx)


@router.post("/pricelists/{price_list_id}/edit")
def edit_price_list_endpoint(
    price_list_id: int,
    discount_percentage: float = Form(0.0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    pl = session.get(PriceList, price_list_id)
    if not pl:
        raise HTTPException(status_code=404, detail="Price list not found")
    pl.discount_percentage = discount_percentage
    pl.updated_at = utc_now()
    session.add(pl)
    session.commit()
    return RedirectResponse(url="/billing/pricelists", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================================
# 11. FINANCIAL REPORTS & ANALYTICS (VIEW & CSV EXPORTS)
# =========================================================================

@router.get("/reports", response_class=HTMLResponse)
def financial_reports_view(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    # Aggregated revenue by department
    rev_by_dept = session.exec(
        select(InvoiceLine.category, func.sum(InvoiceLine.total_price), func.count(InvoiceLine.id))
        .group_by(InvoiceLine.category)
    ).all()

    # Outstanding dues
    total_dues = session.exec(
        select(func.sum(Invoice.balance_due)).where(Invoice.is_finalized == True, Invoice.balance_due > 0)
    ).one() or 0.0

    # Total collected
    total_collected = session.exec(
        select(func.sum(Payment.amount)).where(Payment.status == "completed")
    ).one() or 0.0

    # Total claims
    total_claims = session.exec(
        select(func.sum(Claim.claim_amount)).where(Claim.status.in_(["submitted", "in_review", "approved", "settled"]))
    ).one() or 0.0

    ctx = get_ui_context(
        request,
        title="Financial & Revenue Reports",
        user=user,
        rev_by_dept=rev_by_dept,
        total_dues=total_dues,
        total_collected=total_collected,
        total_claims=total_claims,
    )
    return templates.TemplateResponse("modules/billing/reports.html", ctx)


@router.get("/reports/{report_type}/csv")
def export_csv_report(
    report_type: str,
    session: Session = Depends(get_session),
):
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    if report_type == "revenue_by_department":
        csv_data = export_revenue_by_department_csv(session)
        filename = f"revenue_by_department_{now_str}.csv"
    elif report_type == "outstanding_dues":
        csv_data = export_outstanding_dues_csv(session)
        filename = f"outstanding_dues_{now_str}.csv"
    elif report_type == "collection_summary":
        csv_data = export_collection_summary_csv(session)
        filename = f"collection_summary_{now_str}.csv"
    elif report_type == "insurance_aging":
        csv_data = export_insurance_aging_csv(session)
        filename = f"insurance_aging_{now_str}.csv"
    else:
        raise HTTPException(status_code=400, detail="Invalid report type")

    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# =========================================================================
# 12. PATIENT 360 INTEGRATION & DASHBOARD WIDGET
# =========================================================================

@router.get("/patient/{patient_id}/summary", response_class=HTMLResponse)
def patient_billing_tab_partial(
    request: Request,
    patient_id: int,
    session: Session = Depends(get_session),
):
    """HTMX partial for the Patient 360 Billing tab."""
    patient = session.get(Patient, patient_id)
    if not patient:
        return HTMLResponse("<p class='text-muted text-xs'>Patient not found</p>")

    invoices = session.exec(
        select(Invoice).where(Invoice.patient_id == patient_id).order_by(col(Invoice.created_at).desc())
    ).all()

    deposits = session.exec(
        select(Deposit).where(Deposit.patient_id == patient_id).order_by(col(Deposit.created_at).desc())
    ).all()

    pending_charges = session.exec(
        select(PendingCharge).where(
            PendingCharge.patient_id == patient_id,
            PendingCharge.status == "pending",
        )
    ).all()

    total_due = sum(inv.balance_due for inv in invoices)
    total_deposit = sum(dep.balance_amount for dep in deposits)
    total_pending = sum(ch.total_price for ch in pending_charges)

    ctx = {
        "request": request,
        "patient": patient,
        "invoices": invoices,
        "deposits": deposits,
        "pending_charges": pending_charges,
        "total_due": total_due,
        "total_deposit": total_deposit,
        "total_pending": total_pending,
    }
    return templates.TemplateResponse("modules/billing/partials/patient_billing_tab.html", ctx)


@router.get("/widgets/collection", response_class=HTMLResponse)
def collection_widget(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_payments = session.exec(
        select(Payment).where(Payment.received_at >= today_start, Payment.status == "completed")
    ).all()
    today_collections = sum(p.amount for p in today_payments)
    active_session = get_active_cash_session(session, user.username)

    ctx = {
        "request": request,
        "today_collections": today_collections,
        "today_count": len(today_payments),
        "active_session": active_session,
    }
    return templates.TemplateResponse("modules/billing/widgets/collection.html", ctx)
