"""Generic Event Schedule & Future Trigger Manager (Gap 12 Formalization).

Manages time-shifted future actions (e.g. reminders, cron triggers, scheduled webhooks)
registered under parent event mutations. Reverting the parent event mutation
automatically traverses registered future trigger IDs and dispatches cancellation.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import logging
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("evoundo.core.schedule")


class TriggerStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    EXECUTED = "EXECUTED"


@dataclass
class ScheduledTrigger:
    trigger_id: str
    event_id: str
    trigger_type: str
    due_time: str
    status: TriggerStatus = TriggerStatus.ACTIVE
    metadata: Dict[str, Any] = field(default_factory=dict)
    cancel_fn: Optional[Callable[[str], None]] = None


class EventScheduleManager:
    """Manages events and time-shifted future triggers with cascade cancellation."""

    def __init__(self) -> None:
        self._events: Dict[str, Dict[str, Any]] = {}
        self._triggers: Dict[str, ScheduledTrigger] = {}
        self._event_to_triggers: Dict[str, List[str]] = {}

    def register_event(self, event_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Register a parent event."""
        self._events[event_id] = metadata or {}
        if event_id not in self._event_to_triggers:
            self._event_to_triggers[event_id] = []

    def add_trigger(
        self,
        event_id: str,
        trigger_id: str,
        due_time: str,
        trigger_type: str = "reminder",
        metadata: Optional[Dict[str, Any]] = None,
        cancel_fn: Optional[Callable[[str], None]] = None,
    ) -> ScheduledTrigger:
        """Add a scheduled trigger dependent on a parent event."""
        if event_id not in self._events:
            self.register_event(event_id)

        trigger = ScheduledTrigger(
            trigger_id=trigger_id,
            event_id=event_id,
            trigger_type=trigger_type,
            due_time=due_time,
            metadata=metadata or {},
            cancel_fn=cancel_fn,
        )
        self._triggers[trigger_id] = trigger
        self._event_to_triggers[event_id].append(trigger_id)
        logger.info("Added trigger %s (%s) for event %s due at %s", trigger_id, trigger_type, event_id, due_time)
        return trigger

    def get_triggers(self, event_id: str, active_only: bool = True) -> List[ScheduledTrigger]:
        """Retrieve triggers associated with an event."""
        trigger_ids = self._event_to_triggers.get(event_id, [])
        triggers = [self._triggers[tid] for tid in trigger_ids if tid in self._triggers]
        if active_only:
            triggers = [t for t in triggers if t.status == TriggerStatus.ACTIVE]
        return triggers

    def cancel_trigger(self, trigger_id: str, reason: str = "") -> bool:
        """Cancel an individual trigger."""
        trigger = self._triggers.get(trigger_id)
        if not trigger:
            return False
        if trigger.status != TriggerStatus.ACTIVE:
            return False

        trigger.status = TriggerStatus.CANCELLED
        if trigger.cancel_fn:
            try:
                trigger.cancel_fn(trigger_id)
            except Exception as e:
                logger.warning("Error invoking cancel_fn for trigger %s: %s", trigger_id, e)
        logger.info("Cancelled trigger %s: %s", trigger_id, reason)
        return True

    def cancel_event_triggers(self, event_id: str, reason: str = "Parent event reverted") -> List[str]:
        """Cascade cancel all active triggers associated with a parent event."""
        cancelled = []
        for trigger in self.get_triggers(event_id, active_only=True):
            if self.cancel_trigger(trigger.trigger_id, reason=reason):
                cancelled.append(trigger.trigger_id)
        return cancelled

    def mark_executed(self, trigger_id: str) -> bool:
        """Mark a trigger as executed."""
        trigger = self._triggers.get(trigger_id)
        if trigger and trigger.status == TriggerStatus.ACTIVE:
            trigger.status = TriggerStatus.EXECUTED
            return True
        return False
