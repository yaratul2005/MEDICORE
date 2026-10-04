from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple
from sqlmodel import Session, select
from medicore.core.models import CustomFieldDefinition, User
from medicore.core.security import user_has_permission


@dataclass
class FieldDef:
    name: str
    label: str
    field_type: Literal["string", "text", "number", "date", "select", "boolean", "badge"] = "string"
    required: bool = False
    options: List[str] = field(default_factory=list)
    placeholder: str = ""
    help_text: str = ""
    default: Any = None
    permission_view: Optional[str] = None
    permission_edit: Optional[str] = None
    is_custom: bool = False
    grid_col_span: int = 1  # 1 or 2 in a 2-column grid
    badge_colors: Dict[str, str] = field(default_factory=dict)


@dataclass
class ColumnDef:
    field_name: str
    label: str
    sortable: bool = True
    width: str = ""
    formatter: Optional[str] = None  # "badge", "date", "datetime", "mrn"
    badge_map: Dict[str, str] = field(default_factory=dict)
    is_custom: bool = False


@dataclass
class FilterDef:
    field_name: str
    label: str
    filter_type: Literal["search", "select", "boolean", "date"] = "select"
    options: List[str] = field(default_factory=list)
    is_custom: bool = False


@dataclass
class EntitySchema:
    entity_name: str
    title: str
    plural_title: str
    endpoint_prefix: str
    fields: List[FieldDef] = field(default_factory=list)
    list_columns: List[ColumnDef] = field(default_factory=list)
    filters: List[FilterDef] = field(default_factory=list)
    searchable_fields: List[str] = field(default_factory=list)
    duplicate_check_fields: List[str] = field(default_factory=list)
    default_sort: str = "-created_at"
    custom_template_dir: Optional[str] = None

    def get_effective_schema(self, session: Optional[Session] = None, user: Optional[User] = None) -> "EntitySchema":
        """
        Merge custom fields defined in DB for this entity and filter fields based on user permissions.
        """
        effective = deepcopy(self)

        # 1. Filter out base fields user doesn't have permission to view
        if user and session:
            effective.fields = [
                f for f in effective.fields
                if not f.permission_view or user_has_permission(user, f.permission_view, session)
            ]

        # 2. Dynamically attach DB-configured custom fields
        if session:
            custom_defs = session.exec(
                select(CustomFieldDefinition)
                .where(CustomFieldDefinition.entity_name == self.entity_name)
                .order_by(CustomFieldDefinition.display_order)
            ).all()

            for c_def in custom_defs:
                # Add to fields
                effective.fields.append(
                    FieldDef(
                        name=f"custom_fields.{c_def.field_name}",
                        label=c_def.label,
                        field_type=c_def.field_type,  # type: ignore
                        required=c_def.is_required,
                        options=c_def.options or [],
                        default=c_def.default_value,
                        is_custom=True,
                    )
                )
                # Add to list columns if not too many
                if len(effective.list_columns) < 8:
                    effective.list_columns.append(
                        ColumnDef(
                            field_name=f"custom_fields.{c_def.field_name}",
                            label=c_def.label,
                            sortable=False,
                            is_custom=True,
                        )
                    )
                # Add to filters if select/boolean
                if c_def.field_type in ("select", "boolean"):
                    effective.filters.append(
                        FilterDef(
                            field_name=f"custom_fields.{c_def.field_name}",
                            label=c_def.label,
                            filter_type="select" if c_def.field_type == "select" else "boolean",
                            options=c_def.options or [],
                            is_custom=True,
                        )
                    )

        return effective

    def extract_value(self, obj: Any, field_name: str) -> Any:
        """Extract value from SQLModel object or dict, supporting 'custom_fields.name' syntax"""
        if not obj:
            return None

        if field_name.startswith("custom_fields."):
            key = field_name.split(".", 1)[1]
            if isinstance(obj, dict):
                custom = obj.get("custom_fields") or {}
            else:
                custom = getattr(obj, "custom_fields", {}) or {}
            return custom.get(key)

        if isinstance(obj, dict):
            return obj.get(field_name)
        return getattr(obj, field_name, None)
