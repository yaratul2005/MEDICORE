from sqlmodel import Session, select
from medicore.core.config import detect_default_env, settings
from medicore.core.database import engine
from medicore.core.events import Event, event_bus
from medicore.core.models import AuditLog, Permission, Role, User
from medicore.core.registry import registry
from medicore.core.security import hash_password, user_has_permission, verify_password
from medicore.core.settings import settings_registry


def test_config_profiles():
    env = detect_default_env()
    assert env in ("colab", "local", "prod")
    assert settings.app_name == "MediCore"
    assert settings.database_url.startswith("sqlite:///")


def test_password_hashing():
    pwd = "SecretClinicalPassword!99"
    hashed = hash_password(pwd)
    assert verify_password(pwd, hashed) is True
    assert verify_password("wrong", hashed) is False


def test_event_bus():
    received_payloads = []

    def handler(evt: Event):
        received_payloads.append(evt.payload.get("test_key"))

    event_bus.subscribe("test.domain.event", handler)
    event_bus.emit("test.domain.event", {"test_key": "clinical_payload_val"})

    assert "clinical_payload_val" in received_payloads
    event_bus.unsubscribe("test.domain.event", handler)


def test_settings_registry():
    settings_registry.init_defaults()
    profile = settings_registry.get("hospital", "profile")
    assert profile is not None
    assert "name" in profile

    # Test MRN generation
    mrn1 = settings_registry.generate_mrn()
    mrn2 = settings_registry.generate_mrn()
    assert mrn1 != mrn2
    assert mrn1.startswith("MC-")


def test_module_registry():
    registry.auto_discover()
    manifests = registry.get_active_manifests()
    manifest_names = [m.name for m in manifests]
    assert "patients" in manifest_names

    with Session(engine) as session:
        admin = session.exec(select(User).where(User.username == "admin")).first()
        assert admin is not None
        nav = registry.get_nav_entries(admin, session)
        assert len(nav) > 0

        # Global search
        results = registry.search_all("MC-", admin, session)
        assert isinstance(results, list)
