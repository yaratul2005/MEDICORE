"""Add database-level unique constraint to prevent double-booking

Revision ID: 0003_double_booking_protection
Revises: 0002_appointments
Create Date: 2026-10-08 14:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0003_double_booking_protection'
down_revision: Union[str, None] = '0002_appointments'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Deduplicate any existing non-cancelled duplicate appointments prior to index creation
    # Keeps the earliest created appointment per slot, marks duplicates as Cancelled
    op.execute("""
        UPDATE appointments
        SET status = 'Cancelled'
        WHERE id IN (
            SELECT a1.id
            FROM appointments a1
            INNER JOIN appointments a2
            ON a1.doctor_id = a2.doctor_id
            AND a1.appointment_date = a2.appointment_date
            AND a1.start_time = a2.start_time
            AND a1.status != 'Cancelled'
            AND a2.status != 'Cancelled'
            AND a1.id > a2.id
        )
    """)

    # 2. Enforce strict database-level partial unique index:
    # No two active/non-cancelled appointments can exist for the same clinician, date, and start_time.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_appointment_doctor_slot
        ON appointments (doctor_id, appointment_date, start_time)
        WHERE status != 'Cancelled'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_appointment_doctor_slot")
