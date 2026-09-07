"""Structured logger and event dispatcher for EvoUndo Harness."""

from __future__ import annotations
import json
import logging
import sys
from typing import Any, Callable, Dict, List, Optional
from evoundo.observability.events import EventType, HarnessEvent


class StructuredEventLogger:
    """Pub/Sub event bus and JSON logger for EvoUndo control plane."""

    def __init__(self, logger_name: str = "evoundo", capture_history: bool = True):
        self.logger = logging.getLogger(logger_name)
        self.capture_history = capture_history
        self._history: List[HarnessEvent] = []
        self._subscribers: List[Callable[[HarnessEvent], None]] = []

    def subscribe(self, callback: Callable[[HarnessEvent], None]) -> None:
        """Register a subscriber for structured events."""
        self._subscribers.append(callback)

    def emit(
        self,
        event_type: EventType,
        mutation_id: Optional[str] = None,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> HarnessEvent:
        """Record and broadcast a structured lifecycle event."""
        event = HarnessEvent(
            event_type=event_type,
            mutation_id=mutation_id,
            message=message,
            data=data or {},
        )
        if self.capture_history:
            self._history.append(event)

        # Log via standard library logging at appropriate level
        log_msg = f"[{event.event_type.value}] ({event.mutation_id or 'global'}) {event.message}"
        if event_type in (EventType.UNDECLARED_EFFECT_DETECTED, EventType.RECOVERY_FAILED, EventType.MUTATION_REJECTED):
            self.logger.warning("%s | %s", log_msg, json.dumps(event.data, default=str))
        else:
            self.logger.info("%s | %s", log_msg, json.dumps(event.data, default=str))

        # Notify subscribers
        for subscriber in self._subscribers:
            try:
                subscriber(event)
            except Exception as e:
                self.logger.error(f"Subscriber error during event handling: {e}")

        return event

    def get_history(self, mutation_id: Optional[str] = None) -> List[HarnessEvent]:
        """Retrieve recorded event history, optionally filtered by mutation ID."""
        if mutation_id is None:
            return list(self._history)
        return [e for e in self._history if e.mutation_id == mutation_id]

    def clear_history(self) -> None:
        """Clear recorded event history."""
        self._history.clear()


# Default global logger instance
default_event_logger = StructuredEventLogger()
