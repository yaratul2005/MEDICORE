"""Initial core and patients schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-10-04 20:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = '0001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. permissions
    op.create_table(
        'permissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('module', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('entity', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('action', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_permissions_code'), 'permissions', ['code'], unique=True)
    op.create_index(op.f('ix_permissions_module'), 'permissions', ['module'], unique=False)
    op.create_index(op.f('ix_permissions_entity'), 'permissions', ['entity'], unique=False)
    op.create_index(op.f('ix_permissions_action'), 'permissions', ['action'], unique=False)

    # 2. roles
    op.create_table(
        'roles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_roles_name'), 'roles', ['name'], unique=True)

    # 3. role_permission_link
    op.create_table(
        'role_permission_link',
        sa.Column('role_id', sa.Integer(), nullable=False),
        sa.Column('permission_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['permission_id'], ['permissions.id']),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id']),
        sa.PrimaryKeyConstraint('role_id', 'permission_id')
    )

    # 4. users
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('email', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('full_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('hashed_password', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('is_superuser', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('preferences', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_username'), 'users', ['username'], unique=True)
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)

    # 5. user_role_link
    op.create_table(
        'user_role_link',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('role_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id', 'role_id')
    )

    # 6. settings
    op.create_table(
        'settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('namespace', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('value', sa.JSON(), nullable=True),
        sa.Column('label', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('is_secret', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_settings_namespace'), 'settings', ['namespace'], unique=False)
    op.create_index(op.f('ix_settings_key'), 'settings', ['key'], unique=False)

    # 7. audit_logs
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('username', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('ip_address', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('module', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('entity', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('entity_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('action', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('changes', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_logs_timestamp'), 'audit_logs', ['timestamp'], unique=False)
    op.create_index(op.f('ix_audit_logs_module'), 'audit_logs', ['module'], unique=False)
    op.create_index(op.f('ix_audit_logs_entity'), 'audit_logs', ['entity'], unique=False)
    op.create_index(op.f('ix_audit_logs_action'), 'audit_logs', ['action'], unique=False)

    # 8. custom_field_definitions
    op.create_table(
        'custom_field_definitions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('field_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('label', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('field_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='string'),
        sa.Column('options', sa.JSON(), nullable=True),
        sa.Column('default_value', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('is_required', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default=sa.text('10')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_custom_fields_entity_name'), 'custom_field_definitions', ['entity_name'], unique=False)
    op.create_index(op.f('ix_custom_fields_field_name'), 'custom_field_definitions', ['field_name'], unique=False)

    # 9. patients
    op.create_table(
        'patients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('mrn', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('first_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('last_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('date_of_birth', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('gender', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('blood_group', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('phone', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('email', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('address', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('emergency_contact_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('emergency_contact_phone', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('allergies', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='Active'),
        sa.Column('custom_fields', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_patients_mrn'), 'patients', ['mrn'], unique=True)
    op.create_index(op.f('ix_patients_first_name'), 'patients', ['first_name'], unique=False)
    op.create_index(op.f('ix_patients_last_name'), 'patients', ['last_name'], unique=False)
    op.create_index(op.f('ix_patients_date_of_birth'), 'patients', ['date_of_birth'], unique=False)
    op.create_index(op.f('ix_patients_phone'), 'patients', ['phone'], unique=False)
    op.create_index(op.f('ix_patients_status'), 'patients', ['status'], unique=False)


def downgrade() -> None:
    op.drop_table('patients')
    op.drop_table('custom_field_definitions')
    op.drop_table('audit_logs')
    op.drop_table('settings')
    op.drop_table('user_role_link')
    op.drop_table('users')
    op.drop_table('role_permission_link')
    op.drop_table('roles')
    op.drop_table('permissions')
