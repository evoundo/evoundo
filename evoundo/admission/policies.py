"""Admission policy data structures and configuration."""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AdmissionStatus(str, Enum):
    """Admission decision status."""
    ADMIT = "ADMIT"
    REJECT = "REJECT"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


@dataclass
class CapabilityResult:
    """Evaluation result for task/benchmark performance before and after mutation."""
    improved: bool
    score_before: float
    score_after: float
    delta: float
    metrics: Dict[str, Any] = field(default_factory=dict)
    test_results: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "improved": self.improved,
            "score_before": round(self.score_before, 4),
            "score_after": round(self.score_after, 4),
            "delta": round(self.delta, 4),
            "metrics": self.metrics,
            "test_results": self.test_results,
        }


@dataclass
class AdmissionDecision:
    """Machine-readable admission verdict with comprehensive diagnostic reasons."""
    status: AdmissionStatus
    reasons: List[str] = field(default_factory=list)
    decision_code: str = "PENDING"
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def admissible(self) -> bool:
        return self.status == AdmissionStatus.ADMIT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value if isinstance(self.status, AdmissionStatus) else str(self.status),
            "admissible": self.admissible,
            "decision_code": self.decision_code,
            "reasons": self.reasons,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


@dataclass
class AdmissionPolicyConfig:
    """Configurable gates for admitting proposed self-mutations."""
    require_capability_improvement: bool = True
    require_strict_containment: bool = True
    require_counterfactual_verification: bool = True
    min_capability_delta: float = 0.0
    forbidden_keys: List[str] = field(default_factory=lambda: ["__system_root__", "__private_key__"])
