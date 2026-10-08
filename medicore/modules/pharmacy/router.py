from datetime import date, datetime
import json
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlmodel import Session, col, desc, func, or_, select

from medicore.core.database import get_session
from medicore.core.models import AuditLog, User
from medicore.core.security import get_current_user, require_permission, user_has_permission
from medicore.modules.patients.models import Patient
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
from medicore.modules.pharmacy.service import (
    approve_and_process_return,
    complete_dispense_order,
    create_purchase_order,
    execute_stock_transfer,
    generate_dispense_no,
    generate_expiry_report_csv,
    generate_movement_ledger_csv,
    generate_return_no,
    generate_stock_valuation_csv,
    generate_top_consumed_csv,
    generate_transfer_no,
    get_expired_batches,
    get_fefo_batches,
    get_item_stock_summary,
    get_low_stock_items,
    get_near_expiry_batches,
    get_reorder_suggestions,
    pick_best_fefo_batch,
    process_goods_receipt,
    quarantine_expired_batches,
    record_stock_movement,
    utc_now,
)
from medicore.ui.templating import get_ui_context, templates

router = APIRouter(prefix="/pharmacy", tags=["Pharmacy"])


# =========================================================================
# DISPENSING QUEUE & OVERVIEW
# =========================================================================

@router.get("", response_class=HTMLResponse)
def pharmacy_home(
    request: Request,
    tab: str = Query("queue"),
    status_filter: str = Query("pending"),
    q: Optional[str] = Query(""),
    user: User = Depends(require_permission("pharmacy.dispense.read")),
    session: Session = Depends(get_session),
):
    """Main pharmacy operational hub and dispensing queue."""
    # Summary KPI statistics
    pending_count = session.exec(
        select(func.count(Dispense.id)).where(Dispense.status == "pending")
    ).one()
    in_progress_count = session.exec(
        select(func.count(Dispense.id)).where(Dispense.status == "in_progress")
    ).one()
    dispensed_today_count = session.exec(
        select(func.count(Dispense.id)).where(
            Dispense.status.in_(["dispensed", "partial"]),
            func.date(Dispense.created_at) == date.today(),
        )
    ).one()
    low_stock_count = len(get_low_stock_items(session, store_id=1))
    near_expiry_count = len(get_near_expiry_batches(session, days_threshold=60, store_id=1))
    expired_count = len(get_expired_batches(session, store_id=1))

    # Dispense Query
    stmt = select(Dispense).order_by(col(Dispense.created_at).desc())
    if status_filter and status_filter != "all":
        stmt = stmt.where(Dispense.status == status_filter)

    query_str = (q or "").strip()
    if query_str:
        term = f"%{query_str}%"
        stmt = stmt.where(
            or_(
                Dispense.dispense_no.ilike(term),
                Dispense.patient_name.ilike(term),
                Dispense.patient_mrn.ilike(term),
                Dispense.doctor_name.ilike(term),
            )
        )

    dispenses = session.exec(stmt.limit(50)).all()
    stores = session.exec(select(PharmacyStore).where(PharmacyStore.is_active == True)).all()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        active_tab=tab,
        status_filter=status_filter,
        q=query_str,
        dispenses=dispenses,
        stores=stores,
        pending_count=pending_count,
        in_progress_count=in_progress_count,
        dispensed_today_count=dispensed_today_count,
        low_stock_count=low_stock_count,
        near_expiry_count=near_expiry_count,
        expired_count=expired_count,
    )
    return templates.TemplateResponse("modules/pharmacy/index.html", ctx)


@router.get("/queue/table", response_class=HTMLResponse)
def queue_table_partial(
    request: Request,
    status_filter: str = Query("pending"),
    q: Optional[str] = Query(""),
    user: User = Depends(require_permission("pharmacy.dispense.read")),
    session: Session = Depends(get_session),
):
    """HTMX partial returning the live queue table."""
    stmt = select(Dispense).order_by(col(Dispense.created_at).desc())
    if status_filter and status_filter != "all":
        stmt = stmt.where(Dispense.status == status_filter)

    query_str = (q or "").strip()
    if query_str:
        term = f"%{query_str}%"
        stmt = stmt.where(
            or_(
                Dispense.dispense_no.ilike(term),
                Dispense.patient_name.ilike(term),
                Dispense.patient_mrn.ilike(term),
                Dispense.doctor_name.ilike(term),
            )
        )

    dispenses = session.exec(stmt.limit(50)).all()
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        dispenses=dispenses,
        status_filter=status_filter,
        q=query_str,
    )
    return templates.TemplateResponse("modules/pharmacy/partials/queue_table.html", ctx)


# =========================================================================
# DEDICATED DISPENSE SCREEN
# =========================================================================

@router.get("/dispense/{dispense_id}", response_class=HTMLResponse)
def dispense_detail_screen(
    dispense_id: int,
    request: Request,
    user: User = Depends(require_permission("pharmacy.dispense.read")),
    session: Session = Depends(get_session),
):
    """
    Dedicated Clinical Dispensing Screen:
    - Prescription details
    - Allergy alert banner
    - Physician safety overrides
    - FEFO batch picker (blocks expired batches)
    - Partial dispensing inputs
    - Generic substitution modal & reason logging
    """
    dispense = session.get(Dispense, dispense_id)
    if not dispense:
        raise HTTPException(status_code=404, detail="Dispense order not found")

    lines = session.exec(
        select(DispenseLine).where(DispenseLine.dispense_id == dispense.id)
    ).all()

    # If pending, shift to in_progress to claim lock
    if dispense.status == "pending":
        dispense.status = "in_progress"
        session.add(dispense)
        session.commit()
        session.refresh(dispense)

    # Patient clinical allergy lookup
    patient_allergies = []
    if dispense.patient_id:
        patient = session.get(Patient, dispense.patient_id)
        if patient and patient.allergies:
            patient_allergies = [a.strip() for a in patient.allergies.split(",") if a.strip()]

    # Parse doctor safety overrides if present
    safety_overrides_list = []
    if dispense.safety_overrides:
        try:
            safety_overrides_list = json.loads(dispense.safety_overrides)
        except Exception:
            safety_overrides_list = [{"reason": dispense.safety_overrides}]

    # Pre-fetch FEFO batches and generic substitution alternatives per line
    line_details = []
    for line in lines:
        fefo_batches = get_fefo_batches(session, line.item_id, store_id=dispense.store_id, allow_expired=False)
        all_item_batches = session.exec(
            select(Batch).where(Batch.item_id == line.item_id, Batch.store_id == dispense.store_id)
        ).all()

        item = session.get(Item, line.item_id)
        # Potential generic substitutes in the same generic class
        generic_substitutes = []
        if item:
            generic_substitutes = session.exec(
                select(Item).where(
                    Item.generic_name == item.generic_name,
                    Item.id != item.id,
                    Item.is_active == True,
                )
            ).all()

        line_details.append({
            "line": line,
            "item": item,
            "fefo_batches": fefo_batches,
            "all_batches": all_item_batches,
            "generic_substitutes": generic_substitutes,
            "current_batch": session.get(Batch, line.batch_id) if line.batch_id else (fefo_batches[0] if fefo_batches else None),
        })

    can_process = user_has_permission(user, "pharmacy.dispense.process", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        dispense=dispense,
        line_details=line_details,
        patient_allergies=patient_allergies,
        safety_overrides_list=safety_overrides_list,
        can_process=can_process,
    )
    return templates.TemplateResponse("modules/pharmacy/dispense_detail.html", ctx)


@router.post("/dispense/{dispense_id}/complete")
async def execute_dispense(
    dispense_id: int,
    request: Request,
    user: User = Depends(require_permission("pharmacy.dispense.process")),
    session: Session = Depends(get_session),
):
    """Executes dispense order, deducts stock via ledger, and emits events."""
    form_data = await request.form()
    dispense = session.get(Dispense, dispense_id)
    if not dispense:
        raise HTTPException(status_code=404, detail="Dispense order not found")

    lines = session.exec(
        select(DispenseLine).where(DispenseLine.dispense_id == dispense.id)
    ).all()

    lines_input = []
    for line in lines:
        batch_id_str = form_data.get(f"batch_{line.id}")
        qty_str = form_data.get(f"qty_{line.id}", str(line.prescribed_qty))
        is_sub = bool(form_data.get(f"sub_{line.id}"))
        sub_reason = form_data.get(f"sub_reason_{line.id}", "").strip()

        lines_input.append({
            "line_id": line.id,
            "batch_id": int(batch_id_str) if batch_id_str else None,
            "dispensed_qty": int(qty_str) if qty_str.isdigit() else line.prescribed_qty,
            "is_generic_substitution": is_sub,
            "substitution_reason": sub_reason,
        })

    try:
        updated_dispense = complete_dispense_order(
            session=session,
            dispense_id=dispense.id,
            lines_input=lines_input,
            user_name=user.full_name or user.username,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Redirect to dispense overview
    return RedirectResponse(
        url=f"/pharmacy?tab=queue&status_filter={updated_dispense.status}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/dispense/{dispense_id}/reject")
def reject_dispense_order(
    dispense_id: int,
    reason: str = Form(...),
    user: User = Depends(require_permission("pharmacy.dispense.process")),
    session: Session = Depends(get_session),
):
    """Rejects a dispensing request with a documented reason."""
    dispense = session.get(Dispense, dispense_id)
    if not dispense:
        raise HTTPException(status_code=404, detail="Dispense order not found")

    dispense.status = "rejected"
    dispense.notes = f"Rejected by {user.full_name}: {reason.strip()}"
    session.add(dispense)
    session.commit()

    return RedirectResponse(
        url="/pharmacy?tab=queue&status_filter=rejected",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# =========================================================================
# COUNTER SALES (OTC) & POS
# =========================================================================

@router.get("/otc", response_class=HTMLResponse)
def counter_sales_screen(
    request: Request,
    user: User = Depends(require_permission("pharmacy.dispense.process")),
    session: Session = Depends(get_session),
):
    """Point-of-sale counter sales screen for over-the-counter purchases."""
    stores = session.exec(select(PharmacyStore).where(PharmacyStore.is_active == True)).all()
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        stores=stores,
    )
    return templates.TemplateResponse("modules/pharmacy/otc.html", ctx)


@router.get("/items/search", response_class=HTMLResponse)
def items_search_partial(
    request: Request,
    q: Optional[str] = Query(""),
    store_id: int = Query(1),
    user: User = Depends(require_permission("pharmacy.dispense.read")),
    session: Session = Depends(get_session),
):
    """Barcode / SKU / Name search for quick POS cart addition."""
    query_str = (q or "").strip()
    if not query_str:
        return HTMLResponse("<div class='p-3 text-xs text-slate-400'>Type SKU, barcode, or drug name...</div>")

    term = f"%{query_str}%"
    stmt = (
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
        .limit(10)
    )
    items = session.exec(stmt).all()

    results = []
    for it in items:
        fefo = pick_best_fefo_batch(session, it.id, store_id=store_id)
        stock_sum = get_item_stock_summary(session, it.id, store_id=store_id)
        results.append({
            "item": it,
            "best_batch": fefo,
            "stock": stock_sum["usable_stock"],
        })

    ctx = get_ui_context(request, user=user, session=session, results=results)
    return templates.TemplateResponse("modules/pharmacy/partials/otc_search_results.html", ctx)


@router.post("/otc/checkout")
async def checkout_otc_sale(
    request: Request,
    user: User = Depends(require_permission("pharmacy.dispense.process")),
    session: Session = Depends(get_session),
):
    """Processes a counter sale: registers Dispense, creates ledger entries, and completes order."""
    body = await request.form()
    customer_name = body.get("customer_name", "Walk-in Customer").strip() or "Walk-in Customer"
    store_id = int(body.get("store_id", 1))

    # Parse items in cart
    cart_items_json = body.get("cart_items") or body.get("cart_json") or "[]"
    try:
        cart_items = json.loads(cart_items_json)
    except Exception:
        cart_items = []

    if not cart_items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    dispense_no = generate_dispense_no(session)
    dispense = Dispense(
        dispense_no=dispense_no,
        dispense_type="counter_otc",
        patient_name=customer_name,
        store_id=store_id,
        status="pending",
        created_at=utc_now(),
    )
    session.add(dispense)
    session.flush()

    lines_input = []
    for entry in cart_items:
        item_id = int(entry["item_id"])
        qty = int(entry.get("qty") or entry.get("quantity") or 1)
        item = session.get(Item, item_id)
        if not item:
            continue

        best_batch = pick_best_fefo_batch(session, item_id, store_id=store_id)
        if not best_batch:
            raise HTTPException(status_code=400, detail=f"No stock available for {item.name}")

        line = DispenseLine(
            dispense_id=dispense.id,
            item_id=item.id,
            item_name=item.name,
            prescribed_qty=qty,
            dispensed_qty=qty,
            batch_id=best_batch.id,
            lot_no=best_batch.lot_no,
            expiry_date=best_batch.expiry_date,
            unit_price=best_batch.unit_price or item.unit_price,
            line_total=round((best_batch.unit_price or item.unit_price) * qty, 2),
        )
        session.add(line)
        session.flush()

        lines_input.append({
            "line_id": line.id,
            "batch_id": best_batch.id,
            "dispensed_qty": qty,
        })

    session.commit()

    completed = complete_dispense_order(
        session=session,
        dispense_id=dispense.id,
        lines_input=lines_input,
        user_name=user.full_name or user.username,
    )

    return RedirectResponse(
        url=f"/pharmacy/dispense/{completed.id}?receipt=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# =========================================================================
# INVENTORY, BATCHES & STOCK MOVEMENTS
# =========================================================================

@router.get("/inventory", response_class=HTMLResponse)
def inventory_list_screen(
    request: Request,
    store_id: int = Query(1),
    category: Optional[str] = Query(""),
    low_stock_only: bool = Query(False),
    q: Optional[str] = Query(""),
    page: int = Query(1),
    user: User = Depends(require_permission("pharmacy.stock.read")),
    session: Session = Depends(get_session),
):
    """Dense inventory overview with multi-store filtering and low-stock filters."""
    stmt = select(Item).where(Item.is_active == True)
    if category:
        stmt = stmt.where(Item.category == category)

    query_str = (q or "").strip()
    if query_str:
        term = f"%{query_str}%"
        stmt = stmt.where(
            or_(
                Item.code.ilike(term),
                Item.name.ilike(term),
                Item.generic_name.ilike(term),
                Item.brand_name.ilike(term),
            )
        )

    all_items = session.exec(stmt.order_by(Item.name)).all()

    # Calculate derived stocks
    inventory_entries = []
    for it in all_items:
        stock_sum = get_item_stock_summary(session, it.id, store_id=store_id)
        if low_stock_only and stock_sum["usable_stock"] > it.reorder_level:
            continue
        inventory_entries.append({
            "item": it,
            "stock": stock_sum["usable_stock"],
            "total_stock": stock_sum["total_stock"],
            "near_expiry": stock_sum["near_expiry_count"],
            "expired": stock_sum["expired_count"],
            "is_low": stock_sum["usable_stock"] <= it.reorder_level,
        })

    # Pagination
    limit = 25
    total_count = len(inventory_entries)
    total_pages = max(1, (total_count + limit - 1) // limit)
    offset = (page - 1) * limit
    paginated_entries = inventory_entries[offset:offset + limit]

    stores = session.exec(select(PharmacyStore).where(PharmacyStore.is_active == True)).all()
    categories = session.exec(select(Item.category).distinct()).all()

    can_manage = user_has_permission(user, "pharmacy.stock.manage", session)
    can_approve = user_has_permission(user, "pharmacy.stock.approve", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        inventory_entries=paginated_entries,
        stores=stores,
        current_store_id=store_id,
        categories=categories,
        current_category=category,
        low_stock_only=low_stock_only,
        q=query_str,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        can_manage=can_manage,
        can_approve=can_approve,
    )
    return templates.TemplateResponse("modules/pharmacy/inventory.html", ctx)


@router.get("/inventory/item/{item_id}", response_class=HTMLResponse)
def item_detail_screen(
    item_id: int,
    request: Request,
    store_id: int = Query(1),
    user: User = Depends(require_permission("pharmacy.stock.read")),
    session: Session = Depends(get_session),
):
    """Detailed view of an item with active batches and immutable stock movement ledger."""
    item = session.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    batches = session.exec(
        select(Batch)
        .where(Batch.item_id == item.id)
        .order_by(col(Batch.expiry_date).asc())
    ).all()

    movements = session.exec(
        select(StockMovement)
        .where(StockMovement.item_id == item.id)
        .order_by(col(StockMovement.timestamp).desc())
        .limit(30)
    ).all()

    stock_sum = get_item_stock_summary(session, item.id, store_id=store_id)
    stores = session.exec(select(PharmacyStore).where(PharmacyStore.is_active == True)).all()

    can_manage = user_has_permission(user, "pharmacy.stock.manage", session)
    can_approve = user_has_permission(user, "pharmacy.stock.approve", session)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        item=item,
        batches=batches,
        movements=movements,
        stock_sum=stock_sum,
        stores=stores,
        store_id=store_id,
        can_manage=can_manage,
        can_approve=can_approve,
    )
    return templates.TemplateResponse("modules/pharmacy/item_detail.html", ctx)


@router.post("/inventory/adjust")
def adjust_inventory_stock(
    item_id: int = Form(...),
    batch_id: Optional[int] = Form(None),
    store_id: int = Form(1),
    adjustment_qty: int = Form(...),  # Positive or negative
    reason: str = Form(...),
    user: User = Depends(require_permission("pharmacy.stock.approve")),
    session: Session = Depends(get_session),
):
    """
    Inventory Stock Adjustment:
    Strictly recorded as a StockMovement ledger entry.
    Requires 'pharmacy.stock.approve' permission.
    """
    if adjustment_qty == 0:
        raise HTTPException(status_code=400, detail="Adjustment quantity cannot be 0")

    movement = record_stock_movement(
        session=session,
        movement_type="adjustment",
        item_id=item_id,
        batch_id=batch_id,
        store_id=store_id,
        quantity=adjustment_qty,
        reason_code="STOCK_TAKE_ADJUSTMENT",
        reference_type="adjustment",
        notes=f"Stock adjustment: {reason.strip()}",
        created_by=user.full_name or user.username,
        approved_by=user.full_name or user.username,
    )
    session.commit()

    return RedirectResponse(
        url=f"/pharmacy/inventory/item/{item_id}?store_id={store_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/inventory/quarantine")
def quarantine_expired(
    user: User = Depends(require_permission("pharmacy.stock.manage")),
    session: Session = Depends(get_session),
):
    """One-click batch action: Locks all expired stock into quarantine."""
    count = quarantine_expired_batches(session, user_name=user.full_name or user.username)
    return RedirectResponse(
        url="/pharmacy?tab=alerts&quarantined=" + str(count),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# =========================================================================
# PURCHASING, POs & GOODS RECEIPT (GRN)
# =========================================================================

@router.get("/purchasing", response_class=HTMLResponse)
def purchasing_dashboard(
    request: Request,
    tab: str = Query("orders"),
    po_status: str = Query("all"),
    user: User = Depends(require_permission("pharmacy.po.manage")),
    session: Session = Depends(get_session),
):
    """Purchasing manager: Purchase Orders and AI Reorder Suggestions."""
    po_query = select(PurchaseOrder).order_by(col(PurchaseOrder.created_at).desc())
    if po_status != "all":
        po_query = po_query.where(PurchaseOrder.status == po_status)
    purchase_orders = session.exec(po_query.limit(50)).all()

    # Reorder suggestions from low-stock analytics
    reorder_suggestions = get_reorder_suggestions(session, store_id=1)
    suppliers = session.exec(select(Supplier).where(Supplier.is_active == True)).all()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        active_tab=tab,
        purchase_orders=purchase_orders,
        reorder_suggestions=reorder_suggestions,
        suppliers=suppliers,
        po_status=po_status,
    )
    return templates.TemplateResponse("modules/pharmacy/purchasing.html", ctx)


@router.get("/purchasing/po/{po_id}", response_class=HTMLResponse)
def purchase_order_detail(
    po_id: int,
    request: Request,
    user: User = Depends(require_permission("pharmacy.po.manage")),
    session: Session = Depends(get_session),
):
    """View Purchase Order and line receipt progress."""
    po = session.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(status_code=404, detail="Purchase Order not found")

    supplier = session.get(Supplier, po.supplier_id)
    lines = session.exec(
        select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id)
    ).all()
    grns = session.exec(
        select(GoodsReceipt).where(GoodsReceipt.po_id == po.id)
    ).all()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        po=po,
        supplier=supplier,
        lines=lines,
        grns=grns,
    )
    return templates.TemplateResponse("modules/pharmacy/po_detail.html", ctx)


@router.post("/purchasing/orders/from-suggestions")
def create_po_from_suggestions_action(
    supplier_id: int = Form(...),
    notes: Optional[str] = Form(None),
    user: User = Depends(require_permission("pharmacy.po.create")),
    session: Session = Depends(get_session),
):
    """1-Click generation of purchase order from current reorder suggestions."""
    suggestions = get_reorder_suggestions(session, store_id=1)
    if not suggestions:
        raise HTTPException(status_code=400, detail="No reorder suggestions available")

    lines = [
        {
            "item_id": s["item_id"],
            "requested_qty": s["suggested_qty"],
            "unit_cost": s["unit_cost"],
        }
        for s in suggestions
    ]

    po = create_purchase_order(
        session=session,
        supplier_id=supplier_id,
        lines=lines,
        user_name=user.full_name or user.username,
        notes=notes or "Automated restock order based on consumption and reorder levels",
    )

    return RedirectResponse(
        url=f"/pharmacy/purchasing/po/{po.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/purchasing/grn")
async def process_grn_action(
    request: Request,
    user: User = Depends(require_permission("pharmacy.stock.manage")),
    session: Session = Depends(get_session),
):
    """Receives goods against a PO or direct supplier invoice, recording batches & ledger."""
    form_data = await request.form()
    supplier_id = int(form_data.get("supplier_id"))
    invoice_no = form_data.get("invoice_no", "").strip() or f"INV-{date.today().strftime('%Y%m%d')}"
    po_id_str = form_data.get("po_id")
    po_id = int(po_id_str) if po_id_str and po_id_str.isdigit() else None
    store_id = int(form_data.get("store_id", 1))

    # Parse dynamic lines
    item_ids = form_data.getlist("item_id")
    quantities = form_data.getlist("quantity")
    lot_nos = form_data.getlist("lot_no")
    expiries = form_data.getlist("expiry_date")
    unit_costs = form_data.getlist("unit_cost")
    mrps = form_data.getlist("mrp")

    lines_data = []
    for i in range(len(item_ids)):
        if not item_ids[i] or not quantities[i]:
            continue
        lines_data.append({
            "item_id": int(item_ids[i]),
            "quantity": int(quantities[i]),
            "lot_no": lot_nos[i] if i < len(lot_nos) and lot_nos[i] else f"LOT-{date.today().strftime('%y%m')}-{item_ids[i]}",
            "expiry_date": expiries[i] if i < len(expiries) else None,
            "unit_cost": float(unit_costs[i]) if i < len(unit_costs) and unit_costs[i] else 0.0,
            "mrp": float(mrps[i]) if i < len(mrps) and mrps[i] else 0.0,
        })

    grn = process_goods_receipt(
        session=session,
        supplier_id=supplier_id,
        invoice_no=invoice_no,
        lines_data=lines_data,
        user_name=user.full_name or user.username,
        po_id=po_id,
        store_id=store_id,
    )

    return RedirectResponse(
        url=f"/pharmacy/purchasing?tab=orders&grn={grn.grn_no}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# =========================================================================
# MULTI-STORE TRANSFERS
# =========================================================================

@router.get("/transfers", response_class=HTMLResponse)
def transfers_dashboard(
    request: Request,
    user: User = Depends(require_permission("pharmacy.stock.read")),
    session: Session = Depends(get_session),
):
    """Multi-store transfer requests and movements."""
    transfers = session.exec(
        select(StockTransferRequest).order_by(col(StockTransferRequest.created_at).desc())
    ).all()
    stores = session.exec(select(PharmacyStore).where(PharmacyStore.is_active == True)).all()
    items = session.exec(select(Item).where(Item.is_active == True).order_by(Item.name).limit(100)).all()

    can_approve = user_has_permission(user, "pharmacy.stock.approve", session)
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        transfers=transfers,
        stores=stores,
        items=items,
        can_approve=can_approve,
    )
    return templates.TemplateResponse("modules/pharmacy/transfers.html", ctx)


@router.post("/transfers/new")
def create_transfer_request_action(
    from_store_id: int = Form(...),
    to_store_id: int = Form(...),
    item_id: int = Form(...),
    requested_qty: int = Form(...),
    notes: Optional[str] = Form(None),
    user: User = Depends(require_permission("pharmacy.stock.manage")),
    session: Session = Depends(get_session),
):
    """Creates a stock transfer request from one store to another."""
    if from_store_id == to_store_id:
        raise HTTPException(status_code=400, detail="Source and destination stores must be different")

    transfer_no = generate_transfer_no(session)
    trf = StockTransferRequest(
        transfer_no=transfer_no,
        from_store_id=from_store_id,
        to_store_id=to_store_id,
        status="pending",
        requested_by=user.full_name or user.username,
        notes=notes,
        created_at=utc_now(),
    )
    session.add(trf)
    session.flush()

    line = StockTransferLine(
        transfer_id=trf.id,
        item_id=item_id,
        requested_qty=requested_qty,
    )
    session.add(line)
    session.commit()

    return RedirectResponse(url="/pharmacy/transfers", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/transfers/{transfer_id}/approve")
def approve_transfer_action(
    transfer_id: int,
    user: User = Depends(require_permission("pharmacy.stock.approve")),
    session: Session = Depends(get_session),
):
    """Approves and executes the stock transfer, updating dual ledgers."""
    execute_stock_transfer(session, transfer_id, user_name=user.full_name or user.username)
    return RedirectResponse(url="/pharmacy/transfers", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================================
# RETURNS (PATIENT & SUPPLIER)
# =========================================================================

@router.get("/returns", response_class=HTMLResponse)
def returns_dashboard(
    request: Request,
    user: User = Depends(require_permission("pharmacy.stock.read")),
    session: Session = Depends(get_session),
):
    """Returns management with approval workflows."""
    returns = session.exec(
        select(PharmacyReturn).order_by(col(PharmacyReturn.created_at).desc())
    ).all()
    dispenses = session.exec(
        select(Dispense).where(Dispense.status == "dispensed").order_by(col(Dispense.created_at).desc()).limit(30)
    ).all()
    items = session.exec(select(Item).where(Item.is_active == True).limit(50)).all()

    can_approve = user_has_permission(user, "pharmacy.stock.approve", session)
    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        returns=returns,
        dispenses=dispenses,
        items=items,
        can_approve=can_approve,
    )
    return templates.TemplateResponse("modules/pharmacy/returns.html", ctx)


@router.post("/returns/new")
def create_return_action(
    return_type: str = Form("patient_return"),
    dispense_id: Optional[int] = Form(None),
    item_id: int = Form(...),
    quantity: int = Form(...),
    reason: str = Form(...),
    user: User = Depends(require_permission("pharmacy.stock.manage")),
    session: Session = Depends(get_session),
):
    """Creates a return request awaiting supervisor approval."""
    item = session.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    unit_price = item.unit_price
    line_total = round(unit_price * quantity, 2)

    ret_no = generate_return_no(session)
    ret = PharmacyReturn(
        return_no=ret_no,
        return_type=return_type,
        dispense_id=dispense_id,
        store_id=1,
        status="pending",
        total_amount=line_total,
        reason=reason.strip(),
        created_by=user.full_name or user.username,
        created_at=utc_now(),
    )
    session.add(ret)
    session.flush()

    line = PharmacyReturnLine(
        return_id=ret.id,
        item_id=item_id,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
        reason=reason.strip(),
    )
    session.add(line)
    session.commit()

    return RedirectResponse(url="/pharmacy/returns", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/returns/{return_id}/approve")
def approve_return_action(
    return_id: int,
    user: User = Depends(require_permission("pharmacy.stock.approve")),
    session: Session = Depends(get_session),
):
    """Supervisor approves return and stock is adjusted in the ledger."""
    approve_and_process_return(session, return_id, user_name=user.full_name or user.username)
    return RedirectResponse(url="/pharmacy/returns", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================================
# REPORTS & CSV EXPORTS
# =========================================================================

@router.get("/reports", response_class=HTMLResponse)
def reports_dashboard(
    request: Request,
    report_type: str = Query("valuation"),
    user: User = Depends(require_permission("pharmacy.reports.read")),
    session: Session = Depends(get_session),
):
    """Pharmacy analytical reports with interactive tables and CSV exports."""
    items = session.exec(select(Item).where(Item.is_active == True).order_by(Item.name).limit(50)).all()
    valuation_rows = []
    for it in items:
        sum_data = get_item_stock_summary(session, it.id)
        stock = sum_data["usable_stock"]
        valuation_rows.append({
            "item": it,
            "stock": stock,
            "cost_value": round(stock * it.cost_price, 2),
            "sales_value": round(stock * it.unit_price, 2),
            "is_low": stock <= it.reorder_level,
        })

    today = date.today()
    expiring_batches = session.exec(
        select(Batch, Item)
        .join(Item, Batch.item_id == Item.id)
        .where(Batch.quantity_remaining > 0, Batch.expiry_date <= (today + timedelta(days=90)))
        .order_by(col(Batch.expiry_date).asc())
        .limit(50)
    ).all()

    recent_movements = session.exec(
        select(StockMovement, Item)
        .join(Item, StockMovement.item_id == Item.id)
        .order_by(col(StockMovement.timestamp).desc())
        .limit(50)
    ).all()

    top_consumed = session.exec(
        select(
            DispenseLine.item_id,
            func.sum(DispenseLine.dispensed_qty).label("total_qty"),
            func.sum(DispenseLine.line_total).label("total_revenue"),
        )
        .group_by(DispenseLine.item_id)
        .order_by(desc("total_qty"))
        .limit(20)
    ).all()

    top_drugs = []
    for item_id, total_qty, total_rev in top_consumed:
        it = session.get(Item, item_id)
        if it:
            top_drugs.append({
                "item": it,
                "total_qty": total_qty,
                "total_revenue": round(total_rev or 0, 2),
            })

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        report_type=report_type,
        valuation_rows=valuation_rows,
        expiring_batches=expiring_batches,
        recent_movements=recent_movements,
        top_drugs=top_drugs,
    )
    return templates.TemplateResponse("modules/pharmacy/reports.html", ctx)


@router.get("/reports/valuation/csv")
def export_valuation_csv(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission("pharmacy.reports.read")),
):
    """CSV export of Stock Valuation report."""
    csv_data = generate_stock_valuation_csv(session)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=pharmacy_valuation_{date.today()}.csv"},
    )


@router.get("/reports/expiry/csv")
def export_expiry_csv(
    days: int = Query(90),
    session: Session = Depends(get_session),
    user: User = Depends(require_permission("pharmacy.reports.read")),
):
    """CSV export of Expiry & Quarantine report."""
    csv_data = generate_expiry_report_csv(session, days=days)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=pharmacy_expiry_report_{date.today()}.csv"},
    )


@router.get("/reports/ledger/csv")
def export_ledger_csv(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission("pharmacy.reports.read")),
):
    """CSV export of Immutable Stock Movement Ledger."""
    csv_data = generate_movement_ledger_csv(session)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=pharmacy_ledger_{date.today()}.csv"},
    )


@router.get("/reports/top-consumed/csv")
def export_top_consumed_csv(
    session: Session = Depends(get_session),
    user: User = Depends(require_permission("pharmacy.reports.read")),
):
    """CSV export of Top Consumed Drugs report."""
    csv_data = generate_top_consumed_csv(session)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=pharmacy_top_consumed_{date.today()}.csv"},
    )


# =========================================================================
# DASHBOARD WIDGET
# =========================================================================

@router.get("/widget/alerts", response_class=HTMLResponse)
def pharmacy_alerts_widget(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Dashboard widget showing critical stock alerts and quarantine actions."""
    low_stock = get_low_stock_items(session, store_id=1)
    near_expiry = get_near_expiry_batches(session, days_threshold=60, store_id=1)
    expired = get_expired_batches(session, store_id=1)

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        low_stock_count=len(low_stock),
        near_expiry_count=len(near_expiry),
        expired_count=len(expired),
        top_low_stock=low_stock[:5],
    )
    return templates.TemplateResponse("modules/pharmacy/widgets/alerts.html", ctx)
