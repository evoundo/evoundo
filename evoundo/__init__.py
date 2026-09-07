"""EvoUndo: Autonomous Agent State Recoverability & Safety Framework."""

from __future__ import annotations
from typing import Any, Dict, List, Optional

from evoundo.__version__ import __version__
from evoundo.context import (
    EvoundoContext,
    current_evoundo_context,
    execution_context,
    get_current_context,
)
from evoundo.levels import RecoveryLevel
from evoundo.probe import HTTPProbe, SecurityError, SSRFSecurityError
from evoundo.decorator import (
    protect,
    get_default_harness,
    set_default_harness,
)
from evoundo.protection.declarative import protect_tool, ToolDefinition
from evoundo.wrapper import wrap, wrap_tool, evoundo
from evoundo.effects.address import ResourceAddress
from evoundo.recovery.group import RecoveryTransactionGroup
from evoundo.actions.compensation import CompensationLedger
from evoundo.actions.classifier import ActionClass, IrreversibleActionBlockedError
from evoundo.memory.adapter import MemoryAdapter, DefaultMemoryAdapter
from evoundo.memory.contracts import MemoryDependencyGraph
from evoundo.memory.working_memory import AgentWorkingMemory
from evoundo.governance import (
    RecoveryAuthorizer,
    DefaultRecoveryAuthorizer,
    TenantContext,
    DefaultTenantContext,
    TenantAccessDeniedError,
    ApprovalProvider,
    DefaultApprovalProvider,
    PayloadProtector,
    DefaultPayloadProtector,
)
from evoundo.reconciliation.async_job import AsynchronousJobReconciler

# Core Formalized Primitives (Gaps 12, 14, 16, 17)
from evoundo.core.schedule import EventScheduleManager, ScheduledTrigger, TriggerStatus
from evoundo.core.artifact import (
    ImmutableArtifactLedger,
    ArtifactRecord,
    ArtifactEvaluation,
    ImmutableArtifactError,
    ImmutableArtifactModificationError,
)
from evoundo.core.causal_tree import MultiAgentCausalTree, CausalNode, CausalContingency
from evoundo.actions.broadcast import CompensatableBroadcastChannel, BroadcastMessage

from evoundo.drivers.sqlite import SQLiteDriver, sqlite
from evoundo.drivers.json_config import JSONConfigDriver, json_config
from evoundo.drivers.k8s import KubernetesDriver, k8s_deploy
from evoundo.drivers.file import AuditFileDriver, audit_file
from evoundo.drivers.orm import SQLAlchemyDriver, UnsupportedORMOperationError
from evoundo.drivers.redis import RedisDriver, redis_client
from evoundo.recovery.operations import RecoveryVerificationError
from evoundo.witness import WitnessCaptureError
from evoundo.persistence import (
    MutationStorageBackend,
    JsonFileStorage,
    SqliteStorage,
    create_storage_backend,
)
from evoundo.core.harness import EvoUndoHarness, RecoveryStrategy
from evoundo.registry import MutationRegistry, MutationRecord


def revert(mutation_id: Optional[str] = None, reason: str = "") -> Any:
    """Convenience functional helper to revert a mutation using the default harness."""
    harness = get_default_harness()
    if mutation_id is None:
        active = [m for m in harness.mutation_registry.show_history() if m.get("status") == "ACTIVE"]
        if not active:
            raise ValueError("No active mutations to revert.")
        mutation_id = active[-1]["mutation_id"]
    return harness.revert(mutation_id, reason=reason)


def show_history() -> List[Dict[str, Any]]:
    """Return historical audit log of recorded mutations."""
    harness = get_default_harness()
    return harness.mutation_registry.show_history()


__all__ = [
    "wrap",
    "wrap_tool",
    "evoundo",
    "protect_tool",
    "ToolDefinition",
    "protect",
    "revert",
    "show_history",
    "ResourceAddress",
    "RecoveryTransactionGroup",
    "CompensationLedger",
    "ActionClass",
    "IrreversibleActionBlockedError",
    "MemoryAdapter",
    "DefaultMemoryAdapter",
    "MemoryDependencyGraph",
    "AgentWorkingMemory",
    "RecoveryAuthorizer",
    "DefaultRecoveryAuthorizer",
    "TenantContext",
    "DefaultTenantContext",
    "TenantAccessDeniedError",
    "ApprovalProvider",
    "DefaultApprovalProvider",
    "PayloadProtector",
    "DefaultPayloadProtector",
    "AsynchronousJobReconciler",
    "EventScheduleManager",
    "ScheduledTrigger",
    "TriggerStatus",
    "ImmutableArtifactLedger",
    "ArtifactRecord",
    "ArtifactEvaluation",
    "ImmutableArtifactError",
    "ImmutableArtifactModificationError",
    "MultiAgentCausalTree",
    "CausalNode",
    "CausalContingency",
    "CompensatableBroadcastChannel",
    "BroadcastMessage",
    "MutationStorageBackend",
    "JsonFileStorage",
    "SqliteStorage",
    "create_storage_backend",
    "RecoveryLevel",
    "HTTPProbe",
    "sqlite",
    "SQLiteDriver",
    "json_config",
    "JSONConfigDriver",
    "k8s_deploy",
    "KubernetesDriver",
    "audit_file",
    "AuditFileDriver",
    "EvoundoContext",
    "current_evoundo_context",
    "execution_context",
    "get_current_context",
    "get_default_harness",
    "set_default_harness",
    "SQLAlchemyDriver",
    "UnsupportedORMOperationError",
    "RedisDriver",
    "redis_client",
    "RecoveryVerificationError",
    "WitnessCaptureError",
    "EvoUndoHarness",
    "RecoveryStrategy",
    "MutationRegistry",
    "MutationRecord",
    "__version__",
]
