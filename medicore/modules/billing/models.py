from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PriceList(SQLModel, table=True):
    __tablename__ = "billing_price_lists"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)  # GENERAL, STAFF, INSURED, VIP
    name: str
    patient_category: str = Field(default="general", index=True)  # general, staff, insured, vip
    discount_percentage: float = Field(default=0.0)
    effective_from: datetime = Field(default_factory=utc_now)
    effective_to: Optional[datetime] = Field(default=None)
    is_active: bool = Field(default=True, index=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ServiceCatalog(SQLModel, table=True):
    __tablename__ = "billing_service_catalog"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)  # CONS-GEN, PROC-SUT, BED-ICU, PKG-DELIV
    name: str
    category: str = Field(index=True)  # consultation, procedure, bed_charge, package, laboratory, radiology, nursing, pharmacy
    department: Optional[str] = Field(default=None, index=True)
    base_price: float = Field(default=0.0)
    tax_rate: float = Field(default=0.0)
    is_package: bool = Field(default=False)
    package_id: Optional[int] = Field(default=None)
    is_active: bool = Field(default=True, index=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PriceListItem(SQLModel, table=True):
    __tablename__ = "billing_price_list_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    price_list_id: int = Field(index=True, foreign_key="billing_price_lists.id")
    service_id: int = Field(index=True, foreign_key="billing_service_catalog.id")
    price: float = Field(default=0.0)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Package(SQLModel, table=True):
    __tablename__ = "billing_packages"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)  # PKG-DELIV, PKG-CATARACT
    name: str
    package_price: float = Field(default=0.0)
    duration_days: int = Field(default=1)
    description: Optional[str] = Field(default=None)
    included_services: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    is_active: bool = Field(default=True, index=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class InsuranceProvider(SQLModel, table=True):
    __tablename__ = "billing_insurance_providers"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    code: str = Field(index=True, unique=True)  # MEDISHIELD, STARHLTH, BUPA
    contact_person: Optional[str] = Field(default=None)
    phone: Optional[str] = Field(default=None)
    email: Optional[str] = Field(default=None)
    address: Optional[str] = Field(default=None)
    payer_id: Optional[str] = Field(default=None)
    tpa_name: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True, index=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class InsurancePolicy(SQLModel, table=True):
    __tablename__ = "billing_insurance_policies"

    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(index=True)
    patient_mrn: str = Field(index=True)
    patient_name: str
    provider_id: int = Field(index=True, foreign_key="billing_insurance_providers.id")
    provider_name: str
    policy_number: str = Field(index=True)
    group_number: Optional[str] = Field(default=None)
    member_id: Optional[str] = Field(default=None)
    valid_from: datetime = Field(default_factory=utc_now)
    valid_to: datetime = Field(default_factory=utc_now)
    co_pay_percentage: float = Field(default=0.0)  # e.g. 10.0%, 20.0%
    deductible: float = Field(default=0.0)
    max_coverage_limit: float = Field(default=10000.0)
    per_service_limits: Dict[str, float] = Field(default_factory=dict, sa_column=Column(JSON))
    exclusions: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="active", index=True)  # active, expired, suspended
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PendingCharge(SQLModel, table=True):
    __tablename__ = "billing_pending_charges"

    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(index=True)
    patient_mrn: str = Field(index=True)
    patient_name: str
    encounter_id: Optional[int] = Field(default=None, index=True)
    source_type: str = Field(index=True)  # consultation, pharmacy, laboratory, imaging, procedure, manual
    source_id: str = Field(index=True)
    source_event_id: str = Field(index=True, unique=True)  # IDEMPOTENCY KEY
    service_code: Optional[str] = Field(default=None, index=True)
    description: str
    quantity: float = Field(default=1.0)
    unit_price: float = Field(default=0.0)
    total_price: float = Field(default=0.0)
    status: str = Field(default="pending", index=True)  # pending, invoiced, cancelled
    invoice_id: Optional[int] = Field(default=None, index=True)
    invoiced_at: Optional[datetime] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class Invoice(SQLModel, table=True):
    __tablename__ = "billing_invoices"

    id: Optional[int] = Field(default=None, primary_key=True)
    invoice_no: str = Field(index=True, unique=True)  # INV-2026-00001
    patient_id: int = Field(index=True)
    patient_mrn: str = Field(index=True)
    patient_name: str
    encounter_id: Optional[int] = Field(default=None, index=True)
    price_list_id: Optional[int] = Field(default=None)
    patient_category: str = Field(default="general", index=True)  # general, staff, insured, vip
    status: str = Field(default="draft", index=True)  # draft, finalized, partially_paid, paid, credit_note_issued, cancelled
    subtotal: float = Field(default=0.0)
    tax_amount: float = Field(default=0.0)
    discount_amount: float = Field(default=0.0)
    total_amount: float = Field(default=0.0)
    paid_amount: float = Field(default=0.0)
    deposit_applied: float = Field(default=0.0)
    insurance_covered: float = Field(default=0.0)
    patient_payable: float = Field(default=0.0)
    balance_due: float = Field(default=0.0)
    is_finalized: bool = Field(default=False, index=True)
    finalized_at: Optional[datetime] = Field(default=None)
    finalized_by: Optional[str] = Field(default=None)
    has_credit_note: bool = Field(default=False)
    credit_note_id: Optional[int] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class InvoiceLine(SQLModel, table=True):
    __tablename__ = "billing_invoice_lines"

    id: Optional[int] = Field(default=None, primary_key=True)
    invoice_id: int = Field(index=True, foreign_key="billing_invoices.id")
    service_id: Optional[int] = Field(default=None)
    service_code: str = Field(index=True)
    description: str
    category: str = Field(default="procedure", index=True)  # consultation, pharmacy, laboratory, procedure, bed_charge, package, manual
    quantity: float = Field(default=1.0)
    unit_price: float = Field(default=0.0)
    base_price: float = Field(default=0.0)
    price_override_reason: Optional[str] = Field(default=None)
    discount_amount: float = Field(default=0.0)
    tax_amount: float = Field(default=0.0)
    total_price: float = Field(default=0.0)
    patient_share: float = Field(default=0.0)
    insurance_share: float = Field(default=0.0)
    pending_charge_id: Optional[int] = Field(default=None, index=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Payment(SQLModel, table=True):
    __tablename__ = "billing_payments"

    id: Optional[int] = Field(default=None, primary_key=True)
    payment_no: str = Field(index=True, unique=True)  # PAY-2026-00001
    receipt_no: str = Field(index=True, unique=True)  # REC-2026-00001
    invoice_id: Optional[int] = Field(default=None, index=True)
    patient_id: int = Field(index=True)
    patient_mrn: str = Field(index=True)
    cash_session_id: Optional[int] = Field(default=None, index=True)
    amount: float = Field(default=0.0)
    payment_method: str = Field(default="cash", index=True)  # cash, card, mobile_banking, insurance, deposit, cheque, bank_transfer
    payment_split_details: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    reference_number: Optional[str] = Field(default=None)
    collected_by: str
    received_at: datetime = Field(default_factory=utc_now)
    status: str = Field(default="completed", index=True)  # completed, refunded, cancelled
    notes: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Deposit(SQLModel, table=True):
    __tablename__ = "billing_deposits"

    id: Optional[int] = Field(default=None, primary_key=True)
    deposit_no: str = Field(index=True, unique=True)  # DEP-2026-00001
    patient_id: int = Field(index=True)
    patient_mrn: str = Field(index=True)
    patient_name: str
    amount: float = Field(default=0.0)
    used_amount: float = Field(default=0.0)
    balance_amount: float = Field(default=0.0)
    payment_method: str = Field(default="cash")
    cash_session_id: Optional[int] = Field(default=None, index=True)
    status: str = Field(default="available", index=True)  # available, partially_used, exhausted, refunded
    collected_by: str
    notes: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class Refund(SQLModel, table=True):
    __tablename__ = "billing_refunds"

    id: Optional[int] = Field(default=None, primary_key=True)
    refund_no: str = Field(index=True, unique=True)  # REF-2026-00001
    invoice_id: Optional[int] = Field(default=None, index=True)
    payment_id: Optional[int] = Field(default=None, index=True)
    deposit_id: Optional[int] = Field(default=None, index=True)
    patient_id: int = Field(index=True)
    patient_mrn: str = Field(index=True)
    patient_name: str
    amount: float = Field(default=0.0)
    reason: str
    refund_method: str = Field(default="cash")
    status: str = Field(default="pending", index=True)  # pending, approved, completed, rejected
    requested_by: str
    approved_by: Optional[str] = Field(default=None)
    approved_at: Optional[datetime] = Field(default=None)
    processed_by: Optional[str] = Field(default=None)
    processed_at: Optional[datetime] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class Discount(SQLModel, table=True):
    __tablename__ = "billing_discounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    discount_no: str = Field(index=True, unique=True)  # DSC-2026-00001
    invoice_id: int = Field(index=True, foreign_key="billing_invoices.id")
    type: str = Field(default="percentage")  # percentage, fixed_amount, waiver
    value: float = Field(default=0.0)
    amount: float = Field(default=0.0)
    reason: str
    requires_approval: bool = Field(default=False)
    is_approved: bool = Field(default=True)
    approved_by: Optional[str] = Field(default=None)
    approved_at: Optional[datetime] = Field(default=None)
    status: str = Field(default="applied", index=True)  # pending_approval, applied, rejected
    applied_by: str
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class CreditNote(SQLModel, table=True):
    __tablename__ = "billing_credit_notes"

    id: Optional[int] = Field(default=None, primary_key=True)
    credit_note_no: str = Field(index=True, unique=True)  # CN-2026-00001
    invoice_id: int = Field(index=True, foreign_key="billing_invoices.id")
    original_invoice_no: str = Field(index=True)
    patient_id: int = Field(index=True)
    patient_mrn: str = Field(index=True)
    amount: float = Field(default=0.0)
    reason: str
    issued_by: str
    issued_at: datetime = Field(default_factory=utc_now)
    status: str = Field(default="issued", index=True)  # issued, settled
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Claim(SQLModel, table=True):
    __tablename__ = "billing_claims"

    id: Optional[int] = Field(default=None, primary_key=True)
    claim_no: str = Field(index=True, unique=True)  # CLM-2026-00001
    invoice_id: int = Field(index=True, foreign_key="billing_invoices.id")
    patient_id: int = Field(index=True)
    patient_mrn: str = Field(index=True)
    policy_id: int = Field(index=True, foreign_key="billing_insurance_policies.id")
    provider_id: int = Field(index=True, foreign_key="billing_insurance_providers.id")
    provider_name: str
    claim_amount: float = Field(default=0.0)
    approved_amount: float = Field(default=0.0)
    patient_co_pay: float = Field(default=0.0)
    deductible_applied: float = Field(default=0.0)
    status: str = Field(default="submitted", index=True)  # draft, submitted, in_review, approved, rejected, settled
    submitted_at: datetime = Field(default_factory=utc_now)
    adjudicated_at: Optional[datetime] = Field(default=None)
    rejection_reason: Optional[str] = Field(default=None)
    settled_at: Optional[datetime] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class CashSession(SQLModel, table=True):
    __tablename__ = "billing_cash_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_no: str = Field(index=True, unique=True)  # CS-2026-00001
    cashier_username: str = Field(index=True)
    opened_at: datetime = Field(default_factory=utc_now)
    closed_at: Optional[datetime] = Field(default=None)
    opening_balance: float = Field(default=0.0)
    closing_balance: Optional[float] = Field(default=None)
    cash_collected: float = Field(default=0.0)
    cash_refunded: float = Field(default=0.0)
    card_collected: float = Field(default=0.0)
    mobile_banking_collected: float = Field(default=0.0)
    deposit_collected: float = Field(default=0.0)
    total_expected_cash: float = Field(default=0.0)
    actual_counted_cash: Optional[float] = Field(default=None)
    variance: Optional[float] = Field(default=None)  # counted - expected
    variance_reason: Optional[str] = Field(default=None)
    status: str = Field(default="open", index=True)  # open, closed, reconciled
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
