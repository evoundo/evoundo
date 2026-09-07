"""Universal @harness.protect decorator and wrapper for agent tools and functions."""

from __future__ import annotations
import functools
import inspect
import time
from typing import Any, Callable, Dict, List, Optional, Union

from evoundo.admission.policies import AdmissionDecision, AdmissionStatus
from evoundo.core.state import FileDescriptor, HarnessState
from evoundo.effects.contracts import Effect, EffectCategory, EffectContract, EffectOpType
from evoundo.identity import MutationIdentity
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger
from evoundo.reconciliation.reconciler import (
    FailureStage,
    MutationLifecycleState,
    MutationReconciler,
    ReconciliationStatus,
)
from evoundo.recovery.operations import CustomRecoveryOp, RecoveryProgram
from evoundo.witness import Witness, WitnessCaptureError


class ProtectedToolWrapper:
    """Wraps an existing tool callable with EvoUndo witness capture, reconciliation, and recovery."""

    def __init__(
        self,
        fn: Callable[..., Any],
        harness: Optional[Any] = None,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        side_effect_detector: Optional[Callable[[], List[Effect]]] = None,
        framework: str = "generic",
        event_logger: Optional[StructuredEventLogger] = None,
        reconciler: Optional[MutationReconciler] = None,
    ):
        self.fn = fn
        self.harness = harness
        self.surface = surface
        self.target = target or getattr(fn, "__name__", "tool")
        self.declared_effects = declared_effects or [
            Effect(category=EffectCategory.RESOURCES, target=self.target, op_type=EffectOpType.UPDATE)
        ]
        self.capture_fn = capture_fn
        self.inverse_fn = inverse_fn
        self.post_condition_probe = post_condition_probe
        self.post_condition_validator = post_condition_validator
        self.side_effect_detector = side_effect_detector
        self.framework = framework
        self.event_logger = event_logger or (harness.event_logger if harness else default_event_logger)
        self.reconciler = reconciler or (getattr(harness, "reconciler", None) if harness else MutationReconciler(self.event_logger))

        # Copy original function metadata
        functools.update_wrapper(self, fn)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        # Extract contextual IDs if passed in kwargs or metadata
        framework_run_id = kwargs.pop("__framework_run_id", None) or kwargs.pop("run_id", None) or kwargs.pop("thread_id", None)
        tool_call_id = kwargs.pop("__tool_call_id", None) or kwargs.pop("tool_call_id", None)
        logical_id = kwargs.pop("__logical_mutation_id", None)
        failure_hook = kwargs.pop("__failure_injection_hook", None)

        # 1. Generate / retrieve Identity
        identity = MutationIdentity.create(
            logical_mutation_id=logical_id,
            framework=self.framework,
            framework_run_id=str(framework_run_id) if framework_run_id else None,
            tool_name=self.target,
            tool_call_id=str(tool_call_id) if tool_call_id else None,
            tool_args={"args": [str(a) for a in args], "kwargs": {k: str(v) for k, v in kwargs.items()}},
        )

        m_id = identity.logical_mutation_id
        mutation_id = f"mut_{m_id}" if not m_id.startswith("mut_") else m_id

        # 2. Check Reconciliation on Retries
        rec_decision = self.reconciler.evaluate_request(
            identity=identity,
            current_state_probe=self.post_condition_probe,
            expected_post_condition=self.post_condition_validator,
            mutation_registry=getattr(self.harness, "mutation_registry", None) if self.harness else None,
        )

        if rec_decision.status == ReconciliationStatus.CONFLICT_DETECTED:
            raise RuntimeError(f"CONFLICT_DETECTED: {rec_decision.reason}")

        if not rec_decision.should_execute_fn:
            # Duplicate execution suppressed! Ensure harness has recovery registered if needed
            if self.harness:
                reg = getattr(self.harness, "mutation_registry", None)
                existing_rec = reg.inspect_mutation(mutation_id) if (reg and hasattr(reg, "inspect_mutation")) else None
                entry = self.reconciler.get_entry(m_id) if hasattr(self.reconciler, "get_entry") else None
                entry_epoch = getattr(entry, "epoch", 1) if entry else 1
                rec_epoch = getattr(existing_rec, "epoch", 0) if existing_rec else 0
                if reg and (
                    existing_rec is None
                    or getattr(existing_rec, "status", None) == MutationLifecycleState.REVERTED.value
                    or rec_epoch < entry_epoch
                ):
                    wit_val = entry.witness_data.get(self.target) if (entry and entry.witness_data) else None
                    witness = Witness(mutation_id=mutation_id, data={self.target: wit_val} if self.target else {})
                    if self.inverse_fn:
                        op = CustomRecoveryOp(
                            name=f"Revert:{self.target}:{m_id}",
                            inverse_fn=lambda state, wit, inv=self.inverse_fn, tgt=self.target, res=rec_decision.cached_result: inv(wit.get(tgt), res),
                        )
                    else:
                        op = CustomRecoveryOp(
                            name=f"ReconciledUndo:{self.target}:{m_id}",
                            inverse_fn=lambda state, wit: None,
                        )
                    prog = RecoveryProgram(operations=[op])
                    if hasattr(self.harness, "record_external_protected_mutation"):
                        self.harness.record_external_protected_mutation(
                            mutation_id=mutation_id,
                            description=f"Protected tool call '{self.target}'",
                            witness=witness,
                            recovery_program=prog,
                            declared_effects=self.declared_effects,
                            epoch=entry_epoch,
                            identity=identity,
                            metadata={"result": rec_decision.cached_result, "target": self.target},
                        )
                elif existing_rec and existing_rec.recovery_program and self.inverse_fn:
                    # Re-attach inverse_fn to reloaded recovery program if lost after restart
                    from evoundo.recovery.operations import DriverRecoveryOp
                    for op in getattr(existing_rec.recovery_program, "operations", []):
                        if isinstance(op, CustomRecoveryOp) and not isinstance(op, DriverRecoveryOp) and getattr(op, "inverse_fn", None) is None:
                            op.inverse_fn = lambda state, wit, inv=self.inverse_fn, tgt=self.target, res=rec_decision.cached_result: inv(wit.get(tgt) if hasattr(wit, "get") else (wit.data.get(tgt) if hasattr(wit, "data") else wit), res)
            return rec_decision.cached_result

        # Injection Point: BEFORE_WITNESS_CAPTURE
        if failure_hook and failure_hook.matches("BEFORE_WITNESS_CAPTURE", m_id):
            failure_hook.trigger("BEFORE_WITNESS_CAPTURE", m_id)

        # 3. Capture Pre-State Witness
        witness_value = None
        if self.capture_fn:
            try:
                witness_value = self.capture_fn(*args, **kwargs)
            except Exception as e:
                self.event_logger.emit(
                    event_type=EventType.ERROR,
                    mutation_id=m_id,
                    message=f"Witness capture failed for {m_id}: {e}",
                )
                if hasattr(self.reconciler, "record_failed"):
                    try:
                        self.reconciler.record_failed(m_id, f"Witness capture failed: {e}", failure_stage=FailureStage.CAPTURE)
                    except Exception:
                        pass
                raise WitnessCaptureError(f"Witness capture failed for {m_id}: {e}") from e

        self.reconciler.record_witness(m_id, {self.target: witness_value})
        witness = Witness(
            mutation_id=mutation_id,
            data={self.target: witness_value},
        )
        self.event_logger.emit(
            event_type=EventType.WITNESS_CAPTURED,
            mutation_id=m_id,
            message=f"Witness captured for target '{self.target}'",
            data={"target": self.target, "witness_preview": str(witness_value)},
        )

        # Injection Point: AFTER_WITNESS_CAPTURE
        if failure_hook and failure_hook.matches("AFTER_WITNESS_CAPTURE", m_id):
            failure_hook.trigger("AFTER_WITNESS_CAPTURE", m_id)

        # Injection Point: BEFORE_EXTERNAL_MUTATION
        if failure_hook and failure_hook.matches("BEFORE_EXTERNAL_MUTATION", m_id):
            failure_hook.trigger("BEFORE_EXTERNAL_MUTATION", m_id)

        # 4. Execute the Protected Tool Mutation
        result = None
        execution_err = None
        try:
            result = self.fn(*args, **kwargs)
        except Exception as e:
            execution_err = e
            self.reconciler.record_failed(m_id, str(e), failure_stage=FailureStage.EXECUTION)
            raise e

        # 5. Record Mutation Success in Reconciler Journal
        self.reconciler.record_mutation_executed(
            logical_mutation_id=m_id,
            result=result,
            effects=[e.to_dict() for e in self.declared_effects],
        )

        # Injection Point: AFTER_EXTERNAL_MUTATION (The classic worker crash point!)
        if failure_hook and failure_hook.matches("AFTER_EXTERNAL_MUTATION", m_id):
            failure_hook.trigger("AFTER_EXTERNAL_MUTATION", m_id)

        # 6. Check Dynamic Side-Effects (Undeclared Side Effect Detection)
        if self.side_effect_detector:
            observed_effects = self.side_effect_detector()
            declared_targets = {e.target for e in self.declared_effects}
            undeclared = [eff for eff in observed_effects if eff.target not in declared_targets]
            if undeclared:
                self.event_logger.emit(
                    event_type=EventType.UNDECLARED_EFFECT_DETECTED,
                    mutation_id=m_id,
                    message=f"Undeclared side effects detected for {m_id}",
                    data={"undeclared": [u.to_dict() for u in undeclared]},
                )
                # Auto fail-closed: If undeclared side effects occur, reject and attempt rollback
                if self.inverse_fn:
                    try:
                        self.inverse_fn(witness_value, result)
                    except Exception:
                        pass
                raise RuntimeError(f"Fail-closed: Undeclared side effects detected on {undeclared}")

        # 7. Pre-validate Recoverability if inverse_fn is provided
        if self.inverse_fn is None:
            self.event_logger.emit(
                event_type=EventType.MUTATION_REJECTED,
                mutation_id=m_id,
                message=f"Mutation rejected: No inverse recovery function provided for '{self.target}'",
            )

        # Injection Point: BEFORE_COMMIT
        if failure_hook and failure_hook.matches("BEFORE_COMMIT", m_id):
            failure_hook.trigger("BEFORE_COMMIT", m_id)

        # 8. Register Recovery Operation in Harness if available
        if self.harness and self.inverse_fn:
            cur_epoch = getattr(self.reconciler.get_entry(m_id), "epoch", 1) if hasattr(self.reconciler, "get_entry") else 1
            op = CustomRecoveryOp(
                name=f"Revert:{self.target}:{m_id}",
                inverse_fn=lambda state, wit, inv=self.inverse_fn, tgt=self.target, res=result: inv(wit.get(tgt), res),
            )
            prog = RecoveryProgram(operations=[op])
            # If harness has active mutation registry, record recovery program
            if hasattr(self.harness, "record_external_protected_mutation"):
                self.harness.record_external_protected_mutation(
                    mutation_id=mutation_id,
                    description=f"Protected tool call '{self.target}'",
                    witness=witness,
                    recovery_program=prog,
                    declared_effects=self.declared_effects,
                    epoch=cur_epoch,
                    identity=identity,
                    metadata={"result": result, "target": self.target},
                )

        # 9. Mark Committed
        self.reconciler.record_committed(m_id)

        # Injection Point: AFTER_COMMIT
        if failure_hook and failure_hook.matches("AFTER_COMMIT", m_id):
            failure_hook.trigger("AFTER_COMMIT", m_id)

        return result


def protect(
    harness: Optional[Any] = None,
    surface: str = "custom",
    target: Optional[str] = None,
    declared_effects: Optional[List[Effect]] = None,
    capture_fn: Optional[Callable[..., Any]] = None,
    inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
    post_condition_probe: Optional[Callable[[], Any]] = None,
    post_condition_validator: Optional[Callable[[Any], bool]] = None,
    side_effect_detector: Optional[Callable[[], List[Effect]]] = None,
    framework: str = "generic",
) -> Callable[[Callable[..., Any]], ProtectedToolWrapper]:
    """Decorator wrapping any function or tool with EvoUndo protection."""
    def decorator(fn: Callable[..., Any]) -> ProtectedToolWrapper:
        return ProtectedToolWrapper(
            fn=fn,
            harness=harness,
            surface=surface,
            target=target or getattr(fn, "__name__", "tool"),
            declared_effects=declared_effects,
            capture_fn=capture_fn,
            inverse_fn=inverse_fn,
            post_condition_probe=post_condition_probe,
            post_condition_validator=post_condition_validator,
            side_effect_detector=side_effect_detector,
            framework=framework,
        )
    return decorator
