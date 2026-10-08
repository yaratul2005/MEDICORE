"""Add Consultation OPD/EMR module tables

Revision ID: 0004_consultations
Revises: 0003_double_booking_protection
Create Date: 2026-10-08 15:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = '0004_consultations'
down_revision: Union[str, None] = '0003_double_booking_protection'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. encounters
    op.create_table(
        'encounters',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('patient_name', sa.String(), nullable=False),
        sa.Column('patient_mrn', sa.String(), nullable=False),
        sa.Column('doctor_id', sa.Integer(), nullable=False),
        sa.Column('doctor_name', sa.String(), nullable=False),
        sa.Column('appointment_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='In-Progress'),
        sa.Column('department', sa.String(), nullable=False, server_default='General Medicine'),
        sa.Column('chief_complaint', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_encounters_patient_id', 'encounters', ['patient_id'], unique=False)
    op.create_index('ix_encounters_doctor_id', 'encounters', ['doctor_id'], unique=False)
    op.create_index('ix_encounters_appointment_id', 'encounters', ['appointment_id'], unique=False)
    op.create_index('ix_encounters_status', 'encounters', ['status'], unique=False)

    # 2. vitals
    op.create_table(
        'vitals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('encounter_id', sa.Integer(), nullable=True),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('recorded_by', sa.String(), nullable=True),
        sa.Column('height_cm', sa.Float(), nullable=True),
        sa.Column('weight_kg', sa.Float(), nullable=True),
        sa.Column('bmi', sa.Float(), nullable=True),
        sa.Column('bsa', sa.Float(), nullable=True),
        sa.Column('systolic', sa.Integer(), nullable=True),
        sa.Column('diastolic', sa.Integer(), nullable=True),
        sa.Column('pulse', sa.Integer(), nullable=True),
        sa.Column('temp_c', sa.Float(), nullable=True),
        sa.Column('spo2', sa.Float(), nullable=True),
        sa.Column('resp_rate', sa.Integer(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_vitals_patient_id', 'vitals', ['patient_id'], unique=False)
    op.create_index('ix_vitals_encounter_id', 'vitals', ['encounter_id'], unique=False)

    # 3. clinical_notes
    op.create_table(
        'clinical_notes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('encounter_id', sa.Integer(), nullable=False),
        sa.Column('specialty', sa.String(), nullable=False, server_default='General Medicine'),
        sa.Column('subjective', sa.Text(), nullable=True),
        sa.Column('objective', sa.Text(), nullable=True),
        sa.Column('assessment', sa.Text(), nullable=True),
        sa.Column('plan', sa.Text(), nullable=True),
        sa.Column('is_signed', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('signed_by', sa.String(), nullable=True),
        sa.Column('signed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('addenda', sa.JSON(), nullable=True),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_clinical_notes_encounter_id', 'clinical_notes', ['encounter_id'], unique=False)
    op.create_index('ix_clinical_notes_is_signed', 'clinical_notes', ['is_signed'], unique=False)

    # 4. diagnoses
    op.create_table(
        'diagnoses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('encounter_id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('icd10_code', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=False),
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_diagnoses_encounter_id', 'diagnoses', ['encounter_id'], unique=False)
    op.create_index('ix_diagnoses_patient_id', 'diagnoses', ['patient_id'], unique=False)
    op.create_index('ix_diagnoses_icd10_code', 'diagnoses', ['icd10_code'], unique=False)

    # 5. prescriptions
    op.create_table(
        'prescriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('encounter_id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('doctor_id', sa.Integer(), nullable=False),
        sa.Column('doctor_name', sa.String(), nullable=True),
        sa.Column('prescribed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='Active'),
        sa.Column('safety_override_reason', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_prescriptions_encounter_id', 'prescriptions', ['encounter_id'], unique=False)
    op.create_index('ix_prescriptions_patient_id', 'prescriptions', ['patient_id'], unique=False)
    op.create_index('ix_prescriptions_doctor_id', 'prescriptions', ['doctor_id'], unique=False)

    # 6. prescription_items
    op.create_table(
        'prescription_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('prescription_id', sa.Integer(), nullable=False),
        sa.Column('drug_name', sa.String(), nullable=False),
        sa.Column('generic_name', sa.String(), nullable=True),
        sa.Column('dosage', sa.String(), nullable=False),
        sa.Column('frequency', sa.String(), nullable=False),
        sa.Column('route', sa.String(), nullable=False, server_default='Oral'),
        sa.Column('duration_days', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('quantity', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('instructions', sa.Text(), nullable=True),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_prescription_items_prescription_id', 'prescription_items', ['prescription_id'], unique=False)

    # 7. orders
    op.create_table(
        'orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('encounter_id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('doctor_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(), nullable=False, server_default='Lab'),
        sa.Column('test_name', sa.String(), nullable=False),
        sa.Column('priority', sa.String(), nullable=False, server_default='Routine'),
        sa.Column('clinical_notes', sa.Text(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='Placed'),
        sa.Column('placed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_orders_encounter_id', 'orders', ['encounter_id'], unique=False)
    op.create_index('ix_orders_patient_id', 'orders', ['patient_id'], unique=False)
    op.create_index('ix_orders_doctor_id', 'orders', ['doctor_id'], unique=False)

    # 8. follow_ups
    op.create_table(
        'follow_ups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('encounter_id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('doctor_id', sa.Integer(), nullable=False),
        sa.Column('recommended_date', sa.Date(), nullable=False),
        sa.Column('appointment_id', sa.Integer(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_follow_ups_encounter_id', 'follow_ups', ['encounter_id'], unique=False)
    op.create_index('ix_follow_ups_patient_id', 'follow_ups', ['patient_id'], unique=False)

    # 9. referrals
    op.create_table(
        'referrals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('encounter_id', sa.Integer(), nullable=False),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('referring_doctor_id', sa.Integer(), nullable=False),
        sa.Column('referring_doctor_name', sa.String(), nullable=False),
        sa.Column('referred_to_specialty', sa.String(), nullable=False),
        sa.Column('referred_to_doctor', sa.String(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_referrals_encounter_id', 'referrals', ['encounter_id'], unique=False)
    op.create_index('ix_referrals_patient_id', 'referrals', ['patient_id'], unique=False)


def downgrade() -> None:
    op.drop_table('referrals')
    op.drop_table('follow_ups')
    op.drop_table('orders')
    op.drop_table('prescription_items')
    op.drop_table('prescriptions')
    op.drop_table('diagnoses')
    op.drop_table('clinical_notes')
    op.drop_table('vitals')
    op.drop_table('encounters')
