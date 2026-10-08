import base64
import csv
import io
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlmodel import Session, col, func, or_, select

from medicore.core.database import engine
from medicore.core.events import Event, event_bus
from medicore.core.models import AuditLog, User
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
from medicore.modules.patients.models import Patient

logger = logging.getLogger("medicore.billing.service")


# =========================================================================
# 1. UTILITY: AMOUNT IN WORDS & QR CODE GENERATION
# =========================================================================

def amount_to_words(amount: float, currency_unit: str = "Dollars", subunit: str = "Cents") -> str:
    """Converts a monetary figure into standard formal English words."""
    if amount is None or amount == 0:
        return f"Zero {currency_unit} Only"

    ones = [
        "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen",
    ]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def _convert_hundreds(n: int) -> str:
        res = []
        if n >= 100:
            res.append(ones[n // 100] + " Hundred")
            n %= 100
        if n >= 20:
            res.append(tens[n // 10])
            n %= 10
        if n > 0:
            res.append(ones[n])
        return " ".join(res)

    def _convert_integer(num: int) -> str:
        if num == 0:
            return "Zero"
        parts = []
        scales = [
            (1_000_000_000, "Billion"),
            (1_000_000, "Million"),
            (1_000, "Thousand"),
            (1, ""),
        ]
        rem = num
        for scale, unit in scales:
            if rem >= scale:
                chunk = rem // scale
                rem %= scale
                h_text = _convert_hundreds(chunk)
                if h_text:
                    if unit:
                        parts.append(f"{h_text} {unit}")
                    else:
                        parts.append(h_text)
        return " ".join(parts).strip()

    is_negative = amount < 0
    abs_amt = abs(amount)
    integer_part = int(abs_amt)
    fractional_part = int(round((abs_amt - integer_part) * 100))

    cur_unit = currency_unit[:-1] if integer_part == 1 and currency_unit.endswith("s") else currency_unit
    sub_unit = subunit[:-1] if fractional_part == 1 and subunit.endswith("s") else subunit

    int_words = _convert_integer(integer_part)
    result = f"{int_words} {cur_unit}"
    if fractional_part > 0:
        frac_words = _convert_hundreds(fractional_part)
        result += f" and {frac_words} {sub_unit}"
    result += " Only"

    return f"Negative {result}" if is_negative else result


def generate_qr_code_data_uri(content: str) -> str:
    """Generates a base64 encoded PNG data URI for a QR code."""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=2,
        )
        qr.add_data(content)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        b64_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{b64_str}"
    except Exception as e:
        logger.warning(f"Error generating QR code: {e}")
        return ""


def record_audit(session: Session, action: str, entity_type: str, entity_id: str, changes: Dict[str, Any], user_name: str = "system"):
    """Records an immutable audit trail entry for billing transactions."""
    audit = AuditLog(
        module="billing",
        entity=entity_type,
        entity_id=str(entity_id),
        action=action,
        changes=changes,
        username=user_name,
        timestamp=utc_now(),
    )
    session.add(audit)


# =========================================================================
# 2. EVENT-DRIVEN CHARGE CAPTURE (STRICTLY IDEMPOTENT)
# =========================================================================

def handle_encounter_completed(event: Event):
    """
    Subscribes to 'encounter.completed'.
    Captures consultation fee by doctor/department rate.
    Strictly idempotent using source_event_id.
    """
    payload = event.payload or {}
    encounter_id = payload.get("id")
    if not encounter_id:
        return

    source_event_id = f"encounter.completed:{encounter_id}"
    patient_id = payload.get("patient_id")
    patient_mrn = payload.get("patient_mrn", "UNKNOWN")
    patient_name = payload.get("patient_name", "Unknown Patient")
    doctor_name = payload.get("doctor_name", "Attending Physician")

    with Session(engine) as session:
        # Idempotency check: never double-bill!
        existing = session.exec(
            select(PendingCharge).where(PendingCharge.source_event_id == source_event_id)
        ).first()
        if existing:
            logger.info(f"Billing: Skipping duplicate encounter.completed charge for encounter {encounter_id}")
            return

        # Determine consultation rate from catalog
        consult_service = session.exec(
            select(ServiceCatalog).where(
                ServiceCatalog.category == "consultation",
                ServiceCatalog.is_active == True,
            )
        ).first()
        fee = consult_service.base_price if consult_service else 75.0
        service_code = consult_service.code if consult_service else "CONS-OPD"

        charge = PendingCharge(
            patient_id=patient_id,
            patient_mrn=patient_mrn,
            patient_name=patient_name,
            encounter_id=encounter_id,
            source_type="consultation",
            source_id=f"encounter:{encounter_id}",
            source_event_id=source_event_id,
            service_code=service_code,
            description=f"Outpatient Consultation - Dr. {doctor_name}",
            quantity=1.0,
            unit_price=fee,
            total_price=fee,
            status="pending",
            created_at=utc_now(),
        )
        session.add(charge)
        session.commit()
        logger.info(f"Billing: Captured pending charge {fee} for encounter {encounter_id}")


def handle_dispense_completed(event: Event):
    """
    Subscribes to 'dispense.completed'.
    Captures pharmacy medication lines at batch MRP.
    Strictly idempotent using source_event_id.
    """
    payload = event.payload or {}
    dispense_id = payload.get("dispense_id")
    if not dispense_id:
        return

    source_event_id = f"dispense.completed:{dispense_id}"
    patient_id = payload.get("patient_id") or 0
    patient_mrn = payload.get("patient_mrn", "UNKNOWN")
    patient_name = payload.get("patient_name", "Pharmacy Customer")
    dispense_no = payload.get("dispense_no", f"DISP-{dispense_id}")
    total_amount = float(payload.get("total_amount", 0.0))

    if total_amount <= 0:
        return

    with Session(engine) as session:
        # Idempotency check: never double-bill!
        existing = session.exec(
            select(PendingCharge).where(PendingCharge.source_event_id == source_event_id)
        ).first()
        if existing:
            logger.info(f"Billing: Skipping duplicate dispense.completed charge for dispense {dispense_id}")
            return

        charge = PendingCharge(
            patient_id=patient_id,
            patient_mrn=patient_mrn,
            patient_name=patient_name,
            source_type="pharmacy",
            source_id=f"dispense:{dispense_id}",
            source_event_id=source_event_id,
            service_code="PHARM-DISP",
            description=f"Pharmacy Dispensation ({dispense_no})",
            quantity=1.0,
            unit_price=total_amount,
            total_price=total_amount,
            status="pending",
            created_at=utc_now(),
        )
        session.add(charge)
        session.commit()
        logger.info(f"Billing: Captured pending charge {total_amount} for pharmacy dispense {dispense_id}")


def handle_lab_charge(event: Event):
    """
    Subscribes to 'lab.charge'.
    Captures laboratory test diagnostic lines.
    Strictly idempotent using source_event_id.
    """
    payload = event.payload or {}
    order_id = payload.get("order_id")
    if not order_id:
        return

    source_event_id = f"lab.charge:{order_id}"
    patient_id = payload.get("patient_id") or 0
    patient_mrn = payload.get("patient_mrn", "UNKNOWN")
    amount = float(payload.get("amount", 0.0))
    order_no = payload.get("order_no", f"LAB-{order_id}")
    description = payload.get("description", f"Laboratory Diagnostic Service ({order_no})")

    with Session(engine) as session:
        # Idempotency check: never double-bill!
        existing = session.exec(
            select(PendingCharge).where(PendingCharge.source_event_id == source_event_id)
        ).first()
        if existing:
            logger.info(f"Billing: Skipping duplicate lab.charge for lab order {order_id}")
            return

        # Look up patient name if missing
        patient = session.get(Patient, patient_id) if patient_id else None
        patient_name = f"{patient.first_name} {patient.last_name}" if patient else "Patient"

        charge = PendingCharge(
            patient_id=patient_id,
            patient_mrn=patient_mrn,
            patient_name=patient_name,
            source_type="laboratory",
            source_id=f"lab_order:{order_id}",
            source_event_id=source_event_id,
            service_code="LAB-TEST",
            description=description,
            quantity=1.0,
            unit_price=amount,
            total_price=amount,
            status="pending",
            created_at=utc_now(),
        )
        session.add(charge)
        session.commit()
        logger.info(f"Billing: Captured pending lab charge {amount} for lab order {order_id}")


def handle_order_placed(event: Event):
    """
    Subscribes to 'order.placed'.
    Captures radiology, imaging, or procedure orders into pending charges.
    Ignores type == 'lab' because lab charges are captured upon lab.charge to prevent double-billing!
    """
    payload = event.payload or {}
    order_id = payload.get("order_id")
    order_type = (payload.get("type") or "").lower()
    if not order_id or order_type == "lab":
        # Ignore lab orders here: laboratory module will emit lab.charge
        return

    source_event_id = f"order.placed:{order_id}"
    patient_id = payload.get("patient_id") or 0
    patient_mrn = payload.get("patient_mrn", "UNKNOWN")
    patient_name = payload.get("patient_name", "Patient")
    test_name = payload.get("test_name", "Clinical Diagnostic Procedure")

    with Session(engine) as session:
        existing = session.exec(
            select(PendingCharge).where(PendingCharge.source_event_id == source_event_id)
        ).first()
        if existing:
            return

        # Look up price in catalog
        service = session.exec(
            select(ServiceCatalog).where(
                or_(
                    col(ServiceCatalog.code).ilike(f"%{order_type}%"),
                    col(ServiceCatalog.name).ilike(f"%{test_name}%"),
                ),
                ServiceCatalog.is_active == True,
            )
        ).first()
        price = service.base_price if service else (120.0 if order_type in ("imaging", "radiology") else 90.0)
        code = service.code if service else f"PROC-{order_type.upper()}"

        charge = PendingCharge(
            patient_id=patient_id,
            patient_mrn=patient_mrn,
            patient_name=patient_name,
            encounter_id=payload.get("encounter_id"),
            source_type=order_type,
            source_id=f"order:{order_id}",
            source_event_id=source_event_id,
            service_code=code,
            description=f"Order: {test_name} ({order_type.title()})",
            quantity=1.0,
            unit_price=price,
            total_price=price,
            status="pending",
            created_at=utc_now(),
        )
        session.add(charge)
        session.commit()
        logger.info(f"Billing: Captured procedure charge {price} for {order_type} order {order_id}")


# =========================================================================
# 3. PRICING, DISCOUNTS & INVOICE RECALCULATION
# =========================================================================

def get_service_price(session: Session, service_code: str, price_list_id: Optional[int] = None, patient_category: str = "general") -> float:
    """Determines unit price using catalog, price list items, or category rate adjustment."""
    service = session.exec(
        select(ServiceCatalog).where(ServiceCatalog.code == service_code, ServiceCatalog.is_active == True)
    ).first()
    if not service:
        return 50.0  # Safe fallback

    base = service.base_price

    # Check for specific price list item override
    if price_list_id:
        item = session.exec(
            select(PriceListItem).where(
                PriceListItem.price_list_id == price_list_id,
                PriceListItem.service_id == service.id,
            )
        ).first()
        if item:
            return item.price

        # Check price list general discount/adjustment
        plist = session.get(PriceList, price_list_id)
        if plist and plist.discount_percentage:
            adj = base * (plist.discount_percentage / 100.0)
            return round(base - adj, 2)

    # Category adjustments if no specific price list
    if patient_category == "staff":
        return round(base * 0.80, 2)  # 20% staff concession
    elif patient_category == "vip":
        return round(base * 1.25, 2)  # 25% premium suite rate

    return base


def calculate_insurance_split(
    session: Session,
    lines: List[InvoiceLine],
    policy: Optional[InsurancePolicy],
) -> Tuple[float, float, float]:
    """
    Computes (insurance_covered, patient_payable, deductible_applied)
    considering co-pay %, per-service limits, and exclusions.
    """
    if not policy or policy.status != "active":
        total_p = sum(l.total_price for l in lines)
        return (0.0, total_p, 0.0)

    co_pay_pct = policy.co_pay_percentage / 100.0
    deductible = policy.deductible
    max_coverage = policy.max_coverage_limit
    exclusions = [e.lower() for e in (policy.exclusions or [])]
    per_limits = policy.per_service_limits or {}

    total_insured = 0.0
    total_patient = 0.0

    for line in lines:
        cat = (line.category or "").lower()
        price = line.total_price

        # Check exclusions
        if any(exc in cat or exc in line.description.lower() for exc in exclusions):
            total_patient += price
            line.patient_share = price
            line.insurance_share = 0.0
            continue

        # Check per-service limit
        limit = per_limits.get(cat, None)
        coverable = min(price, limit) if limit is not None else price
        excess = price - coverable

        pat_share = round(coverable * co_pay_pct + excess, 2)
        ins_share = round(price - pat_share, 2)

        total_insured += ins_share
        total_patient += pat_share

        line.patient_share = pat_share
        line.insurance_share = ins_share

    # Apply deductible to insurer portion
    deductible_applied = min(deductible, total_insured)
    total_insured = max(0.0, total_insured - deductible_applied)
    total_patient += deductible_applied

    # Cap at maximum coverage limit
    if total_insured > max_coverage:
        diff = total_insured - max_coverage
        total_insured = max_coverage
        total_patient += diff

    return (round(total_insured, 2), round(total_patient, 2), round(deductible_applied, 2))


def recalculate_invoice(session: Session, invoice: Invoice) -> Invoice:
    """Recalculates invoice subtotal, taxes, discounts, insurance, payments, deposits, and status."""
    lines = session.exec(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).all()
    subtotal = sum(line.total_price for line in lines)
    invoice.subtotal = round(subtotal, 2)

    # Approved discounts
    discounts = session.exec(
        select(Discount).where(
            Discount.invoice_id == invoice.id,
            Discount.is_approved == True,
            Discount.status == "applied",
        )
    ).all()
    discount_amt = sum(d.amount for d in discounts)
    invoice.discount_amount = round(discount_amt, 2)

    # Taxes from settings
    billing_cfg = settings_registry.get("billing", "config", {}) or {}
    tax_rate = float(billing_cfg.get("tax_rate", 5.0))
    tax_included = billing_cfg.get("tax_included", False)

    net_after_discount = max(0.0, invoice.subtotal - invoice.discount_amount)
    if tax_included or tax_rate <= 0:
        invoice.tax_amount = 0.0
        invoice.total_amount = round(net_after_discount, 2)
    else:
        invoice.tax_amount = round(net_after_discount * (tax_rate / 100.0), 2)
        invoice.total_amount = round(net_after_discount + invoice.tax_amount, 2)

    # Insurance calculations if patient is insured
    if invoice.patient_category == "insured":
        policy = session.exec(
            select(InsurancePolicy).where(
                InsurancePolicy.patient_id == invoice.patient_id,
                InsurancePolicy.status == "active",
            )
        ).first()
        ins_cov, pat_pay, _ = calculate_insurance_split(session, lines, policy)
        invoice.insurance_covered = ins_cov
        invoice.patient_payable = pat_pay
    else:
        invoice.insurance_covered = 0.0
        invoice.patient_payable = invoice.total_amount

    # Payments & Deposits
    payments = session.exec(
        select(Payment).where(
            Payment.invoice_id == invoice.id,
            Payment.status == "completed",
        )
    ).all()
    paid_total = sum(p.amount for p in payments)
    invoice.paid_amount = round(paid_total, 2)

    # Net patient balance due
    effective_due = invoice.patient_payable - invoice.paid_amount - invoice.deposit_applied
    invoice.balance_due = max(0.0, round(effective_due, 2))

    # Determine status
    if invoice.has_credit_note and invoice.status == "credit_note_issued":
        pass  # Keep credit note status
    elif invoice.is_finalized:
        if invoice.balance_due <= 0.001:
            invoice.status = "paid"
        elif (invoice.paid_amount + invoice.deposit_applied) > 0.001:
            invoice.status = "partially_paid"
        else:
            invoice.status = "finalized"
    else:
        invoice.status = "draft"

    invoice.updated_at = utc_now()
    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    return invoice


# =========================================================================
# 4. INVOICE LIFECYCLE, MANUAL LINES & CREDIT NOTES
# =========================================================================

def create_invoice_from_pending_charges(
    session: Session,
    patient_id: int,
    pending_charge_ids: List[int],
    price_list_id: Optional[int] = None,
    patient_category: str = "general",
    user_name: str = "cashier",
    notes: Optional[str] = None,
) -> Invoice:
    """Creates a new invoice from selected pending charges."""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise ValueError(f"Patient ID {patient_id} not found")

    invoice_no = settings_registry.generate_number("invoice", "INV", "INV-{YEAR}-{SEQ:5}", session)

    invoice = Invoice(
        invoice_no=invoice_no,
        patient_id=patient.id,
        patient_mrn=patient.mrn,
        patient_name=f"{patient.first_name} {patient.last_name}",
        price_list_id=price_list_id,
        patient_category=patient_category,
        status="draft",
        notes=notes,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(invoice)
    session.commit()
    session.refresh(invoice)

    # Convert pending charges into invoice lines
    charges = session.exec(
        select(PendingCharge).where(
            PendingCharge.id.in_(pending_charge_ids),
            PendingCharge.status == "pending",
        )
    ).all()

    for ch in charges:
        line = InvoiceLine(
            invoice_id=invoice.id,
            service_code=ch.service_code or "CHARGE",
            description=ch.description,
            category=ch.source_type,
            quantity=ch.quantity,
            unit_price=ch.unit_price,
            base_price=ch.unit_price,
            total_price=ch.total_price,
            pending_charge_id=ch.id,
            patient_share=ch.total_price,
        )
        session.add(line)

        ch.status = "invoiced"
        ch.invoice_id = invoice.id
        ch.invoiced_at = utc_now()
        session.add(ch)

    session.commit()
    return recalculate_invoice(session, invoice)


def add_manual_line(
    session: Session,
    invoice_id: int,
    service_code: str,
    description: str,
    category: str,
    quantity: float,
    unit_price: float,
    price_override_reason: Optional[str] = None,
    user_name: str = "cashier",
) -> InvoiceLine:
    """Adds a manual charge line to a draft invoice with price override audit."""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise ValueError("Invoice not found")
    if invoice.is_finalized:
        raise ValueError("Cannot edit finalized invoice. Issue a credit note for adjustments.")

    total = round(quantity * unit_price, 2)
    line = InvoiceLine(
        invoice_id=invoice.id,
        service_code=service_code,
        description=description,
        category=category,
        quantity=quantity,
        unit_price=unit_price,
        base_price=unit_price,
        price_override_reason=price_override_reason,
        total_price=total,
        patient_share=total,
    )
    session.add(line)
    session.commit()

    if price_override_reason:
        record_audit(
            session=session,
            action="PRICE_OVERRIDE",
            entity_type="InvoiceLine",
            entity_id=str(line.id),
            changes={"service_code": service_code, "unit_price": unit_price, "reason": price_override_reason},
            user_name=user_name,
        )

    recalculate_invoice(session, invoice)
    return line


def apply_discount(
    session: Session,
    invoice_id: int,
    discount_type: str,
    value: float,
    reason: str,
    user: User,
) -> Discount:
    """
    Applies discount or waiver.
    If discount exceeds configurable limit (e.g. 10% or $50), requires manager approval.
    """
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise ValueError("Invoice not found")
    if invoice.is_finalized:
        raise ValueError("Cannot modify finalized invoice")

    billing_cfg = settings_registry.get("billing", "config", {}) or {}
    pct_limit = float(billing_cfg.get("discount_approval_limit", 10.0))
    fixed_limit = float(billing_cfg.get("discount_fixed_approval_limit", 50.0))

    if discount_type == "percentage":
        amount = round(invoice.subtotal * (value / 100.0), 2)
        requires_approval = value > pct_limit
    elif discount_type == "waiver":
        amount = invoice.subtotal
        requires_approval = True
    else:  # fixed_amount
        amount = round(min(value, invoice.subtotal), 2)
        requires_approval = amount > fixed_limit

    # If user is manager / superuser, auto-approve
    is_manager = user.is_superuser or any(r.name in ("Manager", "BillingManager", "Admin") for r in user.roles)
    if is_manager:
        is_approved = True
        status = "applied"
        approved_by = user.username
        approved_at = utc_now()
    else:
        is_approved = not requires_approval
        status = "applied" if is_approved else "pending_approval"
        approved_by = user.username if is_approved else None
        approved_at = utc_now() if is_approved else None

    discount_no = settings_registry.generate_number("discount", "DSC", "DSC-{YEAR}-{SEQ:5}", session)
    discount = Discount(
        discount_no=discount_no,
        invoice_id=invoice.id,
        type=discount_type,
        value=value,
        amount=amount,
        reason=reason,
        requires_approval=requires_approval,
        is_approved=is_approved,
        approved_by=approved_by,
        approved_at=approved_at,
        status=status,
        applied_by=user.username,
        created_at=utc_now(),
    )
    session.add(discount)
    session.commit()

    record_audit(
        session=session,
        action="DISCOUNT_APPLIED" if is_approved else "DISCOUNT_REQUESTED",
        entity_type="Discount",
        entity_id=str(discount.id),
        changes={"invoice_id": invoice_id, "type": discount_type, "value": value, "amount": amount, "reason": reason, "status": status},
        user_name=user.username,
    )

    recalculate_invoice(session, invoice)
    return discount


def approve_discount(session: Session, discount_id: int, manager_user: User) -> Discount:
    """Manager approves a pending discount."""
    discount = session.get(Discount, discount_id)
    if not discount:
        raise ValueError("Discount not found")

    discount.is_approved = True
    discount.status = "applied"
    discount.approved_by = manager_user.username
    discount.approved_at = utc_now()
    session.add(discount)
    session.commit()

    record_audit(
        session=session,
        action="DISCOUNT_APPROVED",
        entity_type="Discount",
        entity_id=str(discount.id),
        changes={"status": "applied", "approved_by": manager_user.username},
        user_name=manager_user.username,
    )

    invoice = session.get(Invoice, discount.invoice_id)
    if invoice:
        recalculate_invoice(session, invoice)
    return discount


def finalize_invoice(session: Session, invoice_id: int, user_name: str = "cashier") -> Invoice:
    """
    Finalizes an invoice making it immutable.
    Emits 'invoice.finalized' event.
    Auto-creates insurance claim if patient is insured.
    """
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise ValueError("Invoice not found")
    if invoice.is_finalized:
        return invoice

    invoice.is_finalized = True
    invoice.finalized_at = utc_now()
    invoice.finalized_by = user_name
    recalculate_invoice(session, invoice)

    # Emit domain event
    event_bus.emit(
        "invoice.finalized",
        {
            "invoice_id": invoice.id,
            "invoice_no": invoice.invoice_no,
            "patient_id": invoice.patient_id,
            "patient_mrn": invoice.patient_mrn,
            "total_amount": invoice.total_amount,
            "finalized_by": user_name,
        },
        username=user_name,
    )

    record_audit(
        session=session,
        action="INVOICE_FINALIZED",
        entity_type="Invoice",
        entity_id=str(invoice.id),
        changes={"invoice_no": invoice.invoice_no, "total_amount": invoice.total_amount},
        user_name=user_name,
    )

    # Auto-create claim if insurance covered portion exists
    if invoice.insurance_covered > 0:
        policy = session.exec(
            select(InsurancePolicy).where(
                InsurancePolicy.patient_id == invoice.patient_id,
                InsurancePolicy.status == "active",
            )
        ).first()
        if policy:
            create_insurance_claim(session, invoice.id, policy.id, user_name)

    return invoice


def issue_credit_note(session: Session, invoice_id: int, amount: float, reason: str, user_name: str = "cashier") -> CreditNote:
    """
    Issues an immutable credit note for a finalized invoice correction.
    Never alters finalized invoice historical lines directly.
    """
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise ValueError("Invoice not found")

    credit_note_no = settings_registry.generate_number("credit_note", "CN", "CN-{YEAR}-{SEQ:5}", session)

    credit_note = CreditNote(
        credit_note_no=credit_note_no,
        invoice_id=invoice.id,
        original_invoice_no=invoice.invoice_no,
        patient_id=invoice.patient_id,
        patient_mrn=invoice.patient_mrn,
        amount=round(amount, 2),
        reason=reason,
        issued_by=user_name,
        issued_at=utc_now(),
        status="issued",
    )
    session.add(credit_note)
    session.flush()

    invoice.has_credit_note = True
    invoice.credit_note_id = credit_note.id
    invoice.balance_due = max(0.0, round(invoice.balance_due - amount, 2))
    if amount >= invoice.total_amount or invoice.balance_due <= 0.001:
        invoice.status = "credit_note_issued"
    session.add(invoice)
    session.commit()

    record_audit(
        session=session,
        action="CREDIT_NOTE_ISSUED",
        entity_type="CreditNote",
        entity_id=str(credit_note.id),
        changes={"credit_note_no": credit_note_no, "invoice_no": invoice.invoice_no, "amount": amount, "reason": reason},
        user_name=user_name,
    )
    return credit_note


# =========================================================================
# 5. PAYMENTS, SPLIT PAYMENTS & RECEIPTS
# =========================================================================

def record_payment(
    session: Session,
    invoice_id: Optional[int],
    patient_id: int,
    amount: float,
    payment_method: str = "cash",
    split_details: Optional[List[Dict[str, Any]]] = None,
    reference_number: Optional[str] = None,
    user_name: str = "cashier",
    cash_session_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Payment:
    """
    Records full, partial, or split payment.
    Emits 'payment.received' domain event.
    Updates cash session till balances.
    """
    payment_no = settings_registry.generate_number("payment", "PAY", "PAY-{YEAR}-{SEQ:5}", session)
    receipt_no = settings_registry.generate_number("receipt", "REC", "REC-{YEAR}-{SEQ:5}", session)

    patient = session.get(Patient, patient_id)
    patient_mrn = patient.mrn if patient else "UNKNOWN"

    payment = Payment(
        payment_no=payment_no,
        receipt_no=receipt_no,
        invoice_id=invoice_id,
        patient_id=patient_id,
        patient_mrn=patient_mrn,
        cash_session_id=cash_session_id,
        amount=round(amount, 2),
        payment_method=payment_method,
        payment_split_details=split_details or [],
        reference_number=reference_number,
        collected_by=user_name,
        received_at=utc_now(),
        status="completed",
        notes=notes,
    )
    session.add(payment)
    session.commit()

    # Update till session if active
    if cash_session_id:
        cs = session.get(CashSession, cash_session_id)
        if cs and cs.status == "open":
            if split_details:
                for s in split_details:
                    m = s.get("method", "").lower()
                    a = float(s.get("amount", 0.0))
                    if m == "cash":
                        cs.cash_collected += a
                    elif m == "card":
                        cs.card_collected += a
                    elif m == "mobile_banking":
                        cs.mobile_banking_collected += a
            else:
                if payment_method == "cash":
                    cs.cash_collected += amount
                elif payment_method == "card":
                    cs.card_collected += amount
                elif payment_method == "mobile_banking":
                    cs.mobile_banking_collected += amount
            cs.total_expected_cash = cs.opening_balance + cs.cash_collected + cs.deposit_collected - cs.cash_refunded
            session.add(cs)
            session.commit()

    # If attached to invoice, recalculate
    if invoice_id:
        invoice = session.get(Invoice, invoice_id)
        if invoice:
            recalculate_invoice(session, invoice)

    event_bus.emit(
        "payment.received",
        {
            "payment_id": payment.id,
            "payment_no": payment.payment_no,
            "receipt_no": payment.receipt_no,
            "invoice_id": invoice_id,
            "patient_id": patient_id,
            "amount": payment.amount,
            "method": payment_method,
            "collected_by": user_name,
        },
        username=user_name,
    )

    record_audit(
        session=session,
        action="PAYMENT_RECEIVED",
        entity_type="Payment",
        entity_id=str(payment.id),
        changes={"receipt_no": receipt_no, "amount": amount, "method": payment_method},
        user_name=user_name,
    )
    return payment


# =========================================================================
# 6. DEPOSITS & ADVANCE PAYMENTS
# =========================================================================

def collect_deposit(
    session: Session,
    patient_id: int,
    amount: float,
    payment_method: str = "cash",
    user_name: str = "cashier",
    cash_session_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Deposit:
    """Collects advance patient deposit."""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise ValueError("Patient not found")

    deposit_no = settings_registry.generate_number("deposit", "DEP", "DEP-{YEAR}-{SEQ:5}", session)

    deposit = Deposit(
        deposit_no=deposit_no,
        patient_id=patient.id,
        patient_mrn=patient.mrn,
        patient_name=f"{patient.first_name} {patient.last_name}",
        amount=round(amount, 2),
        used_amount=0.0,
        balance_amount=round(amount, 2),
        payment_method=payment_method,
        cash_session_id=cash_session_id,
        status="available",
        collected_by=user_name,
        notes=notes,
        created_at=utc_now(),
    )
    session.add(deposit)
    session.commit()

    if cash_session_id and payment_method == "cash":
        cs = session.get(CashSession, cash_session_id)
        if cs and cs.status == "open":
            cs.deposit_collected += amount
            cs.total_expected_cash = cs.opening_balance + cs.cash_collected + cs.deposit_collected - cs.cash_refunded
            session.add(cs)
            session.commit()

    record_audit(
        session=session,
        action="DEPOSIT_COLLECTED",
        entity_type="Deposit",
        entity_id=str(deposit.id),
        changes={"deposit_no": deposit_no, "amount": amount, "patient_mrn": patient.mrn},
        user_name=user_name,
    )
    return deposit


def apply_deposit_to_invoice(
    session: Session,
    deposit_id: int,
    invoice_id: int,
    amount: float,
    user_name: str = "cashier",
) -> Invoice:
    """Adjusts an available deposit against an invoice balance."""
    deposit = session.get(Deposit, deposit_id)
    invoice = session.get(Invoice, invoice_id)
    if not deposit or not invoice:
        raise ValueError("Deposit or Invoice not found")

    if amount > deposit.balance_amount:
        raise ValueError(f"Requested adjustment ${amount} exceeds available deposit balance ${deposit.balance_amount}")

    adj = round(amount, 2)
    deposit.used_amount += adj
    deposit.balance_amount -= adj
    deposit.status = "exhausted" if deposit.balance_amount <= 0.01 else "partially_used"
    session.add(deposit)

    invoice.deposit_applied += adj
    session.add(invoice)
    session.commit()

    record_audit(
        session=session,
        action="DEPOSIT_APPLIED_TO_INVOICE",
        entity_type="Invoice",
        entity_id=str(invoice.id),
        changes={"deposit_no": deposit.deposit_no, "amount": adj},
        user_name=user_name,
    )
    return recalculate_invoice(session, invoice)


def refund_deposit_balance(
    session: Session,
    deposit_id: int,
    amount: float,
    reason: str,
    user_name: str = "cashier",
) -> Refund:
    """Refunds unused deposit balance to patient."""
    deposit = session.get(Deposit, deposit_id)
    if not deposit:
        raise ValueError("Deposit not found")
    if amount > deposit.balance_amount:
        raise ValueError("Refund amount exceeds available deposit balance")

    deposit.balance_amount -= round(amount, 2)
    if deposit.balance_amount <= 0.01:
        deposit.status = "refunded"
    session.add(deposit)

    refund = request_refund(
        session=session,
        patient_id=deposit.patient_id,
        amount=amount,
        reason=reason,
        refund_method=deposit.payment_method,
        deposit_id=deposit.id,
        user_name=user_name,
    )
    return refund


# =========================================================================
# 7. REFUNDS & MANAGER APPROVAL
# =========================================================================

def request_refund(
    session: Session,
    patient_id: int,
    amount: float,
    reason: str,
    refund_method: str = "cash",
    invoice_id: Optional[int] = None,
    payment_id: Optional[int] = None,
    deposit_id: Optional[int] = None,
    user_name: str = "cashier",
) -> Refund:
    """Initiates a refund request requiring manager approval."""
    patient = session.get(Patient, patient_id)
    patient_mrn = patient.mrn if patient else "UNKNOWN"
    patient_name = f"{patient.first_name} {patient.last_name}" if patient else "Patient"

    refund_no = settings_registry.generate_number("refund", "REF", "REF-{YEAR}-{SEQ:5}", session)

    refund = Refund(
        refund_no=refund_no,
        invoice_id=invoice_id,
        payment_id=payment_id,
        deposit_id=deposit_id,
        patient_id=patient_id,
        patient_mrn=patient_mrn,
        patient_name=patient_name,
        amount=round(amount, 2),
        reason=reason,
        refund_method=refund_method,
        status="pending",
        requested_by=user_name,
        created_at=utc_now(),
    )
    session.add(refund)
    session.commit()

    record_audit(
        session=session,
        action="REFUND_REQUESTED",
        entity_type="Refund",
        entity_id=str(refund.id),
        changes={"refund_no": refund_no, "amount": amount, "reason": reason},
        user_name=user_name,
    )
    return refund


def approve_refund(session: Session, refund_id: int, manager_user: User, cash_session_id: Optional[int] = None) -> Refund:
    """Manager approves and processes a refund."""
    refund = session.get(Refund, refund_id)
    if not refund:
        raise ValueError("Refund not found")

    refund.status = "completed"
    refund.approved_by = manager_user.username
    refund.approved_at = utc_now()
    refund.processed_by = manager_user.username
    refund.processed_at = utc_now()
    session.add(refund)
    session.commit()

    # Deduct cash refund from till if cash session active
    if cash_session_id and refund.refund_method == "cash":
        cs = session.get(CashSession, cash_session_id)
        if cs and cs.status == "open":
            cs.cash_refunded += refund.amount
            cs.total_expected_cash = cs.opening_balance + cs.cash_collected + cs.deposit_collected - cs.cash_refunded
            session.add(cs)
            session.commit()

    event_bus.emit(
        "refund.issued",
        {
            "refund_id": refund.id,
            "refund_no": refund.refund_no,
            "patient_id": refund.patient_id,
            "amount": refund.amount,
            "reason": refund.reason,
            "approved_by": manager_user.username,
        },
        username=manager_user.username,
    )

    record_audit(
        session=session,
        action="REFUND_APPROVED",
        entity_type="Refund",
        entity_id=str(refund.id),
        changes={"status": "completed", "amount": refund.amount, "approved_by": manager_user.username},
        user_name=manager_user.username,
    )
    return refund


# =========================================================================
# 8. CASHIER TILL WORKFLOW & RECONCILIATION
# =========================================================================

def open_cash_session(session: Session, cashier_username: str, opening_balance: float = 0.0) -> CashSession:
    """Opens a cashier cash drawer session."""
    active = session.exec(
        select(CashSession).where(
            CashSession.cashier_username == cashier_username,
            CashSession.status == "open",
        )
    ).first()
    if active:
        return active

    session_no = settings_registry.generate_number("session", "CS", "CS-{YEAR}-{SEQ:5}", session)

    cs = CashSession(
        session_no=session_no,
        cashier_username=cashier_username,
        opened_at=utc_now(),
        opening_balance=round(opening_balance, 2),
        total_expected_cash=round(opening_balance, 2),
        status="open",
    )
    session.add(cs)
    session.commit()
    session.refresh(cs)

    record_audit(
        session=session,
        action="CASH_SESSION_OPENED",
        entity_type="CashSession",
        entity_id=str(cs.id),
        changes={"session_no": session_no, "opening_balance": opening_balance},
        user_name=cashier_username,
    )
    return cs


def get_active_cash_session(session: Session, cashier_username: str) -> Optional[CashSession]:
    """Retrieves current open cash session for cashier."""
    return session.exec(
        select(CashSession).where(
            CashSession.cashier_username == cashier_username,
            CashSession.status == "open",
        )
    ).first()


def close_cash_session(
    session: Session,
    session_id: int,
    actual_counted_cash: float,
    variance_reason: Optional[str] = None,
    user_name: str = "cashier",
) -> CashSession:
    """Reconciles till, calculates variance, and closes cashier session."""
    cs = session.get(CashSession, session_id)
    if not cs or cs.status != "open":
        raise ValueError("Cash session is not open")

    expected = round(cs.opening_balance + cs.cash_collected + cs.deposit_collected - cs.cash_refunded, 2)
    variance = round(actual_counted_cash - expected, 2)

    cs.closing_balance = round(actual_counted_cash, 2)
    cs.actual_counted_cash = round(actual_counted_cash, 2)
    cs.total_expected_cash = expected
    cs.variance = variance
    cs.variance_reason = variance_reason
    cs.closed_at = utc_now()
    cs.status = "closed"

    session.add(cs)
    session.commit()
    session.refresh(cs)

    record_audit(
        session=session,
        action="CASH_SESSION_CLOSED",
        entity_type="CashSession",
        entity_id=str(cs.id),
        changes={
            "session_no": cs.session_no,
            "expected": expected,
            "counted": actual_counted_cash,
            "variance": variance,
            "variance_reason": variance_reason,
        },
        user_name=user_name,
    )
    return cs


# =========================================================================
# 9. PACKAGES & CLINICAL BUNDLES
# =========================================================================

def apply_package_to_invoice(session: Session, invoice_id: int, package_id: int, user_name: str = "cashier") -> Invoice:
    """Applies a package to an invoice; excess usage billed separately."""
    invoice = session.get(Invoice, invoice_id)
    pkg = session.get(Package, package_id)
    if not invoice or not pkg:
        raise ValueError("Invoice or Package not found")
    if invoice.is_finalized:
        raise ValueError("Cannot modify finalized invoice")

    line = InvoiceLine(
        invoice_id=invoice.id,
        service_code=pkg.code,
        description=f"Package: {pkg.name}",
        category="package",
        quantity=1.0,
        unit_price=pkg.package_price,
        base_price=pkg.package_price,
        total_price=pkg.package_price,
        patient_share=pkg.package_price,
    )
    session.add(line)
    session.commit()

    record_audit(
        session=session,
        action="PACKAGE_APPLIED",
        entity_type="Invoice",
        entity_id=str(invoice.id),
        changes={"package_code": pkg.code, "price": pkg.package_price},
        user_name=user_name,
    )
    return recalculate_invoice(session, invoice)


# =========================================================================
# 10. INSURANCE POLICIES & CLAIMS MANAGEMENT
# =========================================================================

def create_insurance_claim(session: Session, invoice_id: int, policy_id: int, user_name: str = "cashier") -> Claim:
    """Creates an insurance reimbursement claim for an invoice."""
    invoice = session.get(Invoice, invoice_id)
    policy = session.get(InsurancePolicy, policy_id)
    if not invoice or not policy:
        raise ValueError("Invoice or Insurance Policy not found")

    claim_no = settings_registry.generate_number("claim", "CLM", "CLM-{YEAR}-{SEQ:5}", session)

    claim = Claim(
        claim_no=claim_no,
        invoice_id=invoice.id,
        patient_id=invoice.patient_id,
        patient_mrn=invoice.patient_mrn,
        policy_id=policy.id,
        provider_id=policy.provider_id,
        provider_name=policy.provider_name,
        claim_amount=invoice.insurance_covered,
        patient_co_pay=invoice.patient_payable,
        status="submitted",
        submitted_at=utc_now(),
    )
    session.add(claim)
    session.commit()

    record_audit(
        session=session,
        action="INSURANCE_CLAIM_SUBMITTED",
        entity_type="Claim",
        entity_id=str(claim.id),
        changes={"claim_no": claim_no, "amount": claim.claim_amount, "insurer": policy.provider_name},
        user_name=user_name,
    )
    return claim


def update_claim_status(
    session: Session,
    claim_id: int,
    status: str,
    approved_amount: float = 0.0,
    rejection_reason: Optional[str] = None,
    user_name: str = "cashier",
) -> Claim:
    """Updates claim status (submitted -> approved / rejected / settled)."""
    claim = session.get(Claim, claim_id)
    if not claim:
        raise ValueError("Claim not found")

    claim.status = status
    if status == "approved":
        claim.approved_amount = approved_amount
        claim.adjudicated_at = utc_now()
    elif status == "rejected":
        claim.rejection_reason = rejection_reason
        claim.adjudicated_at = utc_now()
    elif status == "settled":
        claim.settled_at = utc_now()

    session.add(claim)
    session.commit()

    record_audit(
        session=session,
        action="CLAIM_STATUS_UPDATED",
        entity_type="Claim",
        entity_id=str(claim.id),
        changes={"status": status, "approved_amount": approved_amount, "rejection_reason": rejection_reason},
        user_name=user_name,
    )
    return claim


# =========================================================================
# 11. PDF GENERATION: INVOICE, RECEIPT & CREDIT NOTE
# =========================================================================

def generate_invoice_pdf(session: Session, invoice_id: int) -> bytes:
    """Generates a professional branded ReportLab PDF invoice."""
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise ValueError("Invoice not found")

    lines = session.exec(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).all()
    h_profile = settings_registry.get("hospital", "profile", {}) or {}
    billing_cfg = settings_registry.get("billing", "config", {}) or {}

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    story = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "InvTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0d9488"),
    )
    meta_style = ParagraphStyle(
        "InvMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#475569"),
    )
    h_style = ParagraphStyle(
        "InvH",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#0f172a"),
    )
    b_style = ParagraphStyle(
        "InvB",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1e293b"),
    )

    # Header with Branding
    h_name = h_profile.get("name", "MediCore Central Hospital")
    h_addr = h_profile.get("address", "450 Medical Center Blvd")
    h_phone = h_profile.get("phone", "+1 (555) 019-2834")

    # Generate verification QR Code image
    qr_data = f"INVOICE:{invoice.invoice_no}|MRN:{invoice.patient_mrn}|TOTAL:{invoice.total_amount}|STATUS:{invoice.status}"
    qr_img = None
    try:
        qr = qrcode.QRCode(box_size=3, border=1)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
        b_io = io.BytesIO()
        img.save(b_io, format="PNG")
        b_io.seek(0)
        qr_img = RLImage(b_io, width=54, height=54)
    except Exception:
        pass

    header_data = [
        [
            Paragraph(f"<b>{h_name}</b><br/>{h_addr}<br/>Tel: {h_phone}", meta_style),
            Paragraph("<b>OFFICIAL INVOICE</b>", title_style),
            qr_img if qr_img else Paragraph("", meta_style),
        ]
    ]
    t_header = Table(header_data, colWidths=[240, 230, 70])
    t_header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
    ]))
    story.append(t_header)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#0d9488"), spaceAfter=10))

    # Patient & Invoice Meta
    p_info = f"<b>Patient:</b> {invoice.patient_name}<br/><b>MRN:</b> {invoice.patient_mrn}<br/><b>Category:</b> {invoice.patient_category.title()}"
    inv_info = f"<b>Invoice No:</b> {invoice.invoice_no}<br/><b>Date:</b> {invoice.created_at.strftime('%Y-%m-%d %H:%M')}<br/><b>Status:</b> {invoice.status.upper()}"
    meta_table = Table([[Paragraph(p_info, meta_style), Paragraph(inv_info, meta_style)]], colWidths=[270, 270])
    meta_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(meta_table)
    story.append(Spacer(1, 12))

    # Items Table
    table_data = [[
        Paragraph("#", h_style),
        Paragraph("Service / Item Description", h_style),
        Paragraph("Qty", h_style),
        Paragraph("Unit Price", h_style),
        Paragraph("Total", h_style),
    ]]
    for idx, l in enumerate(lines, 1):
        table_data.append([
            Paragraph(str(idx), b_style),
            Paragraph(f"<b>{l.description}</b><br/><font color='#64748b'>Code: {l.service_code}</font>", b_style),
            Paragraph(str(l.quantity), b_style),
            Paragraph(f"${l.unit_price:.2f}", b_style),
            Paragraph(f"${l.total_price:.2f}", b_style),
        ])

    items_table = Table(table_data, colWidths=[30, 270, 45, 95, 100])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # Summary Totals Table
    sum_data = [
        ["Subtotal:", f"${invoice.subtotal:.2f}"],
        ["Discount:", f"-${invoice.discount_amount:.2f}"],
        ["Tax (VAT/GST):", f"${invoice.tax_amount:.2f}"],
        ["Total Billed:", f"${invoice.total_amount:.2f}"],
        ["Insurance Covered:", f"${invoice.insurance_covered:.2f}"],
        ["Deposit Adjusted:", f"-${invoice.deposit_applied:.2f}"],
        ["Amount Paid:", f"${invoice.paid_amount:.2f}"],
        ["Balance Due:", f"${invoice.balance_due:.2f}"],
    ]
    t_sum = Table(sum_data, colWidths=[400, 140])
    t_sum.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, -1), (-1, -1), colors.HexColor("#dc2626") if invoice.balance_due > 0 else colors.HexColor("#16a34a")),
        ("LINEABOVE", (0, 3), (-1, 3), 1, colors.HexColor("#94a3b8")),
        ("LINEABOVE", (0, -1), (-1, -1), 1.5, colors.HexColor("#0f172a")),
    ]))
    story.append(t_sum)
    story.append(Spacer(1, 10))

    # Amount in words
    words = amount_to_words(invoice.total_amount, billing_cfg.get("currency_unit", "Dollars"), billing_cfg.get("currency_subunit", "Cents"))
    story.append(Paragraph(f"<b>Amount in Words:</b> {words}", meta_style))
    story.append(Spacer(1, 20))

    # Signatures
    sig_data = [
        [
            Paragraph("Prepared By: ___________________", meta_style),
            Paragraph("Authorized Signatory: ___________________", meta_style),
        ]
    ]
    t_sig = Table(sig_data, colWidths=[270, 270])
    story.append(t_sig)

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def generate_receipt_pdf(session: Session, payment_id: int) -> bytes:
    """Generates a professional ReportLab Money Receipt PDF."""
    payment = session.get(Payment, payment_id)
    if not payment:
        raise ValueError("Payment not found")

    h_profile = settings_registry.get("hospital", "profile", {}) or {}
    billing_cfg = settings_registry.get("billing", "config", {}) or {}

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    story = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "RecTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0d9488"),
    )
    meta_style = ParagraphStyle(
        "RecMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#475569"),
    )
    body_style = ParagraphStyle(
        "RecB",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#1e293b"),
    )

    h_name = h_profile.get("name", "MediCore Central Hospital")
    h_addr = h_profile.get("address", "450 Medical Center Blvd")
    h_phone = h_profile.get("phone", "+1 (555) 019-2834")

    # QR Code
    qr_data = f"RECEIPT:{payment.receipt_no}|PAYMENT:{payment.payment_no}|AMOUNT:{payment.amount}|MRN:{payment.patient_mrn}"
    qr_img = None
    try:
        qr = qrcode.QRCode(box_size=3, border=1)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
        b_io = io.BytesIO()
        img.save(b_io, format="PNG")
        b_io.seek(0)
        qr_img = RLImage(b_io, width=54, height=54)
    except Exception:
        pass

    header_data = [
        [
            Paragraph(f"<b>{h_name}</b><br/>{h_addr}<br/>Tel: {h_phone}", meta_style),
            Paragraph("<b>MONEY RECEIPT</b>", title_style),
            qr_img if qr_img else Paragraph("", meta_style),
        ]
    ]
    t_header = Table(header_data, colWidths=[240, 230, 70])
    t_header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
    ]))
    story.append(t_header)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#0d9488"), spaceAfter=12))

    patient = session.get(Patient, payment.patient_id)
    pat_name = f"{patient.first_name} {patient.last_name}" if patient else "Patient"

    info_data = [
        [Paragraph(f"<b>Receipt No:</b> {payment.receipt_no}", body_style), Paragraph(f"<b>Date:</b> {payment.received_at.strftime('%Y-%m-%d %H:%M')}", body_style)],
        [Paragraph(f"<b>Patient:</b> {pat_name}", body_style), Paragraph(f"<b>MRN:</b> {payment.patient_mrn}", body_style)],
        [Paragraph(f"<b>Payment Method:</b> {payment.payment_method.replace('_', ' ').title()}", body_style), Paragraph(f"<b>Cashier:</b> {payment.collected_by}", body_style)],
        [Paragraph(f"<b>Payment No:</b> {payment.payment_no}", body_style), Paragraph(f"<b>Ref / Slip #:</b> {payment.reference_number or 'N/A'}", body_style)],
    ]
    t_info = Table(info_data, colWidths=[270, 270])
    t_info.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
    ]))
    story.append(t_info)
    story.append(Spacer(1, 14))

    # Payment Amount Box
    amt_box = [
        [
            Paragraph("<b>AMOUNT RECEIVED:</b>", ParagraphStyle("AB", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=12, leading=15)),
            Paragraph(f"<b>${payment.amount:.2f}</b>", ParagraphStyle("AB2", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=14, leading=16, textColor=colors.HexColor("#0d9488"))),
        ]
    ]
    t_amt = Table(amt_box, colWidths=[360, 180])
    t_amt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0fdfa")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#0d9488")),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t_amt)
    story.append(Spacer(1, 10))

    words = amount_to_words(payment.amount, billing_cfg.get("currency_unit", "Dollars"), billing_cfg.get("currency_subunit", "Cents"))
    story.append(Paragraph(f"<b>In Words:</b> {words}", meta_style))
    story.append(Spacer(1, 30))

    # Signature
    sig_data = [
        [
            Paragraph("Customer Acknowledged", meta_style),
            Paragraph("Cashier Signature: ___________________", meta_style),
        ]
    ]
    t_sig = Table(sig_data, colWidths=[270, 270])
    story.append(t_sig)

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def generate_credit_note_pdf(session: Session, credit_note_id: int) -> bytes:
    """Generates an official ReportLab Credit Note PDF."""
    cn = session.get(CreditNote, credit_note_id)
    if not cn:
        raise ValueError("Credit note not found")

    h_profile = settings_registry.get("hospital", "profile", {}) or {}
    billing_cfg = settings_registry.get("billing", "config", {}) or {}

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    story = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "CnTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#b91c1c"),
    )
    meta_style = ParagraphStyle(
        "CnMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#475569"),
    )
    body_style = ParagraphStyle(
        "CnB",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#1e293b"),
    )

    h_name = h_profile.get("name", "MediCore Central Hospital")
    h_addr = h_profile.get("address", "450 Medical Center Blvd")
    h_phone = h_profile.get("phone", "+1 (555) 019-2834")

    qr_data = f"CREDIT_NOTE:{cn.credit_note_no}|ORIG_INV:{cn.original_invoice_no}|AMOUNT:{cn.amount}|MRN:{cn.patient_mrn}"
    qr_img = None
    try:
        qr = qrcode.QRCode(box_size=3, border=1)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
        b_io = io.BytesIO()
        img.save(b_io, format="PNG")
        b_io.seek(0)
        qr_img = RLImage(b_io, width=54, height=54)
    except Exception:
        pass

    header_data = [
        [
            Paragraph(f"<b>{h_name}</b><br/>{h_addr}<br/>Tel: {h_phone}", meta_style),
            Paragraph("<b>CREDIT NOTE</b>", title_style),
            qr_img if qr_img else Paragraph("", meta_style),
        ]
    ]
    t_header = Table(header_data, colWidths=[240, 230, 70])
    t_header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
    ]))
    story.append(t_header)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#b91c1c"), spaceAfter=12))

    patient = session.get(Patient, cn.patient_id)
    pat_name = f"{patient.first_name} {patient.last_name}" if patient else "Patient"

    info_data = [
        [Paragraph(f"<b>Credit Note #:</b> {cn.credit_note_no}", body_style), Paragraph(f"<b>Date:</b> {cn.issued_at.strftime('%Y-%m-%d %H:%M')}", body_style)],
        [Paragraph(f"<b>Original Invoice #:</b> {cn.original_invoice_no}", body_style), Paragraph(f"<b>Issued By:</b> {cn.issued_by}", body_style)],
        [Paragraph(f"<b>Patient:</b> {pat_name}", body_style), Paragraph(f"<b>MRN:</b> {cn.patient_mrn}", body_style)],
        [Paragraph(f"<b>Adjustment Reason:</b> {cn.reason}", body_style), Paragraph(f"<b>Credit Amount:</b> ${cn.amount:.2f}", body_style)],
    ]
    t_info = Table(info_data, colWidths=[270, 270])
    t_info.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
    ]))
    story.append(t_info)
    story.append(Spacer(1, 14))

    words = amount_to_words(cn.amount, billing_cfg.get("currency_unit", "Dollars"), billing_cfg.get("currency_subunit", "Cents"))
    story.append(Paragraph(f"<b>Credit Amount in Words:</b> {words}", meta_style))
    story.append(Spacer(1, 30))

    sig_data = [
        [
            Paragraph("Billing Supervisor: ___________________", meta_style),
            Paragraph("Finance Manager: ___________________", meta_style),
        ]
    ]
    t_sig = Table(sig_data, colWidths=[270, 270])
    story.append(t_sig)

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


# =========================================================================
# 12. FINANCIAL REPORTS & ANALYTICAL EXPORTS (CSV)
# =========================================================================

def export_revenue_by_department_csv(session: Session) -> str:
    """Exports revenue aggregated by department as CSV."""
    lines = session.exec(
        select(InvoiceLine.category, func.sum(InvoiceLine.total_price), func.count(InvoiceLine.id))
        .group_by(InvoiceLine.category)
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Department / Category", "Item Count", "Total Billed Revenue ($)"])
    for cat, total, count in lines:
        writer.writerow([cat.title() if cat else "General", count, f"{total:.2f}"])
    return output.getvalue()


def export_outstanding_dues_csv(session: Session) -> str:
    """Exports all overdue invoices with outstanding balances as CSV."""
    invoices = session.exec(
        select(Invoice).where(
            Invoice.is_finalized == True,
            Invoice.balance_due > 0.01,
        ).order_by(col(Invoice.balance_due).desc())
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Invoice #", "Date", "Patient Name", "MRN", "Category", "Total Billed", "Paid", "Balance Due"])
    for inv in invoices:
        writer.writerow([
            inv.invoice_no,
            inv.created_at.strftime("%Y-%m-%d"),
            inv.patient_name,
            inv.patient_mrn,
            inv.patient_category.title(),
            f"{inv.total_amount:.2f}",
            f"{inv.paid_amount:.2f}",
            f"{inv.balance_due:.2f}",
        ])
    return output.getvalue()


def export_collection_summary_csv(session: Session) -> str:
    """Exports daily payment collection breakdown by cashier and mode."""
    payments = session.exec(
        select(Payment).order_by(col(Payment.received_at).desc())
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Receipt #", "Payment #", "Date Time", "Patient MRN", "Cashier", "Payment Method", "Amount ($)", "Ref #"])
    for p in payments:
        writer.writerow([
            p.receipt_no,
            p.payment_no,
            p.received_at.strftime("%Y-%m-%d %H:%M"),
            p.patient_mrn,
            p.collected_by,
            p.payment_method.replace("_", " ").title(),
            f"{p.amount:.2f}",
            p.reference_number or "",
        ])
    return output.getvalue()


def export_insurance_aging_csv(session: Session) -> str:
    """Exports outstanding claims with aging buckets (0-30d, 31-60d, 61-90d, 90d+)."""
    claims = session.exec(select(Claim).order_by(col(Claim.submitted_at).desc())).all()
    now = utc_now()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Claim #", "Submitted Date", "Insurer / Payer", "Patient MRN", "Claim Amount", "Aging Days", "Aging Bracket", "Status"])

    for c in claims:
        days = (now - c.submitted_at).days if c.submitted_at else 0
        if days <= 30:
            bracket = "0-30 Days"
        elif days <= 60:
            bracket = "31-60 Days"
        elif days <= 90:
            bracket = "61-90 Days"
        else:
            bracket = "90+ Days"

        writer.writerow([
            c.claim_no,
            c.submitted_at.strftime("%Y-%m-%d"),
            c.provider_name,
            c.patient_mrn,
            f"{c.claim_amount:.2f}",
            days,
            bracket,
            c.status.upper(),
        ])
    return output.getvalue()
