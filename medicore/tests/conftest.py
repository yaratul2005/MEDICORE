import os
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from medicore.core.config import settings
from medicore.core.database import get_session
from medicore.core.models import Permission, Role, RolePermissionLink, User, UserRoleLink
from medicore.core.security import hash_password
from medicore.seed.seeder import seed_database
from main import app

# Ensure testing settings
settings.debug = True


@pytest.fixture(scope="session")
def client():
    # Run seeder to ensure base tables and records exist
    seed_database()
    with TestClient(app) as test_client:
        yield test_client
