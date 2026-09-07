"""
EvoUndo Common Framework Integration Contract.

Defines the abstract adapter interface, tool classification ontology,
and framework-independent execution context mapping all agent runtimes into
the canonical EvoUndo lifecycle.
"""

from __future__ import annotations
import enum
import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.core.harness import EvoUndoHarness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.identity import MutationIdentity
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger
from evoundo.reconciliation.reconciler import (
    FailureStage,
    JournalEntry,
    MutationReconciler,
    ReconciliationDecision,
    ReconciliationStatus,
)
from evoundo.recovery.operations import BaseRecoveryOp, CustomRecoveryOp, DriverRecoveryOp, RecoveryProgram
from evoundo.witness.stores import Witness


class ToolClassification(str, enum.Enum):
    """Semantic classification of agent tool operations."""
    READ_ONLY = "READ_ONLY"            # Zero side-effects; bypasses mutation tracking
    MUTATING = "MUTATING"              # Stateful external modification; requires witness & recovery
    IRREVERSIBLE = "IRREVERSIBLE"      # Action cannot be undone (e.g. real email delivered)
    COMPENSATABLE = "COMPENSATABLE"    # Action reversible via explicit compensating inverse function
    RECONCILABLE = "RECONCILABLE"      # Supports post-crash probe & duplicate suppression


@dataclass
class FrameworkContext:
    """Standardized framework-independent execution context."""
    framework_name: str
    tenant_id: Optional[str] = "default"
    application_id: Optional[str] = "agent_app"
    run_id: Optional[str] = None
    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    logical_mutation_id: Optional[str] = None
    retry_attempt: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def derive_logical_id(self) -> str:
        """Derive deterministic logical mutation identity across framework retries."""
        if self.logical_mutation_id:
            return self.logical_mutation_id

        components = [
            self.framework_name,
            self.tenant_id or "",
            self.application_id or "",
            self.thread_id or self.session_id or self.run_id or "default_run",
            self.agent_id or "default_agent",
            self.tool_name or "tool",
            self.tool_call_id or "",
        ]
        raw = ":".join(components)
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"mut_{self.framework_name}_{h}"


def _build_recovery_op(
    surface: str,
    target_name: str,
    logical_id: str,
    witness_val: Any,
    inverse_fn: Optional[Callable[[Any, Any], Any]],
    result: Any,
) -> BaseRecoveryOp:
    if surface == "redis":
        redis_url = os.environ.get("EVOUNDO_REDIS_URL", "redis://localhost:6379/0")
        redis_w = {
            "exists": witness_val is not None,
            "old_value": witness_val,
        }
        return DriverRecoveryOp(
            driver_type="redis",
            target=f"redis://{target_name}",
            operation="SET",
            parameters={"redis_url": redis_url, "key": target_name},
            witness_data=redis_w,
        )
    elif inverse_fn:
        return CustomRecoveryOp(
            name=f"Revert:{target_name}:{logical_id}",
            inverse_fn=lambda state, wit, inv=inverse_fn, tgt=target_name, res=result: inv(
                wit.data.get(tgt) if hasattr(wit, "data") else (wit.get(tgt) if isinstance(wit, dict) else wit),
                res,
            ),
        )
    elif surface in ("mysql", "orm"):
        mysql_url = os.environ.get("EVOUNDO_MYSQL_URL", "mysql+pymysql://root:evoundo@127.0.0.1:3307/evoundo_test")
        return DriverRecoveryOp(
            driver_type="orm",
            target=f"mysql://{target_name}",
            operation="UPDATE",
            parameters={"db_url": mysql_url, "table_name": target_name, "pk": 1},
            witness_data=witness_val,
        )
    else:
        return CustomRecoveryOp(
            name=f"Protected:{target_name}:{logical_id}",
            inverse_fn=None,
        )


class AgentFrameworkAdapter:
    """Base contract for all AI agent framework adapters."""

    def __init__(
        self,
        harness: Optional[EvoUndoHarness] = None,
        event_logger: Optional[StructuredEventLogger] = None,
    ):
        self.harness = harness or EvoUndoHarness()
        self.event_logger = event_logger or (self.harness.event_logger if self.harness else default_event_logger)
        self.reconciler = getattr(self.harness, "reconciler", None) or MutationReconciler(event_logger=self.event_logger)

    @property
    def framework_name(self) -> str:
        raise NotImplementedError

    def classify_tool(self, tool_name: str, tool_callable: Optional[Callable[..., Any]] = None) -> ToolClassification:
        """Classify tool to avoid unnecessary overhead for read-only actions."""
        return ToolClassification.MUTATING

    def extract_context(self, *args: Any, **kwargs: Any) -> FrameworkContext:
        """Extract standardized FrameworkContext from runtime framework event objects."""
        raise NotImplementedError

    def on_handoff(
        self,
        from_agent_id: str,
        to_agent_id: str,
        context: FrameworkContext,
    ) -> FrameworkContext:
        """Propagate mutation context across multi-agent handoff boundaries."""
        new_ctx = FrameworkContext(
            framework_name=self.framework_name,
            tenant_id=context.tenant_id,
            application_id=context.application_id,
            run_id=context.run_id,
            thread_id=context.thread_id,
            session_id=context.session_id,
            agent_id=to_agent_id,
            tool_name=context.tool_name,
            tool_call_id=context.tool_call_id,
            logical_mutation_id=context.logical_mutation_id,
            retry_attempt=context.retry_attempt,
            metadata={**context.metadata, "delegated_from": from_agent_id},
        )
        return new_ctx

    def create_tool_wrapper(
        self,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Wrap an agent tool with canonical EvoUndo protection and recovery."""
        import functools

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = target or getattr(fn, "__name__", "tool")

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                ctx_meta = kwargs.pop(f"__{self.framework_name}_context", None) or kwargs.pop("__context", {}) or {}
                ctx = FrameworkContext(
                    framework_name=self.framework_name,
                    session_id=ctx_meta.get("session_id", kwargs.pop("session_id", None)),
                    run_id=ctx_meta.get("run_id", kwargs.pop("run_id", None)),
                    agent_id=ctx_meta.get("agent_id", f"{self.framework_name}_agent"),
                    tool_name=tool_name,
                    tool_call_id=ctx_meta.get("tool_call_id", kwargs.pop("tool_call_id", None)),
                    logical_mutation_id=ctx_meta.get("logical_mutation_id", kwargs.pop("__logical_mutation_id", None)),
                    retry_attempt=int(ctx_meta.get("retry_attempt", kwargs.pop("retry_attempt", 0))),
                    metadata=ctx_meta.get("metadata", ctx_meta),
                )
                return self.execute_protected_tool(
                    tool_fn=fn,
                    context=ctx,
                    tool_args=args,
                    tool_kwargs=kwargs,
                    surface=surface,
                    target=tool_name,
                    declared_effects=declared_effects or [Effect(category=EffectCategory.RESOURCES, target=tool_name, op_type=EffectOpType.UPDATE)],
                    capture_fn=capture_fn,
                    inverse_fn=inverse_fn,
                    post_condition_probe=post_condition_probe,
                    post_condition_validator=post_condition_validator,
                )

            setattr(wrapper, "_is_evoundo_protected", True)
            setattr(wrapper, "_evoundo_adapter", self)
            return wrapper

        return decorator

    def execute_protected_tool(
        self,
        tool_fn: Callable[..., Any],
        context: FrameworkContext,
        tool_args: Tuple[Any, ...],
        tool_kwargs: Dict[str, Any],
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        side_effect_detector: Optional[Callable[[], List[Effect]]] = None,
    ) -> Any:
        """Execute tool through the canonical EvoUndo reliability & recovery lifecycle."""
        # 1. Classification check
        classification = self.classify_tool(context.tool_name or getattr(tool_fn, "__name__", "tool"), tool_fn)
        if classification == ToolClassification.READ_ONLY:
            # Read-only tool -> bypass mutation tracking and execute directly
            return tool_fn(*tool_args, **tool_kwargs)

        target_name = target or context.tool_name or getattr(tool_fn, "__name__", "tool")
        logical_id = context.derive_logical_id()

        # 2. Build MutationIdentity
        identity = MutationIdentity(
            logical_mutation_id=logical_id,
            framework=self.framework_name,
            framework_run_id=context.thread_id or context.run_id or context.session_id,
            tool_name=target_name,
            tool_call_id=context.tool_call_id,
            retry_attempt=context.retry_attempt,
            metadata=context.metadata,
        )

        # 3. Reconciliation check (Suppress duplicate on crash retry)
        rec_decision = self.reconciler.evaluate_request(
            identity=identity,
            current_state_probe=post_condition_probe,
            expected_post_condition=post_condition_validator,
        )

        if rec_decision.status == ReconciliationStatus.CONFLICT_DETECTED:
            raise RuntimeError(f"CONFLICT_DETECTED: {rec_decision.reason}")

        if not rec_decision.should_execute_fn:
            # Duplicate suppressed! Register recovery program if not already present
            if self.harness:
                entry = self.reconciler.get_entry(logical_id)
                wit_val = entry.witness_data.get(target_name) if entry else None
                witness = Witness(mutation_id=logical_id, data={target_name: wit_val})
                op = _build_recovery_op(surface, target_name, logical_id, wit_val, inverse_fn, rec_decision.cached_result)
                prog = RecoveryProgram(operations=[op])
                if hasattr(self.harness, "record_external_protected_mutation"):
                    self.harness.record_external_protected_mutation(
                        mutation_id=logical_id,
                        description=f"Protected [{self.framework_name}] tool '{target_name}'",
                        witness=witness,
                        recovery_program=prog,
                        declared_effects=declared_effects or [Effect(category=EffectCategory.RESOURCES, target=target_name, op_type=EffectOpType.UPDATE)],
                        identity=identity,
                    )
            return rec_decision.cached_result

        # 4. Capture Pre-State Witness
        witness_val = None
        if capture_fn:
            try:
                witness_val = capture_fn(*tool_args, **tool_kwargs)
            except Exception as e:
                self.event_logger.emit(
                    event_type=EventType.ERROR,
                    mutation_id=logical_id,
                    message=f"Failed to capture witness for {target_name}: {e}",
                )
                self.reconciler.record_failed(logical_id, str(e), failure_stage=FailureStage.CAPTURE)
                raise e

        self.reconciler.record_witness(logical_id, {target_name: witness_val})

        # 5. Admission & Capability Check
        if not inverse_fn and classification in (ToolClassification.MUTATING, ToolClassification.COMPENSATABLE):
            # Check if strict policy requires inverse recovery
            pass

        # 6. Execute External Tool Mutation
        try:
            result = tool_fn(*tool_args, **tool_kwargs)
        except Exception as err:
            self.reconciler.record_failed(logical_id, str(err), failure_stage=FailureStage.EXECUTION)
            raise err

        # 7. Record Mutation Completion
        self.reconciler.record_mutation_executed(
            logical_mutation_id=logical_id,
            result=result,
            effects=[e.to_dict() for e in (declared_effects or [])],
        )

        # 8. Side-Effect Leak Detection (Fail-Closed)
        if side_effect_detector:
            detected = side_effect_detector()
            declared_targets = {e.target for e in (declared_effects or [])}
            undeclared = [e for e in detected if e.target not in declared_targets]
            if undeclared:
                if inverse_fn:
                    try:
                        inverse_fn(witness_val, result)
                    except Exception:
                        pass
                raise RuntimeError(
                    f"Fail-closed: Undeclared side effects detected during [{self.framework_name}] execution: "
                    f"{[e.target for e in undeclared]}"
                )

        # 9. Register Recovery Program with Harness
        if self.harness:
            witness = Witness(mutation_id=logical_id, data={target_name: witness_val})
            op = _build_recovery_op(surface, target_name, logical_id, witness_val, inverse_fn, result)
            prog = RecoveryProgram(operations=[op])
            if hasattr(self.harness, "record_external_protected_mutation"):
                self.harness.record_external_protected_mutation(
                    mutation_id=logical_id,
                    description=f"Protected [{self.framework_name}] tool '{target_name}'",
                    witness=witness,
                    recovery_program=prog,
                    declared_effects=declared_effects or [Effect(category=EffectCategory.RESOURCES, target=target_name, op_type=EffectOpType.UPDATE)],
                    identity=identity,
                )

        # 10. Record Committed
        self.reconciler.record_committed(logical_id)
        return result
