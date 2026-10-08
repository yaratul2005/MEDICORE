from medicore.core.ui_schema import ColumnDef, EntitySchema, FieldDef, FilterDef

test_catalog_schema = EntitySchema(
    entity_name="test_catalog",
    title="Lab Test",
    plural_title="Lab Catalog",
    endpoint_prefix="/laboratory/catalog",
    default_sort="name",
    searchable_fields=["code", "name", "department", "specimen_type"],
    fields=[
        FieldDef(name="code", label="Test Code", field_type="string", required=True, placeholder="e.g. CBC"),
        FieldDef(name="name", label="Test Name", field_type="string", required=True, placeholder="e.g. Complete Blood Count"),
        FieldDef(
            name="department",
            label="Department",
            field_type="select",
            required=True,
            options=["Hematology", "Biochemistry", "Microbiology", "Pathology", "Urinalysis", "Serology", "Immunology"],
        ),
        FieldDef(
            name="specimen_type",
            label="Specimen Type",
            field_type="select",
            required=True,
            options=["Whole Blood (EDTA)", "Serum", "Plasma", "Urine (Clean Catch)", "CSF", "Stool", "Swab"],
        ),
        FieldDef(name="tat_hours", label="Turnaround Time (Hrs)", field_type="number", required=True, default=4),
        FieldDef(name="price", label="Price ($)", field_type="number", required=True, default=0.0),
        FieldDef(name="is_panel", label="Is Multi-Parameter Panel", field_type="boolean", default=False),
        FieldDef(name="is_active", label="Active", field_type="boolean", default=True),
        FieldDef(name="description", label="Description", field_type="textarea", required=False),
    ],
    list_columns=[
        ColumnDef(field_name="code", label="Code"),
        ColumnDef(field_name="name", label="Test Name"),
        ColumnDef(field_name="department", label="Department", formatter="badge"),
        ColumnDef(field_name="specimen_type", label="Specimen"),
        ColumnDef(field_name="tat_hours", label="TAT (hrs)"),
        ColumnDef(field_name="price", label="Price", formatter="currency"),
        ColumnDef(field_name="is_active", label="Status", formatter="boolean"),
    ],
    filters=[
        FilterDef(
            field_name="department",
            label="Department",
            filter_type="select",
            options=["Hematology", "Biochemistry", "Microbiology", "Pathology", "Urinalysis", "Serology"],
        ),
    ],
)

lab_order_schema = EntitySchema(
    entity_name="lab_order",
    title="Lab Order",
    plural_title="Lab Worklist",
    endpoint_prefix="/laboratory",
    default_sort="ordered_at",
    searchable_fields=["order_no", "patient_name", "patient_mrn", "doctor_name"],
    fields=[
        FieldDef(name="order_no", label="Order #", field_type="string", required=True),
        FieldDef(name="patient_name", label="Patient Name", field_type="string", required=True),
        FieldDef(name="patient_mrn", label="MRN", field_type="string", required=True),
        FieldDef(name="priority", label="Priority", field_type="select", options=["Routine", "Urgent", "STAT"]),
        FieldDef(name="department", label="Department", field_type="string", required=True),
        FieldDef(
            name="status",
            label="Status",
            field_type="select",
            options=["ordered", "specimen_collected", "specimen_received", "in_progress", "result_entered", "approved", "cancelled"],
        ),
    ],
    list_columns=[
        ColumnDef(field_name="order_no", label="Order No"),
        ColumnDef(field_name="patient_name", label="Patient"),
        ColumnDef(field_name="priority", label="Priority", formatter="badge"),
        ColumnDef(field_name="department", label="Department"),
        ColumnDef(field_name="status", label="Status", formatter="badge"),
        ColumnDef(field_name="tat_deadline", label="TAT Deadline"),
    ],
    filters=[
        FilterDef(
            field_name="priority",
            label="Priority",
            filter_type="select",
            options=["STAT", "Urgent", "Routine"],
        ),
        FilterDef(
            field_name="status",
            label="Status",
            filter_type="select",
            options=["ordered", "specimen_collected", "specimen_received", "in_progress", "result_entered", "approved"],
        ),
    ],
)
