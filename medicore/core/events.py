import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from sqlmodel import Session
from medicore.core.database import engine
from medicore.core.models import AuditLog

logger = logging.getLogger("medicore.events")


@dataclass
class Event:
    name: str
    payload: Dict[str, Any]
    user_id: Optional[int] = None
    username: Optional[str] = "system"
    ip_address: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EventBus:
    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}
        self._wildcard_handlers: List[Callable] = []

    def subscribe(self, event_pattern: str, handler: Callable):
        """Subscribe a callable to an event pattern (e.g. 'patient.registered', 'patient.*', '*')"""
        if event_pattern == "*":
            if handler not in self._wildcard_handlers:
                self._wildcard_handlers.append(handler)
        else:
            if event_pattern not in self._handlers:
                self._handlers[event_pattern] = []
            if handler not in self._handlers[event_pattern]:
                self._handlers[event_pattern].append(handler)

    def unsubscribe(self, event_pattern: str, handler: Callable):
        if event_pattern == "*" and handler in self._wildcard_handlers:
            self._wildcard_handlers.remove(handler)
        elif event_pattern in self._handlers and handler in self._handlers[event_pattern]:
            self._handlers[event_pattern].remove(handler)

    def _matches(self, pattern: str, event_name: str) -> bool:
        if pattern == event_name:
            return True
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            return event_name.startswith(f"{prefix}.")
        return False

    def emit(
        self,
        name: str,
        payload: Dict[str, Any],
        user_id: Optional[int] = None,
        username: Optional[str] = "system",
        ip_address: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Event:
        event = Event(
            name=name,
            payload=payload,
            user_id=user_id,
            username=username,
            ip_address=ip_address,
            metadata=metadata or {},
        )

        matched_handlers: List[Callable] = list(self._wildcard_handlers)
        for pattern, handlers in self._handlers.items():
            if self._matches(pattern, name):
                matched_handlers.extend(handlers)

        for handler in matched_handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(handler(event))
                    except RuntimeError:
                        asyncio.run(handler(event))
                else:
                    handler(event)
            except Exception as e:
                logger.error(f"Error handling event '{name}' in {handler}: {e}", exc_info=True)

        return event


event_bus = EventBus()


def system_audit_subscriber(event: Event):
    """Automatic audit subscriber for core and module events"""
    # Event name format: module.entity.action e.g. "patient.created" or "patients.patient.created"
    parts = event.name.split(".")
    if len(parts) >= 2:
        module = parts[0]
        entity = parts[1] if len(parts) >= 3 else parts[0]
        raw_action = parts[-1].upper()
    else:
        module = "system"
        entity = event.name
        raw_action = "EVENT"

    action_map = {
        "CREATED": "CREATE",
        "UPDATED": "UPDATE",
        "DELETED": "DELETE",
        "VIEWED": "VIEW",
    }
    action = action_map.get(raw_action, raw_action)
    if module == "patient":
        module = "patients"
    if entity == "patients":
        entity = "patient"

    entity_id = str(event.payload.get("id") or event.payload.get("entity_id") or "")

    try:
        with Session(engine) as session:
            log_entry = AuditLog(
                user_id=event.user_id,
                username=event.username or "system",
                ip_address=event.ip_address,
                module=module,
                entity=entity,
                entity_id=entity_id or None,
                action=action,
                changes=event.payload,
            )
            session.add(log_entry)
            session.commit()
    except Exception as e:
        logger.warning(f"Failed to record audit log for event {event.name}: {e}")


# Register system audit subscriber by default
event_bus.subscribe("*", system_audit_subscriber)
