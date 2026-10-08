from logging.config import fileConfig
import os
import sys
from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from medicore.core.config import settings
from medicore.core.models import (
    AuditLog,
    CustomFieldDefinition,
    Permission,
    Role,
    RolePermissionLink,
    Setting,
    User,
    UserRoleLink,
)
from medicore.modules.patients.models import Patient
from medicore.modules.appointments.models import Appointment, Doctor, QueueToken
from medicore.modules.consultations.models import (
    ClinicalNote,
    Diagnosis,
    Encounter,
    FollowUp,
    Order,
    Prescription,
    PrescriptionItem,
    Referral,
    Vitals,
)
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
from medicore.modules.laboratory.models import (
    LabOrder,
    LabOrderItem,
    LabReport,
    Parameter,
    ReferenceRange,
    Result,
    Specimen,
    TestCatalog,
    TestPanel,
)


# Alembic Config object
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata

# Override sqlalchemy.url with dynamic medicore settings database_url
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
