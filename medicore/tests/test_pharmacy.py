import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
import pytest
from sqlmodel import Session, col, select

from medicore.core.database import engine
from medicore.core.events import Event, event_bus
from medicore.core.models import User
from medicore.modules.patients.models import Patient
from medicore.modules.pharmacy.models import (
    Batch,
    Dispense,
    DispenseLine,
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
    generate_stock_valuation_csv,
    generate_top_consumed_csv,
    get_expired_batches,
    get_fefo_batches,
    get_item_stock_summary,
    get_low_stock_items,
    get_near_expiry_batches,
    get_reorder_suggestions,
    handle_prescription_signed,
    pick_best_fefo_batch,
    quarantine_expired_batches,
    record_stock_movement,
    utc_now,
)


def test_fefo_batch_picking_and_expired_blocking():
    """
    Test First-Expiry-First-Out (FEFO) batch picking:
    1. Earlier non-expired batch is selected before later batch.
    2. Expired batches are excluded from FEFO.
    3. Quarantined batches are excluded.
    4. Attempting to dispense an expired or quarantined batch raises ValueError.
    """
    today = date.today()
    with Session(engine) as session:
        # Create test item
        item = Item(
            code=f"TEST-FEFO-{datetime.now().timestamp()}",
            name="FEFO Test Amoxicillin 500mg",
            generic_name="Amoxicillin",
            category="Antibiotics",
            cost_price=10.0,
            unit_price=15.0,
            reorder_level=20,
            reorder_quantity=50,
        )
        session.add(item)
        session.commit()
        session.refresh(item)

        # Batch 1: Expired 15 days ago
        b_expired = Batch(
            item_id=item.id,
            store_id=1,
            lot_no="LOT-EXP",
            expiry_date=today - timedelta(days=15),
            unit_price=15.0,
            quantity_received=50,
            quantity_remaining=50,
        )
        session.add(b_expired)

        # Batch 2: Quarantined
        b_quarantined = Batch(
            item_id=item.id,
            store_id=1,
            lot_no="LOT-QUAR",
            expiry_date=today + timedelta(days=40),
            unit_price=15.0,
            quantity_received=50,
            quantity_remaining=50,
            is_quarantined=True,
            quarantine_reason="Test quarantine",
        )
        session.add(b_quarantined)

        # Batch 3: Healthy but expires in 90 days
        b_later = Batch(
            item_id=item.id,
            store_id=1,
            lot_no="LOT-LATER",
            expiry_date=today + timedelta(days=90),
            unit_price=15.0,
            quantity_received=100,
            quantity_remaining=100,
        )
        session.add(b_later)

        # Batch 4: Healthy and expires in 30 days (earliest valid)
        b_earlier = Batch(
            item_id=item.id,
            store_id=1,
            lot_no="LOT-EARLY",
            expiry_date=today + timedelta(days=30),
            unit_price=15.0,
            quantity_received=50,
            quantity_remaining=50,
        )
        session.add(b_earlier)
        session.commit()

        # FEFO retrieval should only include valid batches, sorted by expiry date ascending
        fefo_batches = get_fefo_batches(session, item.id, store_id=1)
        assert len(fefo_batches) == 2
        assert fefo_batches[0].lot_no == "LOT-EARLY"
        assert fefo_batches[1].lot_no == "LOT-LATER"

        # Best FEFO pick
        best = pick_best_fefo_batch(session, item.id, store_id=1)
        assert best is not None
        assert best.lot_no == "LOT-EARLY"

        # Test dispense validation: Attempting to complete with expired batch must fail
        disp = Dispense(
            dispense_no=generate_dispense_no(session),
            patient_name="Test Patient",
            store_id=1,
            status="pending",
        )
        session.add(disp)
        session.flush()

        d_line = DispenseLine(
            dispense_id=disp.id,
            item_id=item.id,
            item_name=item.name,
            prescribed_qty=10,
            batch_id=b_expired.id,
        )
        session.add(d_line)
        session.commit()

        with pytest.raises(ValueError, match="Cannot dispense EXPIRED batch"):
            complete_dispense_order(
                session=session,
                dispense_id=disp.id,
                lines_input=[{"line_id": d_line.id, "batch_id": b_expired.id, "dispensed_qty": 10}],
                user_name="pharmacist.lisa",
            )

        # Attempting to dispense quarantined batch must fail
        with pytest.raises(ValueError, match="Cannot dispense quarantined batch"):
            complete_dispense_order(
                session=session,
                dispense_id=disp.id,
                lines_input=[{"line_id": d_line.id, "batch_id": b_quarantined.id, "dispensed_qty": 10}],
                user_name="pharmacist.lisa",
            )


def test_immutable_stock_movement_ledger():
    """
    Test Immutable Stock Ledger:
    1. Every physical change is recorded as a StockMovement with a reason code.
    2. Quantities cannot be updated directly; balance is derived through movements.
    3. Negative stock guard prevents overdrafts.
    """
    with Session(engine) as session:
        item = Item(
            code=f"TEST-LEDGER-{datetime.now().timestamp()}",
            name="Ledger Test Paracetamol",
            generic_name="Paracetamol",
            category="Analgesics",
            cost_price=5.0,
            unit_price=10.0,
        )
        session.add(item)
        session.commit()
        session.refresh(item)

        batch = Batch(
            item_id=item.id,
            store_id=1,
            lot_no="LOT-LEDGER-01",
            expiry_date=date.today() + timedelta(days=365),
            unit_price=10.0,
            cost_price=5.0,
            quantity_received=0,
            quantity_remaining=0,
        )
        session.add(batch)
        session.commit()
        session.refresh(batch)

        # 1. Receipt: +100 units
        mv1 = record_stock_movement(
            session=session,
            movement_type="receipt",
            item_id=item.id,
            store_id=1,
            quantity=100,
            reason_code="PO_RECEIPT",
            batch_id=batch.id,
            reference_type="goods_receipt",
            reference_id="GRN-TEST-100",
            notes="Initial receipt from vendor",
            created_by="storekeeper.dan",
        )
        session.commit()
        session.refresh(batch)
        assert batch.quantity_remaining == 100
        assert mv1.balance_after == 100
        assert mv1.quantity == 100

        # 2. Dispense: -30 units
        mv2 = record_stock_movement(
            session=session,
            movement_type="dispense",
            item_id=item.id,
            store_id=1,
            quantity=-30,
            reason_code="RX_DISPENSE",
            batch_id=batch.id,
            reference_type="dispense",
            reference_id="DSP-TEST-001",
            notes="Prescription dispense",
            created_by="pharmacist.lisa",
        )
        session.commit()
        session.refresh(batch)
        assert batch.quantity_remaining == 70
        assert mv2.balance_after == 70
        assert mv2.quantity == -30

        # 3. Stock Adjustment: -5 units (damaged or broken vial)
        mv3 = record_stock_movement(
            session=session,
            movement_type="adjustment",
            item_id=item.id,
            store_id=1,
            quantity=-5,
            reason_code="STOCK_TAKE_ADJUSTMENT",
            batch_id=batch.id,
            notes="Physical inventory recount discrepancy",
            created_by="storekeeper.dan",
            approved_by="admin",
        )
        session.commit()
        session.refresh(batch)
        assert batch.quantity_remaining == 65
        assert mv3.balance_after == 65

        # 4. Negative stock guard: attempting to deduct 100 from 65 units remaining must raise ValueError
        with pytest.raises(ValueError, match="Insufficient stock in Batch"):
            record_stock_movement(
                session=session,
                movement_type="dispense",
                item_id=item.id,
                store_id=1,
                quantity=-100,
                reason_code="RX_DISPENSE",
                batch_id=batch.id,
            )

        # 5. Verify movements ledger history
        movements = session.exec(
            select(StockMovement)
            .where(StockMovement.item_id == item.id)
            .order_by(col(StockMovement.timestamp).asc())
        ).all()
        assert len(movements) == 3
        assert movements[0].reason_code == "PO_RECEIPT"
        assert movements[1].reason_code == "RX_DISPENSE"
        assert movements[2].reason_code == "STOCK_TAKE_ADJUSTMENT"


def test_low_stock_and_expiry_alerts_and_quarantine():
    """
    Test inventory alerts:
    1. Low stock detection based on reorder level.
    2. Near-expiry detection within configurable days threshold.
    3. Automated quarantine of expired stock.
    """
    today = date.today()
    with Session(engine) as session:
        # Create item with reorder_level=50 and low stock (10 remaining)
        it_low = Item(
            code=f"TEST-ALERT-LOW-{datetime.now().timestamp()}",
            name="Low Stock Item",
            generic_name="Alert Drug Low",
            category="Cardiovascular",
            reorder_level=50,
            reorder_quantity=150,
        )
        session.add(it_low)
        session.flush()

        b_low = Batch(
            item_id=it_low.id,
            store_id=1,
            lot_no="LOT-LOW-01",
            expiry_date=today + timedelta(days=200),
            quantity_received=10,
            quantity_remaining=10,
        )
        session.add(b_low)

        # Item with near expiry (< 60 days)
        it_near = Item(
            code=f"TEST-ALERT-NEAR-{datetime.now().timestamp()}",
            name="Near Expiry Item",
            generic_name="Alert Drug Near",
            category="Cardiovascular",
            reorder_level=10,
            reorder_quantity=50,
        )
        session.add(it_near)
        session.flush()

        b_near = Batch(
            item_id=it_near.id,
            store_id=1,
            lot_no="LOT-NEAR-01",
            expiry_date=today + timedelta(days=25),  # 25 days left
            quantity_received=40,
            quantity_remaining=40,
        )
        session.add(b_near)

        # Item with expired stock
        it_exp = Item(
            code=f"TEST-ALERT-EXP-{datetime.now().timestamp()}",
            name="Expired Stock Item",
            generic_name="Alert Drug Exp",
            category="Cardiovascular",
            reorder_level=10,
            reorder_quantity=50,
        )
        session.add(it_exp)
        session.flush()

        b_exp = Batch(
            item_id=it_exp.id,
            store_id=1,
            lot_no="LOT-EXP-01",
            expiry_date=today - timedelta(days=5),  # Expired 5 days ago
            quantity_received=30,
            quantity_remaining=30,
            is_quarantined=False,
        )
        session.add(b_exp)
        session.commit()

        # 1. Low stock detection
        low_items = get_low_stock_items(session, store_id=1)
        assert any(l["item"].id == it_low.id for l in low_items)

        # 2. Near expiry detection
        near_batches = get_near_expiry_batches(session, days_threshold=60, store_id=1)
        assert any(b[0].id == b_near.id for b in near_batches)

        # 3. Expired batches detection
        expired_batches = get_expired_batches(session, store_id=1)
        assert any(b[0].id == b_exp.id for b in expired_batches)

        # 4. Quarantine expired batches
        quarantined_count = quarantine_expired_batches(session, user_name="storekeeper.dan")
        assert quarantined_count >= 1

        session.refresh(b_exp)
        assert b_exp.is_quarantined is True
        assert "Automated quarantine" in b_exp.quarantine_reason

        # Quarantined batch should now have an immutable ledger movement
        q_mv = session.exec(
            select(StockMovement)
            .where(
                StockMovement.batch_id == b_exp.id,
                StockMovement.movement_type == "quarantine",
            )
        ).first()
        assert q_mv is not None
        assert q_mv.reason_code == "QUARANTINE_LOCK"


def test_prescription_signed_event_creates_dispense_queue():
    """
    Test event subscriber:
    When 'prescription.signed' is emitted from Consultations,
    the Pharmacy module automatically creates a pending Dispense record with matched items.
    """
    rx_id = 99901
    enc_id = 88801
    pt_name = "Eleanor Vance"

    payload = {
        "prescription_id": rx_id,
        "encounter_id": enc_id,
        "patient_id": 1,
        "patient_name": pt_name,
        "patient_mrn": "MC-2026-99901",
        "doctor_id": 2,
        "doctor_name": "Dr. Sarah Chen, MD",
        "allergy_warnings": "Penicillin sensitivity reported.",
        "safety_overrides": [{"interaction": "Drug-allergy warning", "override_reason": "Low risk; patient tolerated previously"}],
        "items": [
            {
                "drug_name": "Amoxil (Amoxicillin) 500 mg",
                "generic_name": "Amoxicillin",
                "quantity": 21,
                "dosage": "500 mg",
                "frequency": "TID",
            }
        ],
    }

    event = Event(name="prescription.signed", payload=payload)
    handle_prescription_signed(event)

    with Session(engine) as session:
        disp = session.exec(select(Dispense).where(Dispense.prescription_id == rx_id)).first()
        assert disp is not None
        assert disp.status == "pending"
        assert disp.patient_name == pt_name
        assert "Penicillin" in (disp.allergy_warnings or "")
        assert "Drug-allergy warning" in (disp.safety_overrides or "")

        lines = session.exec(select(DispenseLine).where(DispenseLine.dispense_id == disp.id)).all()
        assert len(lines) == 1
        assert lines[0].prescribed_qty == 21
        assert "Amoxicillin" in lines[0].item_name

        # Idempotency check: handling same event again does not duplicate
        initial_count = len(session.exec(select(Dispense).where(Dispense.prescription_id == rx_id)).all())
        handle_prescription_signed(event)
        after_count = len(session.exec(select(Dispense).where(Dispense.prescription_id == rx_id)).all())
        assert initial_count == after_count


def test_dispense_order_completion_and_domain_event():
    """
    Test clinical dispense completion:
    1. Status transitions: pending -> dispensed.
    2. Batch stock deducted via StockMovement.
    3. Emits 'dispense.completed' event.
    """
    events_captured = []

    def spy_listener(ev: Event):
        events_captured.append(ev)

    event_bus.subscribe("dispense.completed", spy_listener)

    with Session(engine) as session:
        # Create item with batch
        item = Item(
            code=f"TEST-DISP-{datetime.now().timestamp()}",
            name="Clinical Dispense Ciprofloxacin 500mg",
            generic_name="Ciprofloxacin",
            category="Antibiotics",
            cost_price=8.0,
            unit_price=16.0,
        )
        session.add(item)
        session.commit()
        session.refresh(item)

        batch = Batch(
            item_id=item.id,
            store_id=1,
            lot_no="LOT-DISP-01",
            expiry_date=date.today() + timedelta(days=300),
            unit_price=16.0,
            cost_price=8.0,
            quantity_received=100,
            quantity_remaining=100,
        )
        session.add(batch)
        session.commit()
        session.refresh(batch)

        disp = Dispense(
            dispense_no=generate_dispense_no(session),
            patient_name="Arthur Pendelton",
            store_id=1,
            status="pending",
        )
        session.add(disp)
        session.flush()

        d_line = DispenseLine(
            dispense_id=disp.id,
            item_id=item.id,
            item_name=item.name,
            prescribed_qty=14,
        )
        session.add(d_line)
        session.commit()
        session.refresh(d_line)

        # Complete dispense
        updated_disp = complete_dispense_order(
            session=session,
            dispense_id=disp.id,
            lines_input=[
                {
                    "line_id": d_line.id,
                    "batch_id": batch.id,
                    "dispensed_qty": 14,
                }
            ],
            user_name="pharmacist.lisa",
        )

        assert updated_disp.status == "dispensed"
        assert updated_disp.dispensed_by == "pharmacist.lisa"
        assert updated_disp.total_amount == 14 * 16.0

        # Check stock deduction
        session.refresh(batch)
        assert batch.quantity_remaining == 86

        # Check StockMovement
        mv = session.exec(
            select(StockMovement)
            .where(
                StockMovement.batch_id == batch.id,
                StockMovement.movement_type == "dispense",
            )
        ).first()
        assert mv is not None
        assert mv.quantity == -14
        assert mv.reason_code == "RX_DISPENSE"

    # Verify event emission
    assert any(e.name == "dispense.completed" for e in events_captured)
    event_bus.unsubscribe("dispense.completed", spy_listener)


def test_partial_dispensing_and_generic_substitution():
    """
    Test partial dispensing and generic substitution:
    1. Dispensing less than prescribed quantity sets status to 'partial'.
    2. Generic substitution requires a documented reason.
    """
    with Session(engine) as session:
        item = Item(
            code=f"TEST-SUB-{datetime.now().timestamp()}",
            name="Generic Rosuvastatin 20mg",
            generic_name="Rosuvastatin",
            category="Cardiovascular",
            unit_price=12.0,
            cost_price=6.0,
        )
        session.add(item)
        session.commit()
        session.refresh(item)

        batch = Batch(
            item_id=item.id,
            store_id=1,
            lot_no="LOT-SUB-01",
            expiry_date=date.today() + timedelta(days=200),
            unit_price=12.0,
            quantity_received=50,
            quantity_remaining=50,
        )
        session.add(batch)
        session.commit()
        session.refresh(batch)

        disp = Dispense(
            dispense_no=generate_dispense_no(session),
            patient_name="Clara Higgins",
            store_id=1,
            status="pending",
        )
        session.add(disp)
        session.flush()

        d_line = DispenseLine(
            dispense_id=disp.id,
            item_id=item.id,
            item_name=item.name,
            prescribed_item_name="Crestor (Rosuvastatin) 20mg",
            prescribed_qty=30,
        )
        session.add(d_line)
        session.commit()
        session.refresh(d_line)

        # 1. Generic substitution without reason must fail
        with pytest.raises(ValueError, match="requires a documented reason"):
            complete_dispense_order(
                session=session,
                dispense_id=disp.id,
                lines_input=[
                    {
                        "line_id": d_line.id,
                        "batch_id": batch.id,
                        "dispensed_qty": 15,
                        "is_generic_substitution": True,
                        "substitution_reason": "",  # Empty reason
                    }
                ],
                user_name="pharmacist.lisa",
            )

        # 2. Generic substitution with reason & partial quantity (15 of 30)
        res = complete_dispense_order(
            session=session,
            dispense_id=disp.id,
            lines_input=[
                {
                    "line_id": d_line.id,
                    "batch_id": batch.id,
                    "dispensed_qty": 15,
                    "is_generic_substitution": True,
                    "substitution_reason": "Brand Crestor out of stock; bioequivalent generic dispensed per prescriber approval.",
                }
            ],
            user_name="pharmacist.lisa",
        )

        assert res.status == "partial"
        session.refresh(d_line)
        assert d_line.is_generic_substitution is True
        assert "bioequivalent generic" in (d_line.substitution_reason or "")
        assert d_line.dispensed_qty == 15
        assert batch.quantity_remaining == 35


def test_counter_otc_sales_flow(client):
    """
    Test Counter Sales (OTC POS):
    1. Walk-in patient sale without prescription.
    2. Items added and checked out.
    3. Ledger records 'OTC_SALE' movement.
    """
    with Session(engine) as session:
        batch = session.exec(
            select(Batch).where(
                Batch.store_id == 1,
                Batch.quantity_remaining > 5,
                Batch.is_quarantined == False,
                Batch.expiry_date >= date.today(),
            )
        ).first()
        item = session.get(Item, batch.item_id)

        initial_qty = batch.quantity_remaining
        item_id = item.id
        batch_id = batch.id

    # Post OTC checkout via client
    cart_payload = json.dumps([
        {
            "item_id": item_id,
            "item_name": item.name,
            "batch_id": batch_id,
            "quantity": 2,
            "unit_price": item.unit_price,
            "line_total": round(item.unit_price * 2, 2),
        }
    ])

    resp = client.post(
        "/pharmacy/otc/checkout",
        data={
            "customer_name": "Walk-in John",
            "store_id": 1,
            "cart_json": cart_payload,
            "payment_method": "Cash",
        },
        follow_redirects=False,
    )
    assert resp.status_code in [200, 303]

    # Verify stock reduction and ledger
    with Session(engine) as session:
        refreshed_batch = session.get(Batch, batch_id)
        assert refreshed_batch.quantity_remaining == initial_qty - 2

        mv = session.exec(
            select(StockMovement)
            .where(
                StockMovement.batch_id == batch_id,
                StockMovement.reason_code == "OTC_SALE",
            )
            .order_by(col(StockMovement.timestamp).desc())
        ).first()
        assert mv is not None
        assert mv.quantity == -2


def test_multi_store_transfers():
    """
    Test multi-store stock transfer request and execution:
    1. Transfer from Main Store to Ward Sub-Store.
    2. Execution creates 'transfer_out' in source store and 'transfer_in' in destination store.
    """
    with Session(engine) as session:
        main_store = session.exec(select(PharmacyStore).where(PharmacyStore.code == "MAIN")).first()
        ward_store = session.exec(select(PharmacyStore).where(PharmacyStore.code == "WARD")).first()

        batch = session.exec(
            select(Batch).where(
                Batch.store_id == main_store.id,
                Batch.quantity_remaining >= 20,
                Batch.is_quarantined == False,
            )
        ).first()
        item = session.get(Item, batch.item_id)

        # Create transfer request
        trf = StockTransferRequest(
            transfer_no=f"TRF-TEST-{datetime.now().timestamp()}",
            from_store_id=main_store.id,
            to_store_id=ward_store.id,
            status="pending",
            requested_by="nurse.john",
        )
        session.add(trf)
        session.flush()

        t_line = StockTransferLine(
            transfer_id=trf.id,
            item_id=item.id,
            requested_qty=10,
            batch_id=batch.id,
        )
        session.add(t_line)
        session.commit()
        session.refresh(trf)

        # Execute transfer
        executed = execute_stock_transfer(session, trf.id, user_name="storekeeper.dan")
        assert executed.status == "transferred"
        assert executed.approved_by == "storekeeper.dan"

        # Check ledger movements
        movements = session.exec(
            select(StockMovement)
            .where(StockMovement.reference_id == executed.transfer_no)
        ).all()
        assert len(movements) == 2
        types = [m.movement_type for m in movements]
        assert "transfer_out" in types
        assert "transfer_in" in types


def test_pharmacy_returns_approval_workflow():
    """
    Test pharmacy returns:
    1. Patient return approval restores quantity to inventory ledger.
    """
    with Session(engine) as session:
        item = session.exec(select(Item)).first()
        batch = session.exec(
            select(Batch).where(
                Batch.item_id == item.id,
                Batch.is_quarantined == False,
            )
        ).first()

        initial_qty = batch.quantity_remaining

        ret = PharmacyReturn(
            return_no=f"RET-TEST-{datetime.now().timestamp()}",
            return_type="patient_return",
            store_id=1,
            status="pending",
            reason="Patient switched medication regimen",
            created_by="pharmacist.lisa",
        )
        session.add(ret)
        session.flush()

        r_line = PharmacyReturnLine(
            return_id=ret.id,
            item_id=item.id,
            batch_id=batch.id,
            quantity=5,
            unit_price=item.unit_price,
            line_total=round(item.unit_price * 5, 2),
        )
        session.add(r_line)
        session.commit()
        session.refresh(ret)

        # Approve and process return
        approved_ret = approve_and_process_return(session, ret.id, user_name="pharmacist.lisa")
        assert approved_ret.status == "approved"
        assert approved_ret.approved_by == "pharmacist.lisa"

        # Verify stock increment
        session.refresh(batch)
        assert batch.quantity_remaining == initial_qty + 5

        # Verify ledger
        mv = session.exec(
            select(StockMovement)
            .where(
                StockMovement.batch_id == batch.id,
                StockMovement.movement_type == "return",
                StockMovement.reason_code == "PATIENT_RETURN",
            )
        ).first()
        assert mv is not None
        assert mv.quantity == 5


def test_reports_csv_exports(client):
    """
    Test all 4 analytical CSV reports:
    1. Stock Valuation CSV
    2. Expiry Risk Report CSV
    3. Movement Ledger History CSV
    4. Top Consumed Medications CSV
    """
    # 1. Valuation
    resp_val = client.get("/pharmacy/reports/valuation/csv")
    assert resp_val.status_code == 200
    assert "text/csv" in resp_val.headers["content-type"]
    assert "Item Code,Item Name,Generic Name" in resp_val.text

    # 2. Expiry
    resp_exp = client.get("/pharmacy/reports/expiry/csv?days=90")
    assert resp_exp.status_code == 200
    assert "text/csv" in resp_exp.headers["content-type"]
    assert "Lot / Batch #,Item Code,Item Name" in resp_exp.text

    # 3. Ledger
    resp_led = client.get("/pharmacy/reports/ledger/csv")
    assert resp_led.status_code == 200
    assert "text/csv" in resp_led.headers["content-type"]
    assert "Timestamp,Movement Type,Reason Code" in resp_led.text

    # 4. Top Consumed
    resp_top = client.get("/pharmacy/reports/top-consumed/csv?days=30")
    assert resp_top.status_code == 200
    assert "text/csv" in resp_top.headers["content-type"]
    assert "Rank,Item Code,Item Name,Generic Name" in resp_top.text


def test_pharmacy_ui_and_widget_endpoints(client):
    """
    Test Pharmacy UI endpoints and HTMX partials:
    1. Main pharmacy dashboard / queue
    2. Queue table HTMX partial
    3. Low stock and expiry alert widget
    """
    # Main screen
    resp_home = client.get("/pharmacy")
    assert resp_home.status_code == 200
    assert "Pharmacy & Dispensing" in resp_home.text or "Dispensing Queue" in resp_home.text

    # HTMX Queue partial
    resp_table = client.get("/pharmacy/queue/table?status_filter=pending")
    assert resp_table.status_code == 200
    assert "table" in resp_table.text.lower() or "pending" in resp_table.text.lower()

    # Dashboard alerts widget
    resp_widget = client.get("/pharmacy/widget/alerts")
    assert resp_widget.status_code == 200
    assert "Pharmacy Alerts" in resp_widget.text or "Low Stock" in resp_widget.text
