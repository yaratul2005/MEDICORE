import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from fastapi import APIRouter, FastAPI
from sqlmodel import Session
from medicore.core.events import event_bus
from medicore.core.models import User
from medicore.core.security import user_has_permission
from medicore.core.settings import settings_registry

logger = logging.getLogger("medicore.registry")


@dataclass
class NavEntry:
    label: str
    url: str
    icon: str
    permission: Optional[str] = None
    badge: Optional[str] = None


@dataclass
class PermissionDef:
    code: str  # e.g. "patients.patient.read"
    entity: str
    action: str
    description: Optional[str] = None


@dataclass
class DashboardWidget:
    id: str
    title: str
    template: str
    width: str = "col-span-1"  # Tailwind grid column span
    permission: Optional[str] = None


@dataclass
class SearchResult:
    title: str
    subtitle: str
    url: str
    category: str
    icon: str
    badge: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PaletteAction:
    id: str
    title: str
    category: str
    url: str
    icon: str
    shortcut: Optional[str] = None
    permission: Optional[str] = None


@dataclass
class ModuleManifest:
    name: str
    label: str
    icon: str
    order: int = 100
    nav_entries: List[NavEntry] = field(default_factory=list)
    permissions: List[PermissionDef] = field(default_factory=list)
    router: Optional[APIRouter] = None
    models: List[Any] = field(default_factory=list)
    dashboard_widgets: List[DashboardWidget] = field(default_factory=list)
    search_provider: Optional[Callable[[str, User, Session], List[SearchResult]]] = None
    command_palette_actions: List[PaletteAction] = field(default_factory=list)
    event_handlers: Dict[str, List[Callable]] = field(default_factory=dict)


class ModuleRegistry:
    def __init__(self):
        self._manifests: Dict[str, ModuleManifest] = {}

    def register(self, manifest: ModuleManifest):
        self._manifests[manifest.name] = manifest
        # Bind event handlers to event_bus
        for event_pattern, handlers in manifest.event_handlers.items():
            for handler in handlers:
                event_bus.subscribe(event_pattern, handler)
        logger.info(f"Registered module: {manifest.name} ({manifest.label})")

    def is_enabled(self, module_name: str) -> bool:
        enabled_modules = settings_registry.get("modules", "enabled", {}) or {}
        return enabled_modules.get(module_name, True)

    def set_module_enabled(self, module_name: str, enabled: bool):
        enabled_modules = dict(settings_registry.get("modules", "enabled", {}) or {})
        enabled_modules[module_name] = enabled
        settings_registry.set("modules", "enabled", enabled_modules)

    def get_active_manifests(self) -> List[ModuleManifest]:
        active = [
            m for m in self._manifests.values()
            if self.is_enabled(m.name)
        ]
        return sorted(active, key=lambda m: m.order)

    def get_manifest(self, name: str) -> Optional[ModuleManifest]:
        return self._manifests.get(name)

    def auto_discover(self, package_name: str = "medicore.modules"):
        """Auto-discover all modules in the package having a manifest"""
        try:
            package = importlib.import_module(package_name)
        except ImportError as e:
            logger.warning(f"Could not import {package_name}: {e}")
            return

        for _, sub_name, is_pkg in pkgutil.iter_modules(package.__path__):
            if is_pkg:
                module_path = f"{package_name}.{sub_name}"
                try:
                    # Check for manifest.py
                    manifest_mod = importlib.import_module(f"{module_path}.manifest")
                    if hasattr(manifest_mod, "manifest"):
                        manifest_obj = getattr(manifest_mod, "manifest")
                        if isinstance(manifest_obj, ModuleManifest):
                            self.register(manifest_obj)
                except Exception as e:
                    logger.error(f"Failed to auto-discover module in {module_path}: {e}", exc_info=True)

    def get_nav_entries(self, user: User, session: Session) -> List[NavEntry]:
        """Aggregate navigation entries from active modules, filtered by user permissions"""
        entries: List[NavEntry] = []
        for manifest in self.get_active_manifests():
            for nav in manifest.nav_entries:
                if not nav.permission or user_has_permission(user, nav.permission, session):
                    entries.append(nav)
        return entries

    def get_palette_actions(self, user: User, session: Session) -> List[PaletteAction]:
        """Aggregate command palette actions from active modules, filtered by user permissions"""
        actions: List[PaletteAction] = []
        for manifest in self.get_active_manifests():
            for act in manifest.command_palette_actions:
                if not act.permission or user_has_permission(user, act.permission, session):
                    actions.append(act)
        return actions

    def search_all(self, query: str, user: User, session: Session) -> List[SearchResult]:
        """Run federated global search across all active module search providers"""
        results: List[SearchResult] = []
        clean_q = query.strip()
        if not clean_q:
            return results

        for manifest in self.get_active_manifests():
            if manifest.search_provider:
                try:
                    mod_results = manifest.search_provider(clean_q, user, session)
                    if mod_results:
                        results.extend(mod_results)
                except Exception as e:
                    logger.error(f"Error in search provider for {manifest.name}: {e}")
        return results

    def attach_routers(self, app: FastAPI):
        """Mount all active module routers to the FastAPI app"""
        for manifest in self.get_active_manifests():
            if manifest.router:
                app.include_router(manifest.router)
                logger.info(f"Mounted router for module '{manifest.name}'")


registry = ModuleRegistry()
