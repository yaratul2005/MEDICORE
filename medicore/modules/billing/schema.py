from medicore.core.ui_schema import ColumnDef, EntitySchema, FieldDef, FilterDef

invoice_schema = EntitySchema(
    entity_name="invoice",
    title="Invoice",
    plural_title="Patient Invoices",
    endpoint_prefix="/billing/invoices",
    default_sort="created_at",
    searchable_fields=["invoice_no", "patient_name", "patient_mrn"],
    fields=[
        FieldDef(name="invoice_no", label="Invoice #", field_type="string", required=True),
        FieldDef(name="patient_name", label="Patient Name", field_type="string", required=True),
        FieldDef(name="patient_mrn", label="Patient MRN", field_type="string", required=True),
        FieldDef(
            name="status",
            label="Status",
            field_type="select",
            options=["draft", "finalized", "partially_paid", "paid", "credit_note_issued", "cancelled"],
        ),
        FieldDef(name="total_amount", label="Total Amount", field_type="number"),
        FieldDef(name="paid_amount", label="Paid Amount", field_type="number"),
        FieldDef(name="balance_due", label="Balance Due", field_type="number"),
    ],
    list_columns=[
        ColumnDef(field_name="invoice_no", label="Invoice #"),
        ColumnDef(field_name="created_at", label="Date", formatter="date"),
        ColumnDef(field_name="patient_name", label="Patient"),
        ColumnDef(field_name="patient_mrn", label="MRN"),
        ColumnDef(field_name="patient_category", label="Category", formatter="badge"),
        ColumnDef(field_name="total_amount", label="Total", formatter="currency"),
        ColumnDef(field_name="paid_amount", label="Paid", formatter="currency"),
        ColumnDef(field_name="balance_due", label="Due", formatter="currency"),
        ColumnDef(field_name="status", label="Status", formatter="badge"),
    ],
    filters=[
        FilterDef(
            field_name="status",
            label="Status",
            filter_type="select",
            options=["draft", "finalized", "partially_paid", "paid", "credit_note_issued"],
        ),
        FilterDef(
            field_name="patient_category",
            label="Category",
            filter_type="select",
            options=["general", "staff", "insured", "vip"],
        ),
    ],
)

service_catalog_schema = EntitySchema(
    entity_name="service_catalog",
    title="Service Catalog",
    plural_title="Services & Tariff",
    endpoint_prefix="/billing/services",
    default_sort="code",
    searchable_fields=["code", "name", "category", "department"],
    fields=[
        FieldDef(name="code", label="Service Code", field_type="string", required=True),
        FieldDef(name="name", label="Service Name", field_type="string", required=True),
        FieldDef(
            name="category",
            label="Category",
            field_type="select",
            options=["consultation", "procedure", "bed_charge", "package", "laboratory", "radiology", "nursing", "pharmacy"],
        ),
        FieldDef(name="department", label="Department", field_type="string"),
        FieldDef(name="base_price", label="Base Price", field_type="number", default=0.0),
        FieldDef(name="tax_rate", label="Tax Rate (%)", field_type="number", default=0.0),
    ],
    list_columns=[
        ColumnDef(field_name="code", label="Code"),
        ColumnDef(field_name="name", label="Service Name"),
        ColumnDef(field_name="category", label="Category", formatter="badge"),
        ColumnDef(field_name="department", label="Department"),
        ColumnDef(field_name="base_price", label="Base Price", formatter="currency"),
        ColumnDef(field_name="is_active", label="Status", formatter="boolean"),
    ],
    filters=[
        FilterDef(
            field_name="category",
            label="Category",
            filter_type="select",
            options=["consultation", "procedure", "bed_charge", "package", "laboratory", "radiology", "nursing", "pharmacy"],
        ),
    ],
)

price_list_schema = EntitySchema(
    entity_name="price_list",
    title="Price List",
    plural_title="Price Lists",
    endpoint_prefix="/billing/pricelists",
    default_sort="code",
    searchable_fields=["code", "name", "patient_category"],
    fields=[
        FieldDef(name="code", label="Code", field_type="string", required=True),
        FieldDef(name="name", label="Name", field_type="string", required=True),
        FieldDef(name="patient_category", label="Category", field_type="select", options=["general", "staff", "insured", "vip"]),
        FieldDef(name="discount_percentage", label="Adjustment (%)", field_type="number", default=0.0),
    ],
    list_columns=[
        ColumnDef(field_name="code", label="Code"),
        ColumnDef(field_name="name", label="Name"),
        ColumnDef(field_name="patient_category", label="Patient Category", formatter="badge"),
        ColumnDef(field_name="discount_percentage", label="Adjustment %"),
        ColumnDef(field_name="is_active", label="Status", formatter="boolean"),
    ],
    filters=[],
)

deposit_schema = EntitySchema(
    entity_name="deposit",
    title="Deposit",
    plural_title="Patient Deposits",
    endpoint_prefix="/billing/deposits",
    default_sort="created_at",
    searchable_fields=["deposit_no", "patient_name", "patient_mrn"],
    fields=[
        FieldDef(name="deposit_no", label="Deposit #", field_type="string"),
        FieldDef(name="patient_name", label="Patient", field_type="string"),
        FieldDef(name="amount", label="Total Amount", field_type="number"),
        FieldDef(name="balance_amount", label="Available Balance", field_type="number"),
    ],
    list_columns=[
        ColumnDef(field_name="deposit_no", label="Deposit #"),
        ColumnDef(field_name="created_at", label="Date", formatter="date"),
        ColumnDef(field_name="patient_name", label="Patient"),
        ColumnDef(field_name="amount", label="Original", formatter="currency"),
        ColumnDef(field_name="used_amount", label="Adjusted", formatter="currency"),
        ColumnDef(field_name="balance_amount", label="Balance", formatter="currency"),
        ColumnDef(field_name="status", label="Status", formatter="badge"),
    ],
    filters=[],
)

claim_schema = EntitySchema(
    entity_name="claim",
    title="Insurance Claim",
    plural_title="Insurance Claims",
    endpoint_prefix="/billing/insurance/claims",
    default_sort="submitted_at",
    searchable_fields=["claim_no", "provider_name", "patient_mrn"],
    fields=[
        FieldDef(name="claim_no", label="Claim #", field_type="string"),
        FieldDef(name="provider_name", label="Insurer", field_type="string"),
        FieldDef(name="claim_amount", label="Claim Amount", field_type="number"),
        FieldDef(name="status", label="Status", field_type="select", options=["draft", "submitted", "in_review", "approved", "rejected", "settled"]),
    ],
    list_columns=[
        ColumnDef(field_name="claim_no", label="Claim #"),
        ColumnDef(field_name="submitted_at", label="Submitted", formatter="date"),
        ColumnDef(field_name="provider_name", label="Payer / Insurer"),
        ColumnDef(field_name="claim_amount", label="Claimed", formatter="currency"),
        ColumnDef(field_name="approved_amount", label="Approved", formatter="currency"),
        ColumnDef(field_name="patient_co_pay", label="Patient Co-Pay", formatter="currency"),
        ColumnDef(field_name="status", label="Status", formatter="badge"),
    ],
    filters=[],
)
