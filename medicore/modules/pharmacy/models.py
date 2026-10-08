from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from sqlmodel import Column, Field, JSON, Relationship, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PharmacyStore(SQLModel, table=True):
    __tablename__ = "pharmacy_stores"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str = Field(index=True)
    is_main: bool = Field(default=False)
    location: str = Field(default="")
    is_active: bool = Field(default=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Item(SQLModel, table=True):
    __tablename__ = "pharmacy_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)  # SKU / barcode
    name: str = Field(index=True)
    generic_name: str = Field(index=True)
    brand_name: Optional[str] = Field(default=None, index=True)
    drug_master_ref: Optional[str] = Field(default=None)
    category: str = Field(index=True)
    form: str = Field(default="Tablet")
    strength: str = Field(default="")
    unit: str = Field(default="Tablet")
    unit_price: float = Field(default=0.0)  # MRP / Sales Price
    cost_price: float = Field(default=0.0)  # Default purchase cost
    reorder_level: int = Field(default=50)
    reorder_quantity: int = Field(default=150)
    is_prescription_required: bool = Field(default=True)
    is_active: bool = Field(default=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Batch(SQLModel, table=True):
    __tablename__ = "pharmacy_batches"

    id: Optional[int] = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="pharmacy_items.id", index=True)
    store_id: int = Field(foreign_key="pharmacy_stores.id", index=True, default=1)
    lot_no: str = Field(index=True)
    expiry_date: date = Field(index=True)
    cost_price: float = Field(default=0.0)
    mrp: float = Field(default=0.0)
    unit_price: float = Field(default=0.0)
    quantity_received: int = Field(default=0)
    quantity_remaining: int = Field(default=0, index=True)  # Current stock balance
    is_quarantined: bool = Field(default=False, index=True)
    quarantine_reason: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Supplier(SQLModel, table=True):
    __tablename__ = "pharmacy_suppliers"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str = Field(index=True)
    contact_person: str = Field(default="")
    email: str = Field(default="")
    phone: str = Field(default="")
    address: str = Field(default="")
    tax_id: str = Field(default="")
    payment_terms: str = Field(default="Net 30")
    rating: Optional[float] = Field(default=5.0)
    is_active: bool = Field(default=True)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class PurchaseOrder(SQLModel, table=True):
    __tablename__ = "pharmacy_purchase_orders"

    id: Optional[int] = Field(default=None, primary_key=True)
    po_no: str = Field(index=True, unique=True)
    supplier_id: int = Field(foreign_key="pharmacy_suppliers.id", index=True)
    store_id: int = Field(foreign_key="pharmacy_stores.id", index=True, default=1)
    order_date: date = Field(default_factory=date.today)
    expected_date: Optional[date] = Field(default=None)
    status: str = Field(default="draft", index=True)  # draft, ordered, partial_received, received, cancelled
    total_amount: float = Field(default=0.0)
    notes: Optional[str] = Field(default=None)
    created_by: Optional[str] = Field(default=None)
    approved_by: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class PurchaseOrderLine(SQLModel, table=True):
    __tablename__ = "pharmacy_purchase_order_lines"

    id: Optional[int] = Field(default=None, primary_key=True)
    po_id: int = Field(foreign_key="pharmacy_purchase_orders.id", index=True)
    item_id: int = Field(foreign_key="pharmacy_items.id", index=True)
    item_name: str
    requested_qty: int
    received_qty: int = Field(default=0)
    unit_cost: float
    line_total: float
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class GoodsReceipt(SQLModel, table=True):
    __tablename__ = "pharmacy_goods_receipts"

    id: Optional[int] = Field(default=None, primary_key=True)
    grn_no: str = Field(index=True, unique=True)
    po_id: Optional[int] = Field(default=None, foreign_key="pharmacy_purchase_orders.id", index=True)
    supplier_id: int = Field(foreign_key="pharmacy_suppliers.id", index=True)
    store_id: int = Field(foreign_key="pharmacy_stores.id", index=True, default=1)
    received_date: date = Field(default_factory=date.today)
    invoice_no: str = Field(default="")
    received_by: str = Field(default="")
    status: str = Field(default="received")  # received, inspected
    total_amount: float = Field(default=0.0)
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class GoodsReceiptLine(SQLModel, table=True):
    __tablename__ = "pharmacy_goods_receipt_lines"

    id: Optional[int] = Field(default=None, primary_key=True)
    goods_receipt_id: int = Field(foreign_key="pharmacy_goods_receipts.id", index=True)
    item_id: int = Field(foreign_key="pharmacy_items.id", index=True)
    batch_id: Optional[int] = Field(default=None, foreign_key="pharmacy_batches.id")
    lot_no: str
    expiry_date: date
    quantity: int
    unit_cost: float
    mrp: float
    line_total: float
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class StockMovement(SQLModel, table=True):
    """Immutable Stock Ledger: Every physical change to stock is recorded as an entry."""
    __tablename__ = "pharmacy_stock_movements"

    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=utc_now, index=True)
    movement_type: str = Field(index=True)  # receipt, dispense, return, adjustment, transfer_out, transfer_in, wastage, quarantine
    item_id: int = Field(foreign_key="pharmacy_items.id", index=True)
    batch_id: Optional[int] = Field(default=None, foreign_key="pharmacy_batches.id", index=True)
    store_id: int = Field(foreign_key="pharmacy_stores.id", index=True)
    to_store_id: Optional[int] = Field(default=None, foreign_key="pharmacy_stores.id")
    quantity: int  # Positive for addition, negative for reduction
    balance_after: int  # Current item/batch balance in this store after movement
    unit_cost: float = Field(default=0.0)
    reason_code: str = Field(index=True)  # PO_RECEIPT, RX_DISPENSE, OTC_SALE, PATIENT_RETURN, SUPPLIER_RETURN, STOCK_TAKE_ADJUSTMENT, EXPIRED_WASTAGE, STORE_TRANSFER, QUARANTINE_LOCK
    reference_type: str = Field(default="manual")  # goods_receipt, dispense, return, transfer, adjustment, manual
    reference_id: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    created_by: Optional[str] = Field(default=None)
    approved_by: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Dispense(SQLModel, table=True):
    __tablename__ = "pharmacy_dispenses"

    id: Optional[int] = Field(default=None, primary_key=True)
    dispense_no: str = Field(index=True, unique=True)
    dispense_type: str = Field(default="prescription", index=True)  # prescription, counter_otc
    prescription_id: Optional[int] = Field(default=None, index=True)
    encounter_id: Optional[int] = Field(default=None, index=True)
    patient_id: Optional[int] = Field(default=None, index=True)
    patient_name: str = Field(index=True)
    patient_mrn: Optional[str] = Field(default=None, index=True)
    doctor_id: Optional[int] = Field(default=None)
    doctor_name: Optional[str] = Field(default=None)
    store_id: int = Field(foreign_key="pharmacy_stores.id", default=1, index=True)
    status: str = Field(default="pending", index=True)  # pending, in_progress, dispensed, partial, rejected
    allergy_warnings: Optional[str] = Field(default=None)
    safety_overrides: Optional[str] = Field(default=None)  # Doctor's overrides
    total_amount: float = Field(default=0.0)
    notes: Optional[str] = Field(default=None)
    dispensed_by: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    dispensed_at: Optional[datetime] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class DispenseLine(SQLModel, table=True):
    __tablename__ = "pharmacy_dispense_lines"

    id: Optional[int] = Field(default=None, primary_key=True)
    dispense_id: int = Field(foreign_key="pharmacy_dispenses.id", index=True)
    item_id: int = Field(foreign_key="pharmacy_items.id", index=True)
    item_name: str
    prescribed_item_name: Optional[str] = Field(default=None)
    prescribed_qty: int = Field(default=0)
    dispensed_qty: int = Field(default=0)
    batch_id: Optional[int] = Field(default=None, foreign_key="pharmacy_batches.id")
    lot_no: Optional[str] = Field(default=None)
    expiry_date: Optional[date] = Field(default=None)
    unit_price: float = Field(default=0.0)
    line_total: float = Field(default=0.0)
    is_generic_substitution: bool = Field(default=False)
    substitution_reason: Optional[str] = Field(default=None)
    original_drug_name: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class PharmacyReturn(SQLModel, table=True):
    __tablename__ = "pharmacy_returns"

    id: Optional[int] = Field(default=None, primary_key=True)
    return_no: str = Field(index=True, unique=True)
    return_type: str = Field(default="patient_return", index=True)  # patient_return, supplier_return
    dispense_id: Optional[int] = Field(default=None, foreign_key="pharmacy_dispenses.id")
    supplier_id: Optional[int] = Field(default=None, foreign_key="pharmacy_suppliers.id")
    store_id: int = Field(foreign_key="pharmacy_stores.id", default=1)
    status: str = Field(default="pending", index=True)  # pending, approved, rejected
    total_amount: float = Field(default=0.0)
    reason: str
    created_by: str
    approved_by: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    approved_at: Optional[datetime] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class PharmacyReturnLine(SQLModel, table=True):
    __tablename__ = "pharmacy_return_lines"

    id: Optional[int] = Field(default=None, primary_key=True)
    return_id: int = Field(foreign_key="pharmacy_returns.id", index=True)
    item_id: int = Field(foreign_key="pharmacy_items.id", index=True)
    batch_id: Optional[int] = Field(default=None, foreign_key="pharmacy_batches.id")
    quantity: int
    unit_price: float
    line_total: float
    reason: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class StockTransferRequest(SQLModel, table=True):
    __tablename__ = "pharmacy_stock_transfers"

    id: Optional[int] = Field(default=None, primary_key=True)
    transfer_no: str = Field(index=True, unique=True)
    from_store_id: int = Field(foreign_key="pharmacy_stores.id")
    to_store_id: int = Field(foreign_key="pharmacy_stores.id")
    status: str = Field(default="pending", index=True)  # pending, approved, transferred, rejected
    requested_by: str
    approved_by: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    transferred_at: Optional[datetime] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class StockTransferLine(SQLModel, table=True):
    __tablename__ = "pharmacy_stock_transfer_lines"

    id: Optional[int] = Field(default=None, primary_key=True)
    transfer_id: int = Field(foreign_key="pharmacy_stock_transfers.id", index=True)
    item_id: int = Field(foreign_key="pharmacy_items.id", index=True)
    requested_qty: int
    transferred_qty: int = Field(default=0)
    batch_id: Optional[int] = Field(default=None, foreign_key="pharmacy_batches.id")
    custom_fields: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
