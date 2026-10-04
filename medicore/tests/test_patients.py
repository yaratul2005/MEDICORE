from sqlmodel import Session, col, select
from medicore.core.database import engine
from medicore.core.models import AuditLog
from medicore.modules.patients.models import Patient


def test_patients_index_page(client):
    response = client.get("/patients")
    assert response.status_code == 200
    assert "Patients Directory" in response.text
    assert "detail-pane" in response.text


def test_patients_table_partial(client):
    # Unfiltered
    resp = client.get("/patients/table")
    assert resp.status_code == 200
    assert "<table" in resp.text
    assert "MRN" in resp.text

    # Filtered by search query
    resp_search = client.get("/patients/table?q=MC-")
    assert resp_search.status_code == 200
    assert resp_search.status_code == 200


def test_patient_detail_pane(client):
    with Session(engine) as session:
        first_patient = session.exec(select(Patient)).first()
        assert first_patient is not None

    resp = client.get(f"/patients/detail/{first_patient.id}")
    assert resp.status_code == 200
    assert first_patient.mrn in resp.text
    assert first_patient.full_name in resp.text
    assert "Demographics" in resp.text


def test_duplicate_patient_detection(client):
    with Session(engine) as session:
        patient = session.exec(select(Patient)).first()
        assert patient is not None

    # Test match by phone
    resp = client.post(
        "/patients/check-duplicate",
        data={"phone": patient.phone}
    )
    assert resp.status_code == 200
    assert "Possible Duplicate Patient Detected" in resp.text
    assert patient.mrn in resp.text

    # Test non-matching
    resp_clean = client.post(
        "/patients/check-duplicate",
        data={"phone": "+1-999-000-1111", "first_name": "UniqueZ99", "last_name": "Nonexistent", "date_of_birth": "1990-01-01"}
    )
    assert resp_clean.status_code == 200
    assert "Possible Duplicate Patient Detected" not in resp_clean.text


def test_create_patient_and_audit_event(client):
    new_patient_payload = {
        "first_name": "Arthur",
        "last_name": "Pendelton",
        "date_of_birth": "1985-06-15",
        "gender": "Male",
        "blood_group": "O+",
        "phone": "+1 (555) 777-8899",
        "email": "arthur.pendelton@example.com",
        "allergies": "Penicillin",
        "status": "Active",
        "address": "123 Camelot Way, Avalon",
        "custom_fields.insurance_provider": "Blue Cross Blue Shield",
        "custom_fields.primary_language": "English",
        "custom_fields.vip_status": "true",
    }

    resp = client.post("/patients", data=new_patient_payload)
    assert resp.status_code == 200

    with Session(engine) as session:
        created = session.exec(
            select(Patient).where(Patient.phone == "+1 (555) 777-8899").order_by(col(Patient.id).desc())
        ).first()
        assert created is not None
        assert created.first_name == "Arthur"
        assert created.mrn.startswith("MC-")
        assert created.custom_fields.get("insurance_provider") == "Blue Cross Blue Shield"

        # Check audit log entry
        audit = session.exec(
            select(AuditLog).where(
                AuditLog.module == "patients",
                AuditLog.entity_id == str(created.id),
                AuditLog.action == "CREATE"
            )
        ).first()
        assert audit is not None
        assert audit.changes.get("full_name") == "Arthur Pendelton"
