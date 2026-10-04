from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import Column, JSON
from sqlmodel import Field, Relationship, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserRoleLink(SQLModel, table=True):
    __tablename__ = "user_role_link"
    user_id: int = Field(foreign_key="users.id", primary_key=True)
    role_id: int = Field(foreign_key="roles.id", primary_key=True)


class RolePermissionLink(SQLModel, table=True):
    __tablename__ = "role_permission_link"
    role_id: int = Field(foreign_key="roles.id", primary_key=True)
    permission_id: int = Field(foreign_key="permissions.id", primary_key=True)


class Permission(SQLModel, table=True):
    __tablename__ = "permissions"
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)  # e.g. "patients.patient.read"
    module: str = Field(index=True)             # e.g. "patients"
    entity: str = Field(index=True)             # e.g. "patient"
    action: str = Field(index=True)             # e.g. "read", "create", "update", "delete", "export"
    description: Optional[str] = None

    roles: List["Role"] = Relationship(back_populates="permissions", link_model=RolePermissionLink)


class Role(SQLModel, table=True):
    __tablename__ = "roles"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)  # e.g. "Admin", "Doctor", "Nurse", "Receptionist"
    description: Optional[str] = None
    is_system: bool = Field(default=False)

    users: List["User"] = Relationship(back_populates="roles", link_model=UserRoleLink)
    permissions: List[Permission] = Relationship(back_populates="roles", link_model=RolePermissionLink)


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    email: str = Field(index=True, unique=True)
    full_name: str
    hashed_password: str
    is_active: bool = Field(default=True)
    is_superuser: bool = Field(default=False)
    # Theme & density preferences: {"theme": "light", "density": "compact"}
    preferences: Dict[str, Any] = Field(default_factory=lambda: {"theme": "light", "density": "compact"}, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)

    roles: List[Role] = Relationship(back_populates="users", link_model=UserRoleLink)


class Setting(SQLModel, table=True):
    __tablename__ = "settings"
    id: Optional[int] = Field(default=None, primary_key=True)
    namespace: str = Field(index=True)  # "hospital", "numbering", "departments", "modules", "system"
    key: str = Field(index=True)        # unique together with namespace
    value: Any = Field(default=None, sa_column=Column(JSON))
    label: str
    description: Optional[str] = None
    is_secret: bool = Field(default=False)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_logs"
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=utc_now, index=True)
    user_id: Optional[int] = Field(default=None, index=True)
    username: Optional[str] = Field(default="system", index=True)
    ip_address: Optional[str] = None
    module: str = Field(index=True)
    entity: str = Field(index=True)
    entity_id: Optional[str] = Field(default=None, index=True)
    action: str = Field(index=True)  # "CREATE", "UPDATE", "DELETE", "VIEW"
    changes: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class CustomFieldDefinition(SQLModel, table=True):
    __tablename__ = "custom_field_definitions"
    id: Optional[int] = Field(default=None, primary_key=True)
    entity_name: str = Field(index=True)  # e.g. "patient"
    field_name: str = Field(index=True)   # snake_case identifier, e.g. "insurance_provider"
    label: str                            # human label, e.g. "Insurance Provider"
    field_type: str = Field(default="string")  # "string", "number", "date", "select", "boolean"
    options: List[str] = Field(default_factory=list, sa_column=Column(JSON))  # choices for select
    default_value: Optional[str] = None
    is_required: bool = Field(default=False)
    display_order: int = Field(default=10)
    created_at: datetime = Field(default_factory=utc_now)
