from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from medicore.core.config import settings
from medicore.core.models import User
from medicore.core.security import user_has_permission
from medicore.core.settings import settings_registry

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class MediCoreTemplates(Jinja2Templates):
    def TemplateResponse(self, *args, **kwargs):
        # Support both signatures:
        # 1. TemplateResponse(name, context) where context contains "request"
        # 2. TemplateResponse(request, name, context)
        # 3. TemplateResponse(request=request, name=name, context=context)
        if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], dict):
            name = args[0]
            context = args[1]
            request = context.get("request") or kwargs.get("request")
            if request is None:
                request = Request({"type": "http", "method": "GET", "path": "/"})
                context["request"] = request
            return super().TemplateResponse(request=request, name=name, context=context, **kwargs)
        elif len(args) >= 2 and not isinstance(args[0], str) and isinstance(args[1], str):
            request = args[0]
            name = args[1]
            context = args[2] if len(args) > 2 else kwargs.get("context", {})
            if request is None:
                request = Request({"type": "http", "method": "GET", "path": "/"})
                context["request"] = request
            return super().TemplateResponse(request=request, name=name, context=context, **kwargs)
        return super().TemplateResponse(*args, **kwargs)


templates = MediCoreTemplates(directory=str(TEMPLATES_DIR))


def calculate_age(dob_str: Optional[str]) -> str:
    if not dob_str:
        return "Unknown"
    try:
        from datetime import date, datetime
        dob = datetime.strptime(dob_str[:10], "%Y-%m-%d").date()
        today = date.today()
        years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        return f"{years} yrs"
    except Exception:
        return dob_str


# Add custom Jinja2 template filters and globals
templates.env.filters["age"] = calculate_age


def get_ui_context(
    request: Request,
    user: Optional[User] = None,
    session: Optional[Session] = None,
    **extra: Any
) -> Dict[str, Any]:
    from medicore.core.registry import registry

    hospital_profile = settings_registry.get("hospital", "profile", {}) or {}
    nav_entries = registry.get_nav_entries(user, session) if user and session else []
    palette_actions = registry.get_palette_actions(user, session) if user and session else []

    user_prefs = user.preferences if user else {"theme": "light", "density": "compact"}

    context = {
        "request": request,
        "user": user,
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "hospital_profile": hospital_profile,
        "nav_entries": nav_entries,
        "palette_actions": palette_actions,
        "active_modules": registry.get_active_manifests(),
        "user_theme": user_prefs.get("theme", "light"),
        "user_density": user_prefs.get("density", "compact"),
        "has_permission": lambda perm: user_has_permission(user, perm, session) if user and session else False,
        **extra,
    }
    return context
