"""Add doctors, appointments, and queue_tokens tables

Revision ID: 0002_appointments
Revises: 0001_initial
Create Date: 2026-10-04 21:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = '0002_appointments'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. doctors table
    op.create_table(
        'doctors',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('department', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('specialty', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('email', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('phone', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('room_number', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('slot_length', sa.Integer(), nullable=False, server_default=sa.text('20')),
        sa.Column('schedule_templates', sa.JSON(), nullable=True),
        sa.Column('exceptions', sa.JSON(), nullable=True),
        sa.Column('leave', sa.JSON(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_doctors_name'), 'doctors', ['name'], unique=False)
    op.create_index(op.f('ix_doctors_department'), 'doctors', ['department'], unique=False)
    op.create_index(op.f('ix_doctors_user_id'), 'doctors', ['user_id'], unique=False)
    op.create_index(op.f('ix_doctors_is_active'), 'doctors', ['is_active'], unique=False)

    # 2. appointments table
    op.create_table(
        'appointments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('patient_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('patient_mrn', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('patient_phone', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('doctor_id', sa.Integer(), nullable=False),
        sa.Column('doctor_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('department', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('appointment_date', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('start_time', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('end_time', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('start_datetime', sa.DateTime(), nullable=False),
        sa.Column('end_datetime', sa.DateTime(), nullable=False),
        sa.Column('type', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=sa.text("'Consultation'")),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=sa.text("'Booked'")),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=sa.text("'Reception'")),
        sa.Column('notes', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('checked_in_at', sa.DateTime(), nullable=True),
        sa.Column('consultation_started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('reminder_sent', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_appointments_patient_id'), 'appointments', ['patient_id'], unique=False)
    op.create_index(op.f('ix_appointments_patient_name'), 'appointments', ['patient_name'], unique=False)
    op.create_index(op.f('ix_appointments_patient_mrn'), 'appointments', ['patient_mrn'], unique=False)
    op.create_index(op.f('ix_appointments_doctor_id'), 'appointments', ['doctor_id'], unique=False)
    op.create_index(op.f('ix_appointments_doctor_name'), 'appointments', ['doctor_name'], unique=False)
    op.create_index(op.f('ix_appointments_department'), 'appointments', ['department'], unique=False)
    op.create_index(op.f('ix_appointments_appointment_date'), 'appointments', ['appointment_date'], unique=False)
    op.create_index(op.f('ix_appointments_start_time'), 'appointments', ['start_time'], unique=False)
    op.create_index(op.f('ix_appointments_start_datetime'), 'appointments', ['start_datetime'], unique=False)
    op.create_index(op.f('ix_appointments_end_datetime'), 'appointments', ['end_datetime'], unique=False)
    op.create_index(op.f('ix_appointments_type'), 'appointments', ['type'], unique=False)
    op.create_index(op.f('ix_appointments_status'), 'appointments', ['status'], unique=False)
    op.create_index(op.f('ix_appointments_source'), 'appointments', ['source'], unique=False)
    op.create_index(op.f('ix_appointments_reminder_sent'), 'appointments', ['reminder_sent'], unique=False)

    # 3. queue_tokens table
    op.create_table(
        'queue_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_number', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('appointment_id', sa.Integer(), nullable=True),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('patient_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('patient_mrn', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('doctor_id', sa.Integer(), nullable=False),
        sa.Column('doctor_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('department', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('room_number', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('date', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=sa.text("'Waiting'")),
        sa.Column('sequence', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('is_walk_in', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('called_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_queue_tokens_token_number'), 'queue_tokens', ['token_number'], unique=False)
    op.create_index(op.f('ix_queue_tokens_appointment_id'), 'queue_tokens', ['appointment_id'], unique=False)
    op.create_index(op.f('ix_queue_tokens_patient_id'), 'queue_tokens', ['patient_id'], unique=False)
    op.create_index(op.f('ix_queue_tokens_doctor_id'), 'queue_tokens', ['doctor_id'], unique=False)
    op.create_index(op.f('ix_queue_tokens_department'), 'queue_tokens', ['department'], unique=False)
    op.create_index(op.f('ix_queue_tokens_date'), 'queue_tokens', ['date'], unique=False)
    op.create_index(op.f('ix_queue_tokens_status'), 'queue_tokens', ['status'], unique=False)


def downgrade() -> None:
    op.drop_table('queue_tokens')
    op.drop_table('appointments')
    op.drop_table('doctors')
