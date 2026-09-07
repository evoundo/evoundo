"""Out-of-band framework execution context for EvoUndo 2.0 using contextvars."""

from __future__ import annotations
import contextvars
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class EvoundoContext:
    """Carries execution metadata across framework and tool execution boundaries without polluting function signatures."""
    framework: str = "generic"
    tenant_id: str = "default"
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    agent_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    logical_mutation_id: Optional[str] = None
    parent_logical_id: Optional[str] = None
    parent_mutation_id: Optional[str] = None
    retry_attempt: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Internal runtime slots dynamically populated during tool invocation
    active_harness: Optional[Any] = None
    active_mutation_id: Optional[str] = None
    active_target: Optional[str] = None
    active_witness: Dict[str, Any] = field(default_factory=dict)
    active_inverse_fn: Optional[Callable[..., Any]] = None
    active_recovery_level: Optional[str] = None
    active_effects: List[Any] = field(default_factory=list)
    active_inverses: List[Callable[..., Any]] = field(default_factory=list)
    active_recovery_ops: List[Any] = field(default_factory=list)


# ContextVar holding the current active execution context
current_evoundo_context: contextvars.ContextVar[Optional[EvoundoContext]] = contextvars.ContextVar(
    "current_evoundo_context", default=None
)


def get_current_context() -> EvoundoContext:
    """Return active EvoundoContext or construct a default context if none is active."""
    ctx = current_evoundo_context.get()
    if ctx is None:
        ctx = EvoundoContext()
        current_evoundo_context.set(ctx)
    return ctx


def clone_context(parent: Optional[EvoundoContext] = None, **overrides: Any) -> EvoundoContext:
    """Create an isolated child context inheriting execution metadata but with fresh mutable slots."""
    if parent is None:
        parent = current_evoundo_context.get()

    inherited = {
        "framework": parent.framework if parent else "generic",
        "tenant_id": parent.tenant_id if parent else "default",
        "run_id": parent.run_id if parent else None,
        "thread_id": parent.thread_id if parent else None,
        "agent_id": parent.agent_id if parent else None,
        "tool_call_id": parent.tool_call_id if parent else None,
        "logical_mutation_id": parent.logical_mutation_id if parent else None,
        "parent_logical_id": parent.parent_logical_id if parent else None,
        "parent_mutation_id": parent.parent_mutation_id if parent else None,
        "retry_attempt": parent.retry_attempt if parent else 0,
        "metadata": dict(parent.metadata) if parent and parent.metadata else {},
        "active_harness": parent.active_harness if parent else None,
    }
    inherited.update(overrides)
    return EvoundoContext(**inherited)


class execution_context:
    """Context manager for setting framework context out-of-band."""
    def __init__(
        self,
        framework: str = "generic",
        tenant_id: str = "default",
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        logical_mutation_id: Optional[str] = None,
        retry_attempt: int = 0,
        **metadata: Any,
    ):
        self.ctx = EvoundoContext(
            framework=framework,
            tenant_id=tenant_id,
            run_id=run_id,
            thread_id=thread_id,
            agent_id=agent_id,
            tool_call_id=tool_call_id,
            logical_mutation_id=logical_mutation_id,
            retry_attempt=retry_attempt,
            metadata=metadata,
        )
        self.token = None

    def __enter__(self) -> EvoundoContext:
        self.token = current_evoundo_context.set(self.ctx)
        return self.ctx

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.token is not None:
            current_evoundo_context.reset(self.token)
