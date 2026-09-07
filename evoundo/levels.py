"""First-class recovery levels corresponding to the R0-R4 capability taxonomy."""

from __future__ import annotations
from enum import Enum


class RecoveryLevel(str, Enum):
    """Declarative capability levels for tool protection."""
    OBSERVE = "observe"                  # R0: Audit logging & witness capture only
    RECONCILE = "reconcile"              # R1: Probe-reconciled duplicate suppression (no inverse needed)
    COMPENSATE = "compensate"            # R2: Forward compensating semantic inverse
    INVERT = "invert"                    # R3: Exact state rollback
    DEPENDENCY_AWARE = "dependency_aware"# R4: Rollback with address-collision conflict detection

    # Canonical taxonomy aliases
    R0 = "observe"
    R1 = "reconcile"
    R2 = "compensate"
    R3 = "invert"
    R4 = "dependency_aware"

    @classmethod
    def _missing_(cls, value: object) -> Optional[RecoveryLevel]:
        if isinstance(value, str):
            clean = value.strip().lower().replace("-", "_")
            mapping = {
                "r0": cls.OBSERVE,
                "observe": cls.OBSERVE,
                "r1": cls.RECONCILE,
                "reconcile": cls.RECONCILE,
                "r2": cls.COMPENSATE,
                "compensate": cls.COMPENSATE,
                "r3": cls.INVERT,
                "invert": cls.INVERT,
                "r4": cls.DEPENDENCY_AWARE,
                "dependency_aware": cls.DEPENDENCY_AWARE,
            }
            if clean in mapping:
                return mapping[clean]
        return None
