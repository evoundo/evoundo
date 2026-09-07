"""Structured event definitions and schema for EvoUndo Harness observability."""

from __future__ import annotations
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class EventType(str, Enum):
    """Lifecycle event types emitted by the EvoUndo Harness control plane."""
    # Lifecycle start
    MUTATION_REQUESTED = "MUTATION_REQUESTED"
    MUTATION_PROPOSED = "MUTATION_PROPOSED"
    WITNESS_CAPTURED = "WITNESS_CAPTURED"
    EFFECT_CONTRACT_VALIDATED = "EFFECT_CONTRACT_VALIDATED"
    RECOVERABILITY_CHECKED = "RECOVERABILITY_CHECKED"
    
    # Admission & Execution
    MUTATION_ADMITTED = "MUTATION_ADMITTED"
    MUTATION_REJECTED = "MUTATION_REJECTED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    MUTATION_EXECUTED = "MUTATION_EXECUTED"
    EFFECT_OBSERVED = "EFFECT_OBSERVED"
    EXTERNAL_EFFECT_OBSERVED = "EXTERNAL_EFFECT_OBSERVED"
    EXECUTION_COMMITTED = "EXECUTION_COMMITTED"
    
    # Failures & Diagnostics
    UNDECLARED_EFFECT_DETECTED = "UNDECLARED_EFFECT_DETECTED"
    CAPABILITY_EVALUATED = "CAPABILITY_EVALUATED"
    FAILURE_DETECTED = "FAILURE_DETECTED"
    STATE_RECONCILED = "STATE_RECONCILED"
    MUTATION_RECONCILED = "MUTATION_RECONCILED"
    DUPLICATE_MUTATION_SUPPRESSED = "DUPLICATE_MUTATION_SUPPRESSED"
    
    # Recovery
    RECOVERY_PLANNED = "RECOVERY_PLANNED"
    RECOVERY_VERIFIED = "RECOVERY_VERIFIED"
    RECOVERY_EXECUTED = "RECOVERY_EXECUTED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    MUTATION_REVERTED = "MUTATION_REVERTED"
    
    # Generic
    ERROR = "ERROR"
    INFO = "INFO"


@dataclass
class HarnessEvent:
    """Structured event record emitted during the self-evolution lifecycle."""
    event_type: EventType
    mutation_id: Optional[str]
    timestamp: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to serializable dictionary."""
        return {
            "event_type": self.event_type.value if isinstance(self.event_type, EventType) else str(self.event_type),
            "mutation_id": self.mutation_id,
            "timestamp": self.timestamp,
            "message": self.message,
            "data": self.data,
        }

    def to_json(self) -> str:
        """Format as JSON line."""
        return json.dumps(self.to_dict(), default=str)
