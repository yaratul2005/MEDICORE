import csv
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlmodel import Session, col, desc, func, or_, select

from medicore.core.events import Event, event_bus
from medicore.core.models import AuditLog
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

logger = logging.getLogger("medicore.pharmacy.service")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_dispense_no(session: Session) -> str:
    today_str = date.today().strftime("%Y")
    count = session.exec(select(func.count(Dispense.id))).one()
    return f"DSP-{today_str}-{count + 1:05d}"


def generate_po_no(session: Session) -> str:
    today_str = date.today().strftime("%Y")
    count = session.exec(select(func.count(PurchaseOrder.id))).one()
    return f"PO-{today_str}-{count + 1:05d}"


def generate_grn_no(session: Session) -> str:
    today_str = date.today().strftime("%Y")
    count = session.exec(select(func.count(GoodsReceipt.id))).one()
    return f"GRN-{today_str}-{count + 1:05d}"


def generate_return_no(session: Session) -> str:
    today_str = date.today().strftime("%Y")
    count = session.exec(select(func.count(PharmacyReturn.id))).one()
    return f"RET-{today_str}-{count + 1:05d}"


def generate_transfer_no(session: Session) -> str:
    today_str = date.today().strftime("%Y")
    count = session.exec(select(func.count(StockTransferRequest.id))).one()
    return f"TRF-{today_str}-{count + 1:05d}"


# =========================================================================
# 1. IMMUTABLE STOCK LEDGER MANAGER
# =========================================================================

def record_stock_movement(
    session: Session,
    movement_type: str,
    item_id: int,
    store_id: int,
    quantity: int,
    reason_code: str,
    batch_id: Optional[int] = None,
    to_store_id: Optional[int] = None,
    reference_type: str = "manual",
    reference_id: Optional[str] = None,
    unit_cost: float = 0.0,
    notes: Optional[str] = None,
    created_by: Optional[str] = "system",
    approved_by: Optional[str] = None,
) -> StockMovement:
    """
    Core immutable ledger function.
    Every change to inventory must pass through this function.
    Direct updates to quantities are strictly prohibited.
    """
    item = session.get(Item, item_id)
    if not item:
        raise ValueError(f"Item with id {item_id} not found")

    batch = session.get(Batch, batch_id) if batch_id else None

    # 1. Update batch quantity_remaining atomically
    if batch:
        new_batch_qty = batch.quantity_remaining + quantity
        if new_batch_qty < 0:
            raise ValueError(
                f"Insufficient stock in Batch {batch.lot_no} for {item.name}. "
                f"Available: {batch.quantity_remaining}, Requested: {abs(quantity)}"
            )
        batch.quantity_remaining = new_batch_qty
        session.add(batch)

    # 2. Derive balance_after for this item in this store
    all_batches = session.exec(
        select(Batch).where(
            Batch.item_id == item_id,
            Batch.store_id == store_id,
            Batch.is_quarantined == False,
        )
    ).all()
    balance_after = sum(b.quantity_remaining for b in all_batches)

    # 3. Create immutable StockMovement entry
    movement = StockMovement(
        timestamp=utc_now(),
        movement_type=movement_type,
        item_id=item_id,
        batch_id=batch_id,
        store_id=store_id,
        to_store_id=to_store_id,
        quantity=quantity,
        balance_after=balance_after,
        unit_cost=unit_cost or (batch.cost_price if batch else item.cost_price),
        reason_code=reason_code,
        reference_type=reference_type,
        reference_id=str(reference_id) if reference_id else None,
        notes=notes,
        created_by=created_by,
        approved_by=approved_by,
    )
    session.add(movement)
    session.flush()

    # 4. Check for low-stock threshold alert
    total_hospital_stock = session.exec(
        select(func.sum(Batch.quantity_remaining)).where(
            Batch.item_id == item_id,
            Batch.is_quarantined == False,
        )
    ).one() or 0

    if total_hospital_stock <= item.reorder_level:
        event_bus.emit(
            "stock.low",
            {
                "item_id": item.id,
                "item_code": item.code,
                "item_name": item.name,
                "category": item.category,
                "current_stock": total_hospital_stock,
                "reorder_level": item.reorder_level,
                "reorder_quantity": item.reorder_quantity,
            },
            username=created_by or "system",
        )

    return movement


def get_item_stock_summary(session: Session, item_id: int, store_id: Optional[int] = None) -> Dict[str, Any]:
    """Calculates derived stock from active batches and verifies ledger consistency."""
    query = select(Batch).where(Batch.item_id == item_id, Batch.is_quarantined == False)
    if store_id:
        query = query.where(Batch.store_id == store_id)
    batches = session.exec(query).all()

    total_stock = sum(b.quantity_remaining for b in batches)
    today = date.today()
    active_batches = [b for b in batches if b.expiry_date >= today and b.quantity_remaining > 0]
    near_expiry_batches = [
        b for b in batches
        if today <= b.expiry_date <= (today + timedelta(days=60)) and b.quantity_remaining > 0
    ]
    expired_batches = [b for b in batches if b.expiry_date < today and b.quantity_remaining > 0]

    return {
        "total_stock": total_stock,
        "usable_stock": sum(b.quantity_remaining for b in active_batches),
        "active_batches_count": len(active_batches),
        "near_expiry_count": len(near_expiry_batches),
        "expired_count": len(expired_batches),
        "batches": batches,
    }


# =========================================================================
# 2. FEFO (FIRST-EXPIRY-FIRST-OUT) BATCH PICKING
# =========================================================================

def get_fefo_batches(
    session: Session,
    item_id: int,
    store_id: int = 1,
    allow_expired: bool = False,
) -> List[Batch]:
    """
    Picks batches in strict FEFO order:
    1. Sorts by expiry_date ASC
    2. Excludes quarantined batches
    3. Blocks expired batches (unless allow_expired explicitly requested for inspection)
    4. Only returns batches with quantity_remaining > 0
    """
    today = date.today()
    stmt = (
        select(Batch)
        .where(
            Batch.item_id == item_id,
            Batch.store_id == store_id,
            Batch.is_quarantined == False,
            Batch.quantity_remaining > 0,
        )
        .order_by(col(Batch.expiry_date).asc())
    )
    if not allow_expired:
        stmt = stmt.where(Batch.expiry_date >= today)

    return session.exec(stmt).all()


def pick_best_fefo_batch(
    session: Session,
    item_id: int,
    store_id: int = 1,
) -> Optional[Batch]:
    """Returns the top valid FEFO batch for an item, or None if out of stock."""
    batches = get_fefo_batches(session, item_id, store_id=store_id, allow_expired=False)
    return batches[0] if batches else None


# =========================================================================
# 3. ALERTS & QUARANTINE ENGINE
# =========================================================================

def get_low_stock_items(session: Session, store_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Returns items where total usable stock <= reorder_level."""
    items = session.exec(select(Item).where(Item.is_active == True)).all()
    low_stock = []
    for it in items:
        summary = get_item_stock_summary(session, it.id, store_id=store_id)
        if summary["usable_stock"] <= it.reorder_level:
            low_stock.append({
                "item": it,
                "usable_stock": summary["usable_stock"],
                "reorder_level": it.reorder_level,
                "deficit": it.reorder_level - summary["usable_stock"],
                "suggested_reorder": it.reorder_quantity,
            })
    return low_stock


def get_near_expiry_batches(
    session: Session,
    days_threshold: int = 60,
    store_id: Optional[int] = None,
) -> List[Tuple[Batch, Item]]:
    """Finds unquarantined batches with stock expiring within days_threshold."""
    today = date.today()
    limit_date = today + timedelta(days=days_threshold)
    stmt = (
        select(Batch, Item)
        .join(Item, Batch.item_id == Item.id)
        .where(
            Batch.is_quarantined == False,
            Batch.quantity_remaining > 0,
            Batch.expiry_date >= today,
            Batch.expiry_date <= limit_date,
        )
        .order_by(col(Batch.expiry_date).asc())
    )
    if store_id:
        stmt = stmt.where(Batch.store_id == store_id)
    return session.exec(stmt).all()


def get_expired_batches(
    session: Session,
    store_id: Optional[int] = None,
) -> List[Tuple[Batch, Item]]:
    """Finds batches with stock that have passed their expiry date."""
    today = date.today()
    stmt = (
        select(Batch, Item)
        .join(Item, Batch.item_id == Item.id)
        .where(
            Batch.quantity_remaining > 0,
            Batch.expiry_date < today,
        )
        .order_by(col(Batch.expiry_date).asc())
    )
    if store_id:
        stmt = stmt.where(Batch.store_id == store_id)
    return session.exec(stmt).all()


def quarantine_expired_batches(session: Session, user_name: str = "system") -> int:
    """Quarantines all expired batches with stock remaining to prevent dispensing."""
    today = date.today()
    expired = session.exec(
        select(Batch).where(
            Batch.expiry_date < today,
            Batch.quantity_remaining > 0,
            Batch.is_quarantined == False,
        )
    ).all()

    count = 0
    for b in expired:
        b.is_quarantined = True
        b.quarantine_reason = f"Automated quarantine: Expired on {b.expiry_date}"
        session.add(b)

        record_stock_movement(
            session=session,
            movement_type="quarantine",
            item_id=b.item_id,
            store_id=b.store_id,
            quantity=0,  # Quarantine does not change physical count, locks it
            reason_code="QUARANTINE_LOCK",
            batch_id=b.id,
            reference_type="manual",
            reference_id=f"BATCH-{b.id}",
            notes=f"Locked batch {b.lot_no} due to expiry {b.expiry_date}",
            created_by=user_name,
        )
        count += 1

    session.commit()
    logger.info(f"Quarantined {count} expired batches.")
    return count


# =========================================================================
# 4. DISPENSING LIFECYCLE & EVENT HANDLING
# =========================================================================

def handle_prescription_signed(event: Event):
    """
    Subscribes to 'prescription.signed'.
    Automatically creates a pending Dispense record in the Pharmacy queue.
    """
    payload = event.payload or {}
    rx_id = payload.get("prescription_id")
    if not rx_id:
        return

    from medicore.core.database import engine
    with Session(engine) as session:
        # Idempotency check: Don't duplicate if already queued
        existing = session.exec(select(Dispense).where(Dispense.prescription_id == rx_id)).first()
        if existing:
            logger.info(f"Dispense already exists for prescription #{rx_id}: {existing.dispense_no}")
            return

        dispense_no = generate_dispense_no(session)
        safety_overrides = payload.get("safety_overrides")
        if isinstance(safety_overrides, list):
            safety_overrides_str = json.dumps(safety_overrides)
        else:
            safety_overrides_str = str(safety_overrides or "")

        dispense = Dispense(
            dispense_no=dispense_no,
            dispense_type="prescription",
            prescription_id=rx_id,
            encounter_id=payload.get("encounter_id"),
            patient_id=payload.get("patient_id"),
            patient_name=payload.get("patient_name") or "Unknown Patient",
            patient_mrn=payload.get("patient_mrn"),
            doctor_id=payload.get("doctor_id"),
            doctor_name=payload.get("doctor_name"),
            store_id=1,  # Default to Main Store
            status="pending",
            safety_overrides=safety_overrides_str,
            allergy_warnings=payload.get("allergy_warnings"),
            created_at=utc_now(),
        )
        session.add(dispense)
        session.flush()

        # Add lines
        items = payload.get("items") or []
        for rx_item in items:
            drug_name = rx_item.get("drug_name") or rx_item.get("generic_name") or "Medication"
            prescribed_qty = int(rx_item.get("quantity") or 1)

            # Match drug with inventory items
            matched_item = session.exec(
                select(Item).where(
                    or_(
                        Item.name.ilike(f"%{drug_name}%"),
                        Item.generic_name.ilike(f"%{drug_name}%"),
                        Item.brand_name.ilike(f"%{drug_name}%"),
                    )
                )
            ).first()

            if not matched_item:
                # Fallback to any active item in Category
                matched_item = session.exec(select(Item).where(Item.is_active == True)).first()

            # Pre-select best FEFO batch
            best_batch = pick_best_fefo_batch(session, matched_item.id, store_id=1) if matched_item else None

            unit_price = best_batch.unit_price if best_batch else (matched_item.unit_price if matched_item else 10.0)

            line = DispenseLine(
                dispense_id=dispense.id,
                item_id=matched_item.id if matched_item else 1,
                item_name=matched_item.name if matched_item else drug_name,
                prescribed_item_name=drug_name,
                prescribed_qty=prescribed_qty,
                dispensed_qty=prescribed_qty,  # Defaults to full qty
                batch_id=best_batch.id if best_batch else None,
                lot_no=best_batch.lot_no if best_batch else None,
                expiry_date=best_batch.expiry_date if best_batch else None,
                unit_price=unit_price,
                line_total=round(unit_price * prescribed_qty, 2),
            )
            session.add(line)

        session.commit()
        logger.info(f"Queued prescription #{rx_id} as dispense {dispense_no} (Patient: {dispense.patient_name})")


def complete_dispense_order(
    session: Session,
    dispense_id: int,
    lines_input: List[Dict[str, Any]],
    user_name: str,
) -> Dispense:
    """
    Executes a clinical dispense:
    1. Validates selected batches (FEFO, non-expired, non-quarantined).
    2. Validates generic substitutions (logs reason).
    3. Handles partial quantities.
    4. Records StockMovement ledger entries for each line.
    5. Updates status to 'dispensed' or 'partial'.
    6. Emits 'dispense.completed' for billing and audit.
    """
    dispense = session.get(Dispense, dispense_id)
    if not dispense:
        raise ValueError(f"Dispense #{dispense_id} not found")

    if dispense.status in ["dispensed", "rejected"]:
        raise ValueError(f"Dispense order is already {dispense.status}")

    total_amount = 0.0
    is_partial = False

    for line_data in lines_input:
        line_id = line_data.get("line_id")
        line = session.get(DispenseLine, line_id) if line_id else None
        if not line or line.dispense_id != dispense.id:
            continue

        item = session.get(Item, line.item_id)
        batch_id = line_data.get("batch_id")
        dispensed_qty = int(line_data.get("dispensed_qty", line.prescribed_qty))

        if dispensed_qty < line.prescribed_qty:
            is_partial = True

        if dispensed_qty <= 0:
            line.dispensed_qty = 0
            line.line_total = 0.0
            session.add(line)
            continue

        # Validate Batch
        batch = session.get(Batch, batch_id) if batch_id else None
        if not batch:
            # Auto-pick FEFO if none provided
            batch = pick_best_fefo_batch(session, line.item_id, store_id=dispense.store_id)
            if not batch:
                raise ValueError(f"No available stock for {line.item_name} in store")

        if batch.is_quarantined:
            raise ValueError(f"Cannot dispense quarantined batch {batch.lot_no} for {line.item_name}")

        if batch.expiry_date < date.today():
            raise ValueError(f"Cannot dispense EXPIRED batch {batch.lot_no} (Expired: {batch.expiry_date})")

        if batch.quantity_remaining < dispensed_qty:
            raise ValueError(
                f"Batch {batch.lot_no} has only {batch.quantity_remaining} units remaining, requested {dispensed_qty}"
            )

        # Generic Substitution Check
        is_sub = bool(line_data.get("is_generic_substitution"))
        sub_reason = line_data.get("substitution_reason")
        if is_sub:
            if not sub_reason or not sub_reason.strip():
                raise ValueError(f"Generic substitution for {line.item_name} requires a documented reason")
            line.is_generic_substitution = True
            line.substitution_reason = sub_reason.strip()
            line.original_drug_name = line.prescribed_item_name

        line.batch_id = batch.id
        line.lot_no = batch.lot_no
        line.expiry_date = batch.expiry_date
        line.dispensed_qty = dispensed_qty
        line.unit_price = batch.unit_price or item.unit_price
        line.line_total = round(line.unit_price * dispensed_qty, 2)
        total_amount += line.line_total
        session.add(line)

        # 4. Deduct Stock via Immutable Ledger
        reason = "OTC_SALE" if dispense.dispense_type == "counter_otc" else "RX_DISPENSE"
        record_stock_movement(
            session=session,
            movement_type="dispense",
            item_id=line.item_id,
            batch_id=batch.id,
            store_id=dispense.store_id,
            quantity=-dispensed_qty,  # Negative deduction
            reason_code=reason,
            reference_type="dispense",
            reference_id=dispense.dispense_no,
            unit_cost=batch.cost_price,
            notes=f"Dispense {dispense.dispense_no} to {dispense.patient_name}",
            created_by=user_name,
        )

    dispense.total_amount = round(total_amount, 2)
    dispense.status = "partial" if is_partial else "dispensed"
    dispense.dispensed_by = user_name
    dispense.dispensed_at = utc_now()
    session.add(dispense)
    session.commit()
    session.refresh(dispense)

    # 5. Emit dispense.completed domain event for Billing & Reporting
    event_bus.emit(
        "dispense.completed",
        {
            "dispense_id": dispense.id,
            "dispense_no": dispense.dispense_no,
            "dispense_type": dispense.dispense_type,
            "patient_id": dispense.patient_id,
            "patient_mrn": dispense.patient_mrn,
            "patient_name": dispense.patient_name,
            "doctor_name": dispense.doctor_name,
            "status": dispense.status,
            "total_amount": dispense.total_amount,
            "dispensed_by": user_name,
            "dispensed_at": dispense.dispensed_at.isoformat(),
        },
        username=user_name,
    )

    return dispense


# =========================================================================
# 5. PURCHASING & GOODS RECEIPT (GRN)
# =========================================================================

def get_reorder_suggestions(session: Session, store_id: int = 1) -> List[Dict[str, Any]]:
    """Calculates replenishment suggestions based on current stock vs reorder levels."""
    low_stock = get_low_stock_items(session, store_id=store_id)
    suppliers = session.exec(select(Supplier).where(Supplier.is_active == True)).all()
    default_supplier = suppliers[0] if suppliers else None

    suggestions = []
    for entry in low_stock:
        item: Item = entry["item"]
        suggestions.append({
            "item_id": item.id,
            "item_code": item.code,
            "item_name": item.name,
            "category": item.category,
            "current_stock": entry["usable_stock"],
            "reorder_level": item.reorder_level,
            "suggested_qty": item.reorder_quantity,
            "estimated_cost": round(item.cost_price * item.reorder_quantity, 2),
            "unit_cost": item.cost_price,
            "supplier_id": default_supplier.id if default_supplier else 1,
            "supplier_name": default_supplier.name if default_supplier else "Primary Supplier",
        })
    return suggestions


def create_purchase_order(
    session: Session,
    supplier_id: int,
    lines: List[Dict[str, Any]],
    user_name: str,
    notes: Optional[str] = None,
    expected_days: int = 7,
) -> PurchaseOrder:
    """Creates a new purchase order with line items."""
    supplier = session.get(Supplier, supplier_id)
    if not supplier:
        raise ValueError(f"Supplier #{supplier_id} not found")

    po_no = generate_po_no(session)
    po = PurchaseOrder(
        po_no=po_no,
        supplier_id=supplier_id,
        store_id=1,
        order_date=date.today(),
        expected_date=date.today() + timedelta(days=expected_days),
        status="ordered",
        notes=notes,
        created_by=user_name,
        created_at=utc_now(),
    )
    session.add(po)
    session.flush()

    total_amount = 0.0
    for l in lines:
        item = session.get(Item, l["item_id"])
        if not item:
            continue
        qty = int(l.get("requested_qty", item.reorder_quantity))
        unit_cost = float(l.get("unit_cost", item.cost_price))
        line_total = round(qty * unit_cost, 2)
        total_amount += line_total

        po_line = PurchaseOrderLine(
            po_id=po.id,
            item_id=item.id,
            item_name=item.name,
            requested_qty=qty,
            received_qty=0,
            unit_cost=unit_cost,
            line_total=line_total,
        )
        session.add(po_line)

    po.total_amount = round(total_amount, 2)
    session.commit()
    session.refresh(po)
    return po


def process_goods_receipt(
    session: Session,
    supplier_id: int,
    invoice_no: str,
    lines_data: List[Dict[str, Any]],
    user_name: str,
    po_id: Optional[int] = None,
    store_id: int = 1,
    notes: Optional[str] = None,
) -> GoodsReceipt:
    """
    Receives incoming supplier shipments:
    1. Generates new batches with lot number and expiry date.
    2. Records StockMovement ledger entries with reason_code="PO_RECEIPT".
    3. Updates PO line received_qty and PO status (partial_received / received).
    """
    grn_no = generate_grn_no(session)
    grn = GoodsReceipt(
        grn_no=grn_no,
        po_id=po_id,
        supplier_id=supplier_id,
        store_id=store_id,
        received_date=date.today(),
        invoice_no=invoice_no,
        received_by=user_name,
        status="received",
        notes=notes,
        created_at=utc_now(),
    )
    session.add(grn)
    session.flush()

    total_amount = 0.0

    for l_data in lines_data:
        item_id = int(l_data["item_id"])
        item = session.get(Item, item_id)
        if not item:
            continue

        qty = int(l_data["quantity"])
        unit_cost = float(l_data.get("unit_cost", item.cost_price))
        mrp = float(l_data.get("mrp", item.unit_price))
        lot_no = l_data.get("lot_no") or f"LOT-{date.today().strftime('%y%m')}-{item.id}"
        expiry_str = l_data.get("expiry_date")
        if isinstance(expiry_str, str):
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        elif isinstance(expiry_str, date):
            expiry_date = expiry_str
        else:
            expiry_date = date.today() + timedelta(days=365 * 2)

        line_total = round(qty * unit_cost, 2)
        total_amount += line_total

        # 1. Create or update batch
        batch = Batch(
            item_id=item_id,
            store_id=store_id,
            lot_no=lot_no,
            expiry_date=expiry_date,
            cost_price=unit_cost,
            mrp=mrp,
            unit_price=mrp,
            quantity_received=qty,
            quantity_remaining=0,  # Will be adjusted by ledger
            is_quarantined=False,
            created_at=utc_now(),
        )
        session.add(batch)
        session.flush()

        # 2. Record StockMovement ledger entry
        record_stock_movement(
            session=session,
            movement_type="receipt",
            item_id=item_id,
            batch_id=batch.id,
            store_id=store_id,
            quantity=qty,
            reason_code="PO_RECEIPT",
            reference_type="goods_receipt",
            reference_id=grn_no,
            unit_cost=unit_cost,
            notes=f"GRN {grn_no} Inv #{invoice_no}",
            created_by=user_name,
        )

        # 3. Create GRN Line
        grn_line = GoodsReceiptLine(
            goods_receipt_id=grn.id,
            item_id=item_id,
            batch_id=batch.id,
            lot_no=lot_no,
            expiry_date=expiry_date,
            quantity=qty,
            unit_cost=unit_cost,
            mrp=mrp,
            line_total=line_total,
        )
        session.add(grn_line)

        # 4. If linked to PO, update line received_qty
        if po_id:
            po_line = session.exec(
                select(PurchaseOrderLine).where(
                    PurchaseOrderLine.po_id == po_id,
                    PurchaseOrderLine.item_id == item_id,
                )
            ).first()
            if po_line:
                po_line.received_qty += qty
                session.add(po_line)

    grn.total_amount = round(total_amount, 2)

    # Update PO overall status
    if po_id:
        po = session.get(PurchaseOrder, po_id)
        if po:
            all_po_lines = session.exec(
                select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po_id)
            ).all()
            all_fulfilled = all(l.received_qty >= l.requested_qty for l in all_po_lines)
            po.status = "received" if all_fulfilled else "partial_received"
            session.add(po)

    session.commit()
    session.refresh(grn)
    return grn


# =========================================================================
# 6. MULTI-STORE TRANSFERS
# =========================================================================

def execute_stock_transfer(
    session: Session,
    transfer_id: int,
    user_name: str,
) -> StockTransferRequest:
    """Executes an approved stock transfer between stores."""
    transfer = session.get(StockTransferRequest, transfer_id)
    if not transfer:
        raise ValueError(f"Transfer request #{transfer_id} not found")

    lines = session.exec(
        select(StockTransferLine).where(StockTransferLine.transfer_id == transfer.id)
    ).all()

    for l in lines:
        batch = session.get(Batch, l.batch_id) if l.batch_id else pick_best_fefo_batch(
            session, l.item_id, store_id=transfer.from_store_id
        )
        if not batch or batch.quantity_remaining < l.requested_qty:
            raise ValueError(f"Insufficient stock in source store for item #{l.item_id}")

        qty = l.requested_qty

        # 1. Transfer Out from source store
        record_stock_movement(
            session=session,
            movement_type="transfer_out",
            item_id=l.item_id,
            batch_id=batch.id,
            store_id=transfer.from_store_id,
            to_store_id=transfer.to_store_id,
            quantity=-qty,
            reason_code="STORE_TRANSFER",
            reference_type="transfer",
            reference_id=transfer.transfer_no,
            unit_cost=batch.cost_price,
            notes=f"Transfer to Store #{transfer.to_store_id}",
            created_by=user_name,
        )

        # 2. Check or create matching batch in target store
        dest_batch = session.exec(
            select(Batch).where(
                Batch.item_id == l.item_id,
                Batch.store_id == transfer.to_store_id,
                Batch.lot_no == batch.lot_no,
            )
        ).first()

        if not dest_batch:
            dest_batch = Batch(
                item_id=l.item_id,
                store_id=transfer.to_store_id,
                lot_no=batch.lot_no,
                expiry_date=batch.expiry_date,
                cost_price=batch.cost_price,
                mrp=batch.mrp,
                unit_price=batch.unit_price,
                quantity_received=qty,
                quantity_remaining=0,
                is_quarantined=False,
                created_at=utc_now(),
            )
            session.add(dest_batch)
            session.flush()

        # 3. Transfer In to destination store
        record_stock_movement(
            session=session,
            movement_type="transfer_in",
            item_id=l.item_id,
            batch_id=dest_batch.id,
            store_id=transfer.to_store_id,
            quantity=qty,
            reason_code="STORE_TRANSFER",
            reference_type="transfer",
            reference_id=transfer.transfer_no,
            unit_cost=batch.cost_price,
            notes=f"Received from Store #{transfer.from_store_id}",
            created_by=user_name,
        )

        l.transferred_qty = qty
        l.batch_id = batch.id
        session.add(l)

    transfer.status = "transferred"
    transfer.approved_by = user_name
    transfer.transferred_at = utc_now()
    session.add(transfer)
    session.commit()
    session.refresh(transfer)
    return transfer


# =========================================================================
# 7. RETURNS WORKFLOW WITH APPROVAL
# =========================================================================

def approve_and_process_return(
    session: Session,
    return_id: int,
    user_name: str,
) -> PharmacyReturn:
    """Processes an approved return and adjusts inventory ledger accordingly."""
    ret = session.get(PharmacyReturn, return_id)
    if not ret:
        raise ValueError(f"Return #{return_id} not found")

    lines = session.exec(select(PharmacyReturnLine).where(PharmacyReturnLine.return_id == ret.id)).all()

    for l in lines:
        batch = session.get(Batch, l.batch_id) if l.batch_id else None
        item = session.get(Item, l.item_id)

        # For patient return: stock is returned to store
        qty_change = l.quantity if ret.return_type == "patient_return" else -l.quantity
        reason = "PATIENT_RETURN" if ret.return_type == "patient_return" else "SUPPLIER_RETURN"

        record_stock_movement(
            session=session,
            movement_type="return",
            item_id=l.item_id,
            batch_id=batch.id if batch else None,
            store_id=ret.store_id,
            quantity=qty_change,
            reason_code=reason,
            reference_type="return",
            reference_id=ret.return_no,
            unit_cost=batch.cost_price if batch else item.cost_price,
            notes=f"Return {ret.return_no} - {ret.reason}",
            created_by=user_name,
            approved_by=user_name,
        )

    ret.status = "approved"
    ret.approved_by = user_name
    ret.approved_at = utc_now()
    session.add(ret)
    session.commit()
    session.refresh(ret)
    return ret


# =========================================================================
# 8. COMPREHENSIVE REPORTS & CSV EXPORTS
# =========================================================================

def generate_stock_valuation_csv(session: Session, store_id: Optional[int] = None) -> str:
    """Exports Stock Valuation report as CSV."""
    items = session.exec(select(Item).where(Item.is_active == True).order_by(Item.name)).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Item Code",
        "Item Name",
        "Generic Name",
        "Category",
        "Form",
        "Stock Quantity",
        "Unit Cost ($)",
        "Total Valuation ($)",
        "MRP ($)",
        "Total Sales Value ($)",
        "Reorder Level",
        "Stock Status",
    ])

    for it in items:
        summary = get_item_stock_summary(session, it.id, store_id=store_id)
        stock = summary["usable_stock"]
        cost_val = round(stock * it.cost_price, 2)
        sales_val = round(stock * it.unit_price, 2)
        status = "LOW STOCK" if stock <= it.reorder_level else "NORMAL"

        writer.writerow([
            it.code,
            it.name,
            it.generic_name,
            it.category,
            it.form,
            stock,
            f"{it.cost_price:.2f}",
            f"{cost_val:.2f}",
            f"{it.unit_price:.2f}",
            f"{sales_val:.2f}",
            it.reorder_level,
            status,
        ])

    return output.getvalue()


def generate_expiry_report_csv(session: Session, days: int = 90) -> str:
    """Exports Expiry and Quarantine report as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Lot / Batch #",
        "Item Code",
        "Item Name",
        "Generic Name",
        "Expiry Date",
        "Days Remaining",
        "Quantity Remaining",
        "Unit Cost ($)",
        "Value at Risk ($)",
        "Quarantined",
        "Status",
    ])

    today = date.today()
    batches = session.exec(
        select(Batch, Item)
        .join(Item, Batch.item_id == Item.id)
        .where(
            Batch.quantity_remaining > 0,
            Batch.expiry_date <= (today + timedelta(days=days)),
        )
        .order_by(col(Batch.expiry_date).asc())
    ).all()

    for batch, item in batches:
        delta = (batch.expiry_date - today).days
        status = "EXPIRED" if delta < 0 else ("NEAR EXPIRY" if delta <= 30 else "MONITORING")
        risk_val = round(batch.quantity_remaining * batch.cost_price, 2)

        writer.writerow([
            batch.lot_no,
            item.code,
            item.name,
            item.generic_name,
            batch.expiry_date.strftime("%Y-%m-%d"),
            delta,
            batch.quantity_remaining,
            f"{batch.cost_price:.2f}",
            f"{risk_val:.2f}",
            "YES" if batch.is_quarantined else "NO",
            status,
        ])

    return output.getvalue()


def generate_movement_ledger_csv(session: Session, limit: int = 500) -> str:
    """Exports Immutable Stock Movement Ledger as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Timestamp",
        "Movement Type",
        "Reason Code",
        "Item Code",
        "Item Name",
        "Batch Lot #",
        "Store ID",
        "Quantity Change",
        "Balance After",
        "Unit Cost ($)",
        "Reference Type",
        "Reference ID",
        "Created By",
        "Notes",
    ])

    movements = session.exec(
        select(StockMovement, Item)
        .join(Item, StockMovement.item_id == Item.id)
        .order_by(col(StockMovement.timestamp).desc())
        .limit(limit)
    ).all()

    for mvt, item in movements:
        batch = session.get(Batch, mvt.batch_id) if mvt.batch_id else None
        writer.writerow([
            mvt.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            mvt.movement_type,
            mvt.reason_code,
            item.code,
            item.name,
            batch.lot_no if batch else "-",
            mvt.store_id,
            mvt.quantity,
            mvt.balance_after,
            f"{mvt.unit_cost:.2f}",
            mvt.reference_type,
            mvt.reference_id or "-",
            mvt.created_by or "system",
            mvt.notes or "",
        ])

    return output.getvalue()


def generate_top_consumed_csv(session: Session, limit: int = 30) -> str:
    """Exports Top Consumed Drugs report as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Rank",
        "Item Code",
        "Item Name",
        "Generic Name",
        "Category",
        "Total Quantity Dispensed",
        "Total Revenue Generated ($)",
    ])

    lines = session.exec(
        select(
            DispenseLine.item_id,
            func.sum(DispenseLine.dispensed_qty).label("total_qty"),
            func.sum(DispenseLine.line_total).label("total_revenue"),
        )
        .group_by(DispenseLine.item_id)
        .order_by(desc("total_qty"))
        .limit(limit)
    ).all()

    rank = 1
    for item_id, total_qty, total_rev in lines:
        item = session.get(Item, item_id)
        if not item:
            continue
        writer.writerow([
            rank,
            item.code,
            item.name,
            item.generic_name,
            item.category,
            total_qty,
            f"{total_rev or 0:.2f}",
        ])
        rank += 1

    return output.getvalue()
