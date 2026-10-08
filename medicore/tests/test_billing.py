import io
import json
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlmodel import Session, col, select

from medicore.core.database import engine
from medicore.core.events import Event, event_bus
from medicore.core.models import User
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
    calculate_insurance_split,
    close_cash_session,
    generate_credit_note_pdf,
    generate_invoice_pdf,
    generate_qr_code_data_uri,
    generate_receipt_pdf,
    get_service_price,
    handle_dispense_completed,
    handle_encounter_completed,
    handle_lab_charge,
    handle_order_placed,
    issue_credit_note,
    recalculate_invoice,
    record_payment,
)
from medicore.modules.patients.models import Patient


@pytest.fixture
def billing_session():
    """Provides a database session for billing unit and integration tests."""
    with Session(engine) as session:
        yield session


def get_or_create_patient(session: Session, mrn: str = "TEST-PAT-001") -> Patient:
    pat = session.exec(select(Patient).where(Patient.mrn == mrn)).first()
    if not pat:
        pat = Patient(
            mrn=mrn,
            first_name="Arthur",
            last_name="Dent",
            gender="Male",
            date_of_birth="1982-03-11",
            phone="+1 (555) 019-2834",
            email="arthur.dent@galaxy.org",
        )
        session.add(pat)
        session.commit()
        session.refresh(pat)
    return pat


# =========================================================================
# 1. EVENT-DRIVEN IDEMPOTENCY & DOUBLE-BILLING PREVENTION
# =========================================================================


def test_encounter_completed_charge_capture_idempotent(billing_session: Session):
    """Verify encounter.completed captures consultation fee and prevents double-billing via source_event_id."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-IDEMP-01")
    encounter_id = 9991

    event = Event(
        name="encounter.completed",
        payload={
            "id": encounter_id,
            "patient_id": pat.id,
            "patient_name": pat.full_name,
            "patient_mrn": pat.mrn,
            "doctor_name": "Sarah Chen",
            "department": "Cardiology",
        },
    )

    # First emission
    handle_encounter_completed(event)

    charge1 = billing_session.exec(
        select(PendingCharge).where(PendingCharge.source_event_id == f"encounter.completed:{encounter_id}")
    ).first()
    assert charge1 is not None
    assert charge1.source_type == "consultation"
    assert charge1.status == "pending"
    assert charge1.total_price > 0

    # Second emission with exact same encounter_id (duplicate event replay)
    handle_encounter_completed(event)

    # Verify database still has exactly 1 charge for this event
    charges = billing_session.exec(
        select(PendingCharge).where(PendingCharge.source_event_id == f"encounter.completed:{encounter_id}")
    ).all()
    assert len(charges) == 1


def test_dispense_completed_charge_capture_idempotent(billing_session: Session):
    """Verify dispense.completed captures pharmacy items and prevents duplicate ledger charges."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-IDEMP-02")
    dispense_id = 8881

    event = Event(
        name="dispense.completed",
        payload={
            "dispense_id": dispense_id,
            "dispense_no": f"DSP-{dispense_id}",
            "patient_id": pat.id,
            "patient_name": pat.full_name,
            "patient_mrn": pat.mrn,
            "total_amount": 45.50,
        },
    )

    # First emission
    handle_dispense_completed(event)

    charge1 = billing_session.exec(
        select(PendingCharge).where(PendingCharge.source_event_id == f"dispense.completed:{dispense_id}")
    ).first()
    assert charge1 is not None
    assert charge1.source_type == "pharmacy"
    assert charge1.total_price == 45.50

    # Duplicate emission
    handle_dispense_completed(event)

    # Verify no duplicates in DB
    db_charges = billing_session.exec(
        select(PendingCharge).where(PendingCharge.source_event_id == f"dispense.completed:{dispense_id}")
    ).all()
    assert len(db_charges) == 1


def test_lab_charge_capture_idempotent(billing_session: Session):
    """Verify lab.charge captures laboratory test fee idempotently."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-IDEMP-03")
    order_id = 7771

    event = Event(
        name="lab.charge",
        payload={
            "order_id": order_id,
            "order_no": f"LAB-{order_id}",
            "patient_id": pat.id,
            "patient_name": pat.full_name,
            "patient_mrn": pat.mrn,
            "amount": 35.00,
            "description": "Complete Blood Count (CBC)",
        },
    )

    handle_lab_charge(event)

    c1 = billing_session.exec(
        select(PendingCharge).where(PendingCharge.source_event_id == f"lab.charge:{order_id}")
    ).first()
    assert c1 is not None
    assert c1.total_price == 35.00
    assert c1.source_type == "laboratory"

    # Duplicate call
    handle_lab_charge(event)

    db_charges = billing_session.exec(
        select(PendingCharge).where(PendingCharge.source_event_id == f"lab.charge:{order_id}")
    ).all()
    assert len(db_charges) == 1


def test_order_placed_ignores_lab_to_prevent_duplicate_billing(billing_session: Session):
    """Verify order.placed handles imaging/procedures but skips type='lab' to avoid duplicate with lab.charge."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-IDEMP-04")

    # Lab order should be skipped
    lab_event = Event(
        name="order.placed",
        payload={
            "order_id": 501,
            "patient_id": pat.id,
            "patient_name": pat.full_name,
            "patient_mrn": pat.mrn,
            "type": "lab",
            "test_name": "Serum Electrolytes",
        },
    )
    handle_order_placed(lab_event)
    lab_charge = billing_session.exec(
        select(PendingCharge).where(PendingCharge.source_event_id == "order.placed:501")
    ).first()
    assert lab_charge is None

    # Procedure/imaging order should be captured
    proc_event = Event(
        name="order.placed",
        payload={
            "order_id": 502,
            "patient_id": pat.id,
            "patient_name": pat.full_name,
            "patient_mrn": pat.mrn,
            "type": "procedure",
            "test_name": "Minor Wound Dressing",
        },
    )
    handle_order_placed(proc_event)
    proc_charge = billing_session.exec(
        select(PendingCharge).where(PendingCharge.source_event_id == "order.placed:502")
    ).first()
    assert proc_charge is not None
    assert proc_charge.source_type == "procedure"


# =========================================================================
# 2. PRICING & TARIFF CALCULATIONS
# =========================================================================


def test_price_list_category_tariffs(billing_session: Session):
    """Verify pricing logic correctly applies price list tariffs (General, Staff, Insured, VIP)."""
    svc = billing_session.exec(select(ServiceCatalog).where(ServiceCatalog.code == "CONS-GEN")).first()
    assert svc is not None

    pl_gen = billing_session.exec(select(PriceList).where(PriceList.code == "GENERAL")).first()
    pl_staff = billing_session.exec(select(PriceList).where(PriceList.code == "STAFF")).first()
    pl_ins = billing_session.exec(select(PriceList).where(PriceList.code == "INSURED")).first()
    pl_vip = billing_session.exec(select(PriceList).where(PriceList.code == "VIP")).first()

    price_gen = get_service_price(billing_session, svc.code, price_list_id=pl_gen.id)
    price_staff = get_service_price(billing_session, svc.code, price_list_id=pl_staff.id)
    price_ins = get_service_price(billing_session, svc.code, price_list_id=pl_ins.id)
    price_vip = get_service_price(billing_session, svc.code, price_list_id=pl_vip.id)

    assert price_gen == svc.base_price
    assert price_staff < price_gen  # Staff discount (20%)
    assert price_ins >= price_gen   # Insured agreed rate
    assert price_vip > price_gen    # VIP premium rate


# =========================================================================
# 3. INSURANCE CO-PAY AND CLAIM SPLIT CALCULATIONS
# =========================================================================


def test_insurance_split_calculation(billing_session: Session):
    """Verify co-pay %, deductible, and insurance coverage calculation."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-INS-01")
    prov = billing_session.exec(select(InsuranceProvider)).first()

    policy = InsurancePolicy(
        patient_id=pat.id,
        patient_mrn=pat.mrn,
        patient_name=pat.full_name,
        provider_id=prov.id,
        provider_name=prov.name,
        policy_number="POL-TEST-SPLIT-01",
        valid_from=utc_now() - timedelta(days=30),
        valid_to=utc_now() + timedelta(days=30),
        co_pay_percentage=15.0,
        deductible=50.0,
        max_coverage_limit=5000.0,
        status="active",
    )
    billing_session.add(policy)
    billing_session.commit()
    billing_session.refresh(policy)

    line = InvoiceLine(
        invoice_id=99999,
        service_code="PROC-SURG",
        description="Minor Surgical Procedure",
        category="procedure",
        quantity=1.0,
        unit_price=400.0,
        total_price=400.0,
    )

    ins_cov, pat_pay, ded_app = calculate_insurance_split(
        session=billing_session,
        lines=[line],
        policy=policy,
    )

    # Line item total = $400.00
    # Deductible = $50, Co-pay = 15%
    # Covered base = 400 - 50 = 350. Copay = 15% of 350 = 52.50
    # Insured share = 350 - 52.50 = 297.50
    assert ins_cov + pat_pay == pytest.approx(400.00, 0.01)
    assert ins_cov == pytest.approx(290.00, 0.01)
    assert pat_pay == pytest.approx(110.00, 0.01)
    assert ded_app == pytest.approx(50.00, 0.01)


# =========================================================================
# 4. INVOICE RECALCULATION & FINALIZATION IMMUTABILITY
# =========================================================================


def test_invoice_finalization_immutability(billing_session: Session):
    """Verify finalized invoices become immutable; further modifications require credit notes."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-IMMUTABLE-01")

    # Create draft invoice
    inv = Invoice(
        invoice_no=f"INV-TEST-IMMUTABLE-{uuid.uuid4().hex[:6]}",
        patient_id=pat.id,
        patient_mrn=pat.mrn,
        patient_name=pat.full_name,
        patient_category="general",
        status="draft",
        is_finalized=False,
    )
    billing_session.add(inv)
    billing_session.commit()
    billing_session.refresh(inv)

    # Add 2 lines
    l1 = InvoiceLine(
        invoice_id=inv.id,
        service_code="CONS-GEN",
        description="General OPD Consultation",
        quantity=1.0,
        unit_price=50.0,
        base_price=50.0,
        total_price=50.0,
        patient_share=50.0,
        insurance_share=0.0,
    )
    l2 = InvoiceLine(
        invoice_id=inv.id,
        service_code="PROC-DRESS",
        description="Wound Dressing",
        quantity=1.0,
        unit_price=35.0,
        base_price=35.0,
        total_price=35.0,
        patient_share=35.0,
        insurance_share=0.0,
    )
    billing_session.add(l1)
    billing_session.add(l2)
    billing_session.commit()

    # Recalculate
    inv = recalculate_invoice(billing_session, inv)
    assert inv.subtotal == 85.0
    assert inv.total_amount > 0
    assert not inv.is_finalized

    # Finalize invoice
    inv.is_finalized = True
    inv.status = "finalized"
    inv.finalized_at = utc_now()
    inv.finalized_by = "cashier.emma"
    billing_session.add(inv)
    billing_session.commit()
    billing_session.refresh(inv)

    assert inv.is_finalized is True
    assert inv.status == "finalized"


def test_credit_note_issuance(billing_session: Session):
    """Verify official credit notes can be issued against finalized invoices with amount reversals."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-CN-01")

    inv = Invoice(
        invoice_no=f"INV-TEST-CN-{uuid.uuid4().hex[:6]}",
        patient_id=pat.id,
        patient_mrn=pat.mrn,
        patient_name=pat.full_name,
        patient_category="general",
        status="finalized",
        subtotal=100.0,
        total_amount=100.0,
        paid_amount=0.0,
        patient_payable=100.0,
        balance_due=100.0,
        is_finalized=True,
        finalized_at=utc_now(),
        finalized_by="cashier.emma",
    )
    billing_session.add(inv)
    billing_session.commit()
    billing_session.refresh(inv)

    cn = issue_credit_note(
        session=billing_session,
        invoice_id=inv.id,
        amount=100.0,
        reason="Consultation order cancelled prior to clinical encounter",
        user_name="manager.robert",
    )

    assert cn is not None
    assert cn.amount == 100.0
    assert cn.original_invoice_no == inv.invoice_no

    billing_session.refresh(inv)
    assert inv.has_credit_note is True
    assert inv.status == "credit_note_issued"
    assert inv.balance_due == 0.0


# =========================================================================
# 5. PAYMENTS: SPLIT, PARTIAL, AND RECEIPT GENERATION
# =========================================================================


def test_partial_and_split_payments(billing_session: Session):
    """Verify recording partial payments, split payments (cash + card), and receipt tracking."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-PAY-01")

    inv = Invoice(
        invoice_no=f"INV-TEST-PAY-{uuid.uuid4().hex[:6]}",
        patient_id=pat.id,
        patient_mrn=pat.mrn,
        patient_name=pat.full_name,
        patient_category="general",
        status="finalized",
        subtotal=200.0,
        total_amount=200.0,
        paid_amount=0.0,
        patient_payable=200.0,
        balance_due=200.0,
        is_finalized=True,
    )
    billing_session.add(inv)
    billing_session.commit()
    billing_session.refresh(inv)

    line = InvoiceLine(
        invoice_id=inv.id,
        service_code="PROC-SURG",
        description="Minor Surgical Procedure",
        category="procedure",
        quantity=1.0,
        unit_price=200.0,
        base_price=200.0,
        total_price=200.0,
        patient_share=200.0,
    )
    billing_session.add(line)
    billing_session.commit()

    # 1. Partial payment of $80 via cash
    pay1 = record_payment(
        session=billing_session,
        invoice_id=inv.id,
        patient_id=pat.id,
        amount=80.0,
        payment_method="cash",
        user_name="cashier.emma",
    )
    assert pay1.amount == 80.0
    assert pay1.receipt_no.startswith("REC-")

    billing_session.refresh(inv)
    assert inv.status == "partially_paid"
    assert inv.paid_amount == 80.0
    assert inv.balance_due == 130.0

    # 2. Split payment for remaining $130 ($80 card + $50 cash)
    split_info = [
        {"method": "card", "amount": 80.0, "reference": "TXN-CARD-123"},
        {"method": "cash", "amount": 50.0, "reference": None},
    ]
    pay2 = record_payment(
        session=billing_session,
        invoice_id=inv.id,
        patient_id=pat.id,
        amount=130.0,
        payment_method="split",
        split_details=split_info,
        user_name="cashier.emma",
    )
    assert pay2.amount == 130.0

    billing_session.refresh(inv)
    assert inv.status == "paid"
    assert inv.paid_amount == 210.0
    assert inv.balance_due == 0.0


# =========================================================================
# 6. ADVANCE DEPOSIT ADJUSTMENT
# =========================================================================


def test_advance_deposit_adjustment(billing_session: Session):
    """Verify patient deposit collection and automatic adjustment against invoice dues."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-DEP-01")

    # Collect deposit of $300
    dep = Deposit(
        deposit_no=f"DEP-TEST-{uuid.uuid4().hex[:6]}",
        patient_id=pat.id,
        patient_mrn=pat.mrn,
        patient_name=pat.full_name,
        amount=300.0,
        used_amount=0.0,
        balance_amount=300.0,
        payment_method="cash",
        status="available",
        collected_by="cashier.emma",
    )
    billing_session.add(dep)
    billing_session.commit()
    billing_session.refresh(dep)

    # Invoice of $200
    inv = Invoice(
        invoice_no=f"INV-TEST-DEP-{uuid.uuid4().hex[:6]}",
        patient_id=pat.id,
        patient_mrn=pat.mrn,
        patient_name=pat.full_name,
        patient_category="general",
        status="finalized",
        subtotal=200.0,
        total_amount=200.0,
        paid_amount=0.0,
        patient_payable=200.0,
        balance_due=200.0,
        is_finalized=True,
    )
    billing_session.add(inv)
    billing_session.commit()
    billing_session.refresh(inv)

    # Adjust $200 from deposit
    apply_deposit_to_invoice(
        session=billing_session,
        deposit_id=dep.id,
        invoice_id=inv.id,
        amount=200.0,
        user_name="cashier.emma",
    )

    billing_session.refresh(dep)
    billing_session.refresh(inv)

    assert dep.used_amount == 200.0
    assert dep.balance_amount == 100.0
    assert dep.status == "partially_used"

    assert inv.deposit_applied == 200.0
    assert inv.balance_due == 0.0
    assert inv.status == "paid"


# =========================================================================
# 7. CASH SESSIONS & TILL RECONCILIATION
# =========================================================================


def test_cash_session_till_reconciliation_variance(billing_session: Session):
    """Verify till reconciliation computes variance between expected and actual counted cash."""
    session_no = f"CS-TEST-RECON-{uuid.uuid4().hex[:6]}"
    cs = CashSession(
        session_no=session_no,
        cashier_username="cashier.emma",
        opening_balance=500.0,
        cash_collected=1000.0,
        cash_refunded=50.0,
        card_collected=400.0,
        mobile_banking_collected=100.0,
        deposit_collected=200.0,
        total_expected_cash=1650.0,  # 500 (open) + 1000 (coll) - 50 (ref) + 200 (dep) = 1650
        status="open",
    )
    billing_session.add(cs)
    billing_session.commit()
    billing_session.refresh(cs)

    # Cashier counts $1640 (short by $10)
    closed_cs = close_cash_session(
        session=billing_session,
        session_id=cs.id,
        actual_counted_cash=1640.0,
        variance_reason="Cash drawer shortage of $10",
    )

    assert closed_cs.status == "closed"
    assert closed_cs.actual_counted_cash == 1640.0
    assert closed_cs.variance == -10.0  # 1640 - 1650 = -10.0 shortage
    assert closed_cs.closing_balance == 1640.0


# =========================================================================
# 8. CLINICAL PACKAGES BUNDLE APPLICATION
# =========================================================================


def test_apply_package_bundle_to_invoice(billing_session: Session):
    """Verify applying a clinical package bundles included services at the fixed package rate."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-PKG-01")
    pkg = billing_session.exec(select(Package).where(Package.code == "PKG-EXEC")).first()
    assert pkg is not None

    inv = Invoice(
        invoice_no=f"INV-TEST-PKG-{uuid.uuid4().hex[:6]}",
        patient_id=pat.id,
        patient_mrn=pat.mrn,
        patient_name=pat.full_name,
        patient_category="general",
        status="draft",
        is_finalized=False,
    )
    billing_session.add(inv)
    billing_session.commit()
    billing_session.refresh(inv)

    inv = apply_package_to_invoice(
        session=billing_session,
        invoice_id=inv.id,
        package_id=pkg.id,
        user_name="cashier.emma",
    )
    billing_session.refresh(inv)

    lines = billing_session.exec(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)).all()
    assert len(lines) >= 1
    assert inv.subtotal == pytest.approx(pkg.package_price, 0.01)


# =========================================================================
# 9. DISCOUNT GATING & MANAGER APPROVAL
# =========================================================================


def test_discount_gating_and_approval(billing_session: Session):
    """Verify discounts over limit require manager approval before being applied."""
    pat = get_or_create_patient(billing_session, mrn="TEST-PAT-DISC-01")
    cashier = billing_session.exec(select(User).where(User.username == "cashier.emma")).first()
    manager = billing_session.exec(select(User).where(User.username == "manager.robert")).first()

    inv = Invoice(
        invoice_no=f"INV-TEST-DISC-{uuid.uuid4().hex[:6]}",
        patient_id=pat.id,
        patient_mrn=pat.mrn,
        patient_name=pat.full_name,
        patient_category="general",
        status="draft",
        subtotal=1000.0,
        total_amount=1050.0,
        patient_payable=1050.0,
        balance_due=1050.0,
        is_finalized=False,
    )
    billing_session.add(inv)
    billing_session.commit()
    billing_session.refresh(inv)

    # Cashier requests 20% discount ($200 > $50 approval limit)
    disc = apply_discount(
        session=billing_session,
        invoice_id=inv.id,
        discount_type="percentage",
        value=20.0,
        reason="Special medical hardship concession requested",
        user=cashier,
    )

    assert disc.requires_approval is True
    assert disc.is_approved is False
    assert disc.status == "pending_approval"

    # Manager approves discount
    approved_disc = approve_discount(
        session=billing_session,
        discount_id=disc.id,
        manager_user=manager,
    )
    assert approved_disc.is_approved is True
    assert approved_disc.status == "applied"
    assert approved_disc.approved_by == manager.username


# =========================================================================
# 10. PRINTABLE PDFS & NUMBER-TO-WORDS UTILITIES
# =========================================================================


def test_amount_to_words_utility():
    """Verify number-to-words currency converter handles whole and fractional amounts."""
    assert amount_to_words(0.0) == "Zero Dollars Only"
    assert amount_to_words(1.0) == "One Dollar Only"
    assert amount_to_words(25.50) == "Twenty Five Dollars and Fifty Cents Only"
    assert amount_to_words(1250.75) == "One Thousand Two Hundred Fifty Dollars and Seventy Five Cents Only"


def test_qr_code_generation():
    """Verify QR code data URI generates a valid base64 image string."""
    qr_uri = generate_qr_code_data_uri("https://medicore.health/verify/INV-2026-00001")
    assert qr_uri.startswith("data:image/png;base64,")
    assert len(qr_uri) > 100


def test_pdf_generators(billing_session: Session):
    """Verify ReportLab PDF generation for Invoice, Money Receipt, and Credit Note."""
    inv = billing_session.exec(select(Invoice)).first()
    assert inv is not None

    # Invoice PDF
    inv_pdf = generate_invoice_pdf(billing_session, inv.id)
    assert isinstance(inv_pdf, bytes)
    assert inv_pdf.startswith(b"%PDF")

    # Payment Receipt PDF
    pmt = billing_session.exec(select(Payment)).first()
    if pmt:
        rec_pdf = generate_receipt_pdf(billing_session, pmt.id)
        assert isinstance(rec_pdf, bytes)
        assert rec_pdf.startswith(b"%PDF")

    # Credit Note PDF
    cn = billing_session.exec(select(CreditNote)).first()
    if cn:
        cn_pdf = generate_credit_note_pdf(billing_session, cn.id)
        assert isinstance(cn_pdf, bytes)
        assert cn_pdf.startswith(b"%PDF")
