from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session, col, select
from medicore.core.database import get_session
from medicore.core.models import AuditLog, CustomFieldDefinition, Permission, Role, User
from medicore.core.registry import registry
from medicore.core.security import get_current_user, require_permission
from medicore.core.settings import settings_registry
from medicore.ui.templating import get_ui_context, templates

core_router = APIRouter()


@core_router.get("/", response_class=RedirectResponse)
def root():
    return RedirectResponse(url="/patients", status_code=status.HTTP_302_FOUND)


@core_router.get("/api/palette", response_class=HTMLResponse)
def get_palette_results(
    request: Request,
    q: Optional[str] = Query(""),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    query = (q or "").strip()
    actions = registry.get_palette_actions(user, session)
    if query:
        actions = [a for a in actions if query.lower() in a.title.lower() or query.lower() in a.category.lower()]

    search_results = registry.search_all(query, user, session) if query else []

    ctx = {
        "request": request,
        "actions": actions,
        "search_results": search_results,
    }
    return templates.TemplateResponse("partials/palette.html", ctx)


@core_router.post("/api/preferences")
async def save_user_preferences(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        body = await request.json()
        prefs = dict(user.preferences or {})
        prefs.update(body)
        user.preferences = prefs
        session.add(user)
        session.commit()
        return JSONResponse({"status": "ok", "preferences": user.preferences})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@core_router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: User = Depends(require_permission("core.settings.view")),
    session: Session = Depends(get_session),
):
    hospital_profile = settings_registry.get("hospital", "profile", {})
    departments = settings_registry.get("departments", "list", [])
    mrn_settings = settings_registry.get("numbering", "mrn", {})
    enabled_modules = settings_registry.get("modules", "enabled", {})

    custom_fields = session.exec(
        select(CustomFieldDefinition).order_by(CustomFieldDefinition.entity_name, CustomFieldDefinition.display_order)
    ).all()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        hospital=hospital_profile,
        departments=departments,
        mrn=mrn_settings,
        enabled_modules=enabled_modules,
        custom_fields=custom_fields,
    )
    return templates.TemplateResponse("core/settings.html", ctx)


@core_router.post("/settings/hospital")
async def update_hospital_settings(
    request: Request,
    user: User = Depends(require_permission("core.settings.edit")),
):
    form = await request.form()
    current = dict(settings_registry.get("hospital", "profile", {}) or {})
    current["name"] = str(form.get("name", current.get("name"))).strip()
    current["tagline"] = str(form.get("tagline", current.get("tagline"))).strip()
    current["phone"] = str(form.get("phone", current.get("phone"))).strip()
    current["emergency_hotline"] = str(form.get("emergency_hotline", current.get("emergency_hotline"))).strip()
    current["email"] = str(form.get("email", current.get("email"))).strip()
    current["address"] = str(form.get("address", current.get("address"))).strip()
    current["license_number"] = str(form.get("license_number", current.get("license_number"))).strip()

    settings_registry.set("hospital", "profile", current)
    return RedirectResponse(url="/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@core_router.post("/settings/modules/toggle")
async def toggle_module(
    module_name: str = Form(...),
    enabled: bool = Form(...),
    user: User = Depends(require_permission("core.settings.edit")),
):
    registry.set_module_enabled(module_name, enabled)
    return RedirectResponse(url="/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@core_router.post("/settings/custom-fields")
async def create_custom_field(
    entity_name: str = Form(...),
    field_name: str = Form(...),
    label: str = Form(...),
    field_type: str = Form("string"),
    options_raw: Optional[str] = Form(None),
    is_required: bool = Form(False),
    user: User = Depends(require_permission("core.settings.edit")),
    session: Session = Depends(get_session),
):
    # Parse options if select
    options = [o.strip() for o in options_raw.split(",")] if options_raw else []

    clean_name = field_name.strip().lower().replace(" ", "_")
    c_def = CustomFieldDefinition(
        entity_name=entity_name.strip().lower(),
        field_name=clean_name,
        label=label.strip(),
        field_type=field_type,
        options=options,
        is_required=is_required,
    )
    session.add(c_def)
    session.commit()
    return RedirectResponse(url="/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@core_router.get("/audit", response_class=HTMLResponse)
def audit_trail_page(
    request: Request,
    user: User = Depends(require_permission("core.audit.view")),
    session: Session = Depends(get_session),
    module: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
):
    stmt = select(AuditLog).order_by(col(AuditLog.timestamp).desc()).limit(100)
    if module and module != "All":
        stmt = stmt.where(AuditLog.module == module)
    if action and action != "All":
        stmt = stmt.where(AuditLog.action == action)

    logs = session.exec(stmt).all()
    roles = session.exec(select(Role)).all()
    users = session.exec(select(User)).all()
    permissions = session.exec(select(Permission)).all()

    ctx = get_ui_context(
        request,
        user=user,
        session=session,
        logs=logs,
        roles=roles,
        users=users,
        permissions=permissions,
    )
    return templates.TemplateResponse("core/audit.html", ctx)
