from medicore.core.ui_schema import ColumnDef, EntitySchema, FieldDef, FilterDef

encounter_schema = EntitySchema(
    entity_name="encounter",
    title="Clinical Encounter",
    plural_title="Consultations & OPD Queue",
    endpoint_prefix="/consultations",
    fields=[
        FieldDef(name="patient_name", label="Patient Name", field_type="string", required=True),
        FieldDef(name="patient_mrn", label="MRN", field_type="string", required=True),
        FieldDef(name="doctor_name", label="Physician", field_type="string", required=True),
        FieldDef(
            name="department",
            label="Department",
            field_type="select",
            options=["General Medicine", "Cardiology", "Pediatrics", "Orthopedics", "Dermatology", "ENT"],
            required=True,
        ),
        FieldDef(
            name="status",
            label="Encounter Status",
            field_type="select",
            options=["In-Progress", "Completed", "Cancelled"],
            default="In-Progress",
            required=True,
        ),
        FieldDef(name="chief_complaint", label="Chief Complaint", field_type="text"),
    ],
    list_columns=[
        ColumnDef(field_name="patient_mrn", label="MRN", formatter="mrn", width="w-28"),
        ColumnDef(field_name="patient_name", label="Patient"),
        ColumnDef(field_name="doctor_name", label="Doctor"),
        ColumnDef(field_name="department", label="Department"),
        ColumnDef(
            field_name="status",
            label="Status",
            formatter="badge",
            badge_map={
                "In-Progress": "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300",
                "Completed": "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300",
                "Cancelled": "bg-slate-100 text-slate-700 border-slate-200 dark:bg-slate-800 dark:text-slate-400",
            },
        ),
        ColumnDef(field_name="started_at", label="Started At", formatter="datetime"),
    ],
    filters=[
        FilterDef(field_name="status", label="Status", filter_type="select", options=["All", "In-Progress", "Completed", "Cancelled"]),
        FilterDef(field_name="department", label="Department", filter_type="select", options=["All", "General Medicine", "Cardiology", "Pediatrics", "Orthopedics"]),
    ],
    searchable_fields=["patient_name", "patient_mrn", "doctor_name", "chief_complaint"],
    default_sort="-started_at",
)
