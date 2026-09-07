"""EvoUndo Harness with strict multi-tenant fencing during mutation recovery."""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional

from evoundo.core.harness import EvoUndoHarness as BaseHarness, RecoveryStrategy
from evoundo.governance import TenantAccessDeniedError
from evoundo.registry import MutationRegistry
from evoundo.witness import WitnessManager

logger = logging.getLogger("evoundo.harness")


class EvoUndoHarness(BaseHarness):
    """Subclass of EvoUndoHarness enforcing tenant boundaries during recovery."""

    def __init__(self, *args, tenant_id: str = "default", **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant_id = tenant_id

    def revert(
        self,
        mutation_id: str,
        reason: str = "",
        strategy: RecoveryStrategy = RecoveryStrategy.EVOUNDO_RECOVERY,
        actor_tenant_id: Optional[str] = None,
    ) -> Any:
        """Revert mutation, strictly verifying that requesting tenant matches mutation tenant."""
        record = self.mutation_registry.inspect_mutation(mutation_id)
        if not record:
            raise KeyError(f"Mutation ID '{mutation_id}' not found in registry.")

        rec_tenant = getattr(record, "tenant_id", "default")
        effective_actor_tenant = actor_tenant_id or getattr(self, "tenant_id", None)
        if effective_actor_tenant is not None and effective_actor_tenant != rec_tenant:
            raise TenantAccessDeniedError(
                f"Multi-Tenancy Violation: Actor tenant '{effective_actor_tenant}' denied permission "
                f"to revert mutation '{mutation_id}' belonging to tenant '{rec_tenant}'"
            )

        return super().revert(mutation_id, reason=reason, strategy=strategy)


def revert(mutation_id: str, reason: str = "", actor_tenant_id: Optional[str] = None) -> Any:
    """Convenience functional helper to revert a mutation with tenant fencing."""
    from evoundo.decorator import get_default_harness
    harness = get_default_harness()
    if isinstance(harness, EvoUndoHarness):
        return harness.revert(mutation_id, reason=reason, actor_tenant_id=actor_tenant_id)
    # Check tenant if record exists
    record = harness.mutation_registry.inspect_mutation(mutation_id)
    if record:
        rec_tenant = getattr(record, "tenant_id", "default")
        if actor_tenant_id is not None and actor_tenant_id != rec_tenant:
            raise TenantAccessDeniedError(
                f"Multi-Tenancy Violation: Actor tenant '{actor_tenant_id}' denied permission "
                f"to revert mutation '{mutation_id}' belonging to tenant '{rec_tenant}'"
            )
    return harness.revert(mutation_id, reason=reason)


__all__ = [
    "EvoUndoHarness",
    "revert",
    "RecoveryStrategy",
    "TenantAccessDeniedError",
]
