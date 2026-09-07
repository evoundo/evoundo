"""EvoUndo 2.0 Parameterized @protect decorator with automatic signature inspection, template binding, and first-class recovery levels."""

from __future__ import annotations
import functools
import hashlib
import inspect
import json
import os
from typing import Any, Callable, Dict, List, Optional, Union

from evoundo.core.harness import EvoUndoHarness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.identity import MutationIdentity
from evoundo.reconciliation.reconciler import (
    FailureStage,
    MutationLifecycleState,
    MutationReconciler,
    ReconciliationStatus,
)
from evoundo.recovery.operations import CustomRecoveryOp, DriverRecoveryOp, RecoveryProgram
from evoundo.witness.stores import Witness
from evoundo.protection.declarative import protect_tool

from evoundo.context import get_current_context
from evoundo.levels import RecoveryLevel
from evoundo.probe import HTTPProbe


# Global singleton harness and reconciler for zero-configuration developer adoption
_DEFAULT_HARNESS: Optional[EvoUndoHarness] = None


from evoundo.paths import get_canonical_registry_path, get_canonical_journal_path


def get_default_harness() -> EvoUndoHarness:
    global _DEFAULT_HARNESS
    if _DEFAULT_HARNESS is None:
        reg_path = get_canonical_registry_path()
        os.makedirs(os.path.dirname(os.path.abspath(reg_path)), exist_ok=True)
        _DEFAULT_HARNESS = EvoUndoHarness(registry_path=reg_path)

        jpath = get_canonical_journal_path()
        os.makedirs(os.path.dirname(os.path.abspath(jpath)), exist_ok=True)
        _DEFAULT_HARNESS.reconciler = MutationReconciler(journal_path=jpath)
    return _DEFAULT_HARNESS


def set_default_harness(harness: EvoUndoHarness) -> None:
    global _DEFAULT_HARNESS
    _DEFAULT_HARNESS = harness


def protect(
    target: Optional[Union[str, Callable[[Dict[str, Any]], str]]] = None,
    recovery: Union[str, RecoveryLevel] = RecoveryLevel.DEPENDENCY_AWARE,
    probe: Optional[Union[HTTPProbe, Callable[..., Any]]] = None,
    validator: Optional[Callable[[Any], bool]] = None,
    inverse: Optional[Callable[..., Any]] = None,
    surface: Optional[str] = None,
    harness: Optional[EvoUndoHarness] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Universal decorator providing automatic argument binding, driver integration, and recovery orchestration.
    
    .. deprecated:: 0.1.0
       Use :func:`protect_tool` instead.
    """
    import warnings
    warnings.warn(
        "@protect is deprecated in EvoUndo 0.1.0 and will be removed in 1.0.0; please use @protect_tool.",
        DeprecationWarning,
        stacklevel=2,
    )
    rec_level = RecoveryLevel(recovery) if isinstance(recovery, str) else recovery

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            active_harness = harness or get_default_harness()

            # 1. Bind runtime arguments to function signature
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            bound_args = bound.arguments

            # 2. Automatically resolve target resource template
            if callable(target):
                resolved_target = str(target(bound_args))
            elif target is not None:
                try:
                    resolved_target = str(target).format(**bound_args)
                except KeyError:
                    resolved_target = str(target)
            else:
                resolved_target = None

            # 3. Derive deterministic mutation identity from framework context or arguments
            ctx = get_current_context()
            m_id = ctx.logical_mutation_id
            if not m_id:
                try:
                    args_str = json.dumps(bound_args, sort_keys=True, default=str)
                except Exception:
                    args_str = str(sorted(bound_args.items()))
                arg_hash = hashlib.sha256(args_str.encode("utf-8")).hexdigest()[:8]
                run_pfx = f"{ctx.run_id}_" if ctx.run_id else ""
                m_id = f"mut_{run_pfx}{fn.__name__}_{arg_hash}"

            # Set active context slots for surface drivers to attach to
            ctx.active_mutation_id = m_id
            ctx.active_target = resolved_target
            ctx.active_recovery_level = rec_level.value
            ctx.active_witness = {}
            ctx.active_effects = []
            ctx.active_inverses = []
            ctx.active_recovery_ops = []
            ctx.active_inverse_fn = None

            identity = MutationIdentity.create(
                logical_mutation_id=m_id,
                framework=ctx.framework,
                framework_run_id=ctx.run_id,
                tool_name=fn.__name__,
                tool_call_id=ctx.tool_call_id,
                tool_args=bound_args,
                retry_attempt=ctx.retry_attempt,
            )

            # 4. Configure reconciliation probe for post-commit uncertainty
            probe_fn = None
            validator_fn = None
            if isinstance(probe, HTTPProbe):
                probe_fn = lambda: probe.execute_probe(bound_args)
                validator_fn = lambda res: probe.evaluate_validator(res)
            elif callable(probe):
                probe_fn = lambda: probe(**bound_args)
                validator_fn = validator

            # 5. Evaluate Reconciler: Suppress duplicate executions under network drops
            # 5. Evaluate Reconciler: Suppress duplicate executions under network drops
            rec_decision = active_harness.reconciler.evaluate_request(
                identity=identity,
                current_state_probe=probe_fn,
                expected_post_condition=validator_fn,
                mutation_registry=getattr(active_harness, "mutation_registry", None),
            )

            if not rec_decision.should_execute_fn:
                # Duplicate execution cleanly suppressed!
                if rec_decision.status == ReconciliationStatus.RECONCILED_DUPLICATE_SUPPRESSED:
                    if hasattr(active_harness, "mutation_registry"):
                        existing_rec = active_harness.mutation_registry.inspect_mutation(m_id)
                        entry = active_harness.reconciler.get_entry(m_id)
                        entry_epoch = getattr(entry, "epoch", 1) if entry else 1
                        rec_epoch = getattr(existing_rec, "epoch", 0) if existing_rec else 0
                        if (
                            existing_rec is None
                            or getattr(existing_rec, "status", None) == MutationLifecycleState.REVERTED.value
                            or rec_epoch < entry_epoch
                        ):
                            journal_entry = getattr(active_harness.reconciler, "_journal", {}).get(m_id)
                            wit_data = dict(journal_entry.witness_data) if journal_entry and journal_entry.witness_data else dict(ctx.active_witness)
                            if resolved_target and resolved_target not in wit_data:
                                wit_data[resolved_target] = None

                            effects = list(declared_effects) if declared_effects else [
                                Effect(category=EffectCategory.RESOURCES, target=resolved_target, op_type=EffectOpType.UPDATE)
                            ]

                            prog = None
                            if ctx.active_recovery_ops:
                                prog = RecoveryProgram(operations=list(ctx.active_recovery_ops))
                            elif ctx.active_inverse_fn or inverse:
                                inv_fn = ctx.active_inverse_fn or inverse
                                is_driver = (inv_fn is ctx.active_inverse_fn)

                                def make_recovery_runner(inv, tgt, res, is_drv):
                                    def _recovery_runner(state, wit):
                                        w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                                        if is_drv:
                                            inv(w_dict, res)
                                        else:
                                            target_val = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                                            inv(target_val, res)
                                    return _recovery_runner

                                op = CustomRecoveryOp(
                                    name=f"Undo:{fn.__name__}:{m_id}",
                                    inverse_fn=make_recovery_runner(inv_fn, resolved_target, rec_decision.cached_result, is_driver),
                                )
                                prog = RecoveryProgram(operations=[op])
                            else:
                                op = CustomRecoveryOp(
                                    name=f"ReconciledUndo:{fn.__name__}:{m_id}",
                                    inverse_fn=lambda state, wit: None,
                                )
                                prog = RecoveryProgram(operations=[op])

                            active_harness.record_external_protected_mutation(
                                mutation_id=m_id,
                                description=f"{fn.__name__} on {resolved_target} (Reconciled Duplicate)",
                                witness=Witness(mutation_id=m_id, data=wit_data),
                                recovery_program=prog,
                                declared_effects=effects,
                                epoch=entry_epoch,
                            )
                return rec_decision.cached_result

            # 6. Execute Forward Tool Logic
            # Surface drivers inside fn will record witnesses and generate inverses into ctx
            result = None
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                active_harness.reconciler.record_failed(m_id, str(e), failure_stage=FailureStage.EXECUTION)
                ctx.active_mutation_id = None
                ctx.active_target = None
                ctx.active_witness = {}
                ctx.active_inverse_fn = None
                ctx.active_recovery_level = None
                ctx.active_effects = []
                ctx.active_inverses = []
                ctx.active_recovery_ops = []
                raise e

            # 7. Adopt dynamic target address if set by surface drivers (e.g. SQLAlchemyDriver)
            if ctx.active_target and (target is None or resolved_target in (None, "None", fn.__name__) or target == "orm"):
                resolved_target = ctx.active_target

            # Record Execution in Durable Journal
            if ctx.active_effects:
                effects = list(ctx.active_effects)
            else:
                effects = [
                    Effect(category=EffectCategory.RESOURCES, target=resolved_target, op_type=EffectOpType.UPDATE)
                ]
            active_harness.reconciler.record_mutation_executed(
                m_id, result=result, effects=[e.to_dict() for e in effects]
            )

            # 8. Register Recovery Program based on Recovery Level
            witness_data = dict(ctx.active_witness)
            if resolved_target not in witness_data:
                witness_data[resolved_target] = None
            active_harness.reconciler.record_witness(m_id, witness_data)

            if rec_level in (RecoveryLevel.INVERT, RecoveryLevel.DEPENDENCY_AWARE, RecoveryLevel.COMPENSATE):
                prog = None
                if ctx.active_recovery_ops:
                    prog = RecoveryProgram(operations=list(ctx.active_recovery_ops))
                elif ctx.active_inverse_fn or inverse:
                    inverse_fn_to_use = ctx.active_inverse_fn or inverse
                    is_driver_inverse = (inverse_fn_to_use is ctx.active_inverse_fn)
                    def make_recovery_runner(inv_fn, tgt, res, is_driver):
                        def _recovery_runner(state, wit):
                            w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                            if is_driver:
                                inv_fn(w_dict, res)
                            else:
                                target_val = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                                inv_fn(target_val, res)
                        return _recovery_runner

                    op = CustomRecoveryOp(
                        name=f"Undo:{fn.__name__}:{m_id}",
                        inverse_fn=make_recovery_runner(inverse_fn_to_use, resolved_target, result, is_driver_inverse),
                    )
                    prog = RecoveryProgram(operations=[op])

                cur_epoch = getattr(active_harness.reconciler.get_entry(m_id), "epoch", 1) if hasattr(active_harness, "reconciler") and active_harness.reconciler else 1
                if prog is not None:
                    active_harness.record_external_protected_mutation(
                        mutation_id=m_id,
                        description=f"{fn.__name__} on {resolved_target}",
                        witness=Witness(mutation_id=m_id, data=witness_data),
                        recovery_program=prog,
                        declared_effects=effects,
                        epoch=cur_epoch,
                        metadata={"result": result, "target": resolved_target},
                    )
            elif rec_level == RecoveryLevel.RECONCILE:
                # Reconcile-only operations (R1) are recorded in harness without requiring an inverse
                op = CustomRecoveryOp(
                    name=f"ReconcileOnly:{fn.__name__}:{m_id}",
                    inverse_fn=lambda state, wit: None,
                )
                prog = RecoveryProgram(operations=[op])
                active_harness.record_external_protected_mutation(
                    mutation_id=m_id,
                    description=f"{fn.__name__} on {resolved_target} (Reconcile Only)",
                    witness=Witness(mutation_id=m_id, data=witness_data),
                    recovery_program=prog,
                    declared_effects=effects,
                )

            active_harness.reconciler.record_committed(m_id)
            
            # Reset active mutation slots
            ctx.active_mutation_id = None
            ctx.active_target = None
            ctx.active_witness = {}
            ctx.active_inverse_fn = None
            ctx.active_recovery_level = None
            ctx.active_effects = []
            ctx.active_inverses = []
            ctx.active_recovery_ops = []
            return result

        return wrapper

    return decorator
