"""Declarative Tool Protection API for Autonomous Agents.

Automatically derives recovery components from declarative parameters:
1. Canonical ResourceAddress from target templates
2. Pre-state witness capture query
3. Inverse compensation / recovery operations
4. Post-condition verifiers
"""

from __future__ import annotations
import functools
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Union

from evoundo.actions.classifier import ActionClass
from evoundo.core.harness import EvoUndoHarness
from evoundo.effects.address import ResourceAddress
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.identity import MutationIdentity, derive_canonical_identity
from evoundo.reconciliation.reconciler import (
    ReconciliationStatus,
    FailureStage,
    MutationLifecycleState,
)
from evoundo.recovery.operations import CustomRecoveryOp, RecoveryProgram, OP_TYPE_MAP
from evoundo.witness import Witness, WitnessCaptureError

logger = logging.getLogger("evoundo.protection.declarative")


@dataclass
class ToolDefinition:
    """Represents a tool definition for autonomous agent runtimes."""
    name: str
    description: str
    parameters: Dict[str, Any]
    fn: Callable[..., Any]
    is_mutation: bool = False
    surface: str = "general"
    target: str = "default"
    action_class: Union[ActionClass, str] = ActionClass.REVERSIBLE
    capture_fn: Optional[Callable[..., Any]] = None
    inverse_fn: Optional[Callable[[Any, Any], Any]] = None
    post_condition_probe: Optional[Callable[[], Any]] = None
    post_condition_validator: Optional[Callable[[Any], bool]] = None

    def to_openai_tool(self) -> Dict[str, Any]:
        """Convert to OpenAI-compatible tool JSON schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }



def protect_tool(
    target: str,
    action_class: Union[ActionClass, str] = ActionClass.REVERSIBLE,
    surface: str = "general",
    table: Optional[str] = None,
    pk: Optional[str] = None,
    attribute: Optional[str] = None,
    db_conn_fn: Optional[Callable[[], Any]] = None,
    capture_fn: Optional[Callable[..., Any]] = None,
    harness: Optional[EvoUndoHarness] = None,
    inverse_fn: Optional[Callable[..., Any]] = None,
    post_condition_validator: Optional[Callable[[Any], bool]] = None,
    post_condition_probe: Optional[Callable[[], Any]] = None,
) -> Callable[[Callable[..., Any]], Any]:
    """Declarative decorator protecting an autonomous agent tool with automated witness and inverse."""
    act_class = ActionClass(action_class) if isinstance(action_class, str) else action_class

    def decorator(fn: Callable[..., Any]) -> Any:
        sig = inspect.signature(fn)
        tool_name = fn.__name__
        doc = fn.__doc__ or f"Protected tool {tool_name}"
        is_coroutine = inspect.iscoroutinefunction(fn)

        def _run_pre(args: Any, kwargs: Any):
            if harness is not None:
                active_harness = harness
            else:
                try:
                    from evoundo.decorator import get_default_harness
                    active_harness = get_default_harness()
                except Exception:
                    active_harness = EvoUndoHarness()
            explicit_logical_id = kwargs.pop("__logical_mutation_id", None)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            all_args = dict(bound.arguments)
            if explicit_logical_id is not None:
                all_args["__logical_mutation_id"] = explicit_logical_id

            from evoundo.context import get_current_context
            ctx = get_current_context()

            # 1. Format hierarchical target URI
            formatted_target = target
            try:
                formatted_target = target.format(**all_args)
            except Exception:
                pass
            res_addr = ResourceAddress.parse(formatted_target)

            # 2. Derive canonical logical ID & identity
            explicit_id = all_args.get("__logical_mutation_id")
            parent_log_id = getattr(ctx, "parent_logical_id", None) or getattr(ctx, "active_mutation_id", None)
            if not explicit_id and not getattr(ctx, "active_mutation_id", None):
                explicit_id = ctx.logical_mutation_id

            logical_id = derive_canonical_identity(
                framework="declarative_tool",
                tool_name=tool_name,
                arguments=all_args,
                platform_run_id=ctx.run_id,
                subagent_id=ctx.agent_id,
                tool_call_id=ctx.tool_call_id,
                explicit_logical_id=explicit_id,
                parent_logical_id=parent_log_id,
            )
            mutation_id = f"mut_{logical_id}" if not logical_id.startswith("mut_") else logical_id

            identity_metadata = {"surface": surface, "target": res_addr.canonical_uri}
            if getattr(ctx, "active_mutation_id", None):
                identity_metadata["parent_mutation_id"] = ctx.active_mutation_id
            if parent_log_id:
                identity_metadata["parent_logical_id"] = parent_log_id

            identity = MutationIdentity(
                logical_mutation_id=logical_id,
                framework="declarative_tool",
                framework_run_id=ctx.run_id,
                tool_name=tool_name,
                tool_call_id=ctx.tool_call_id,
                retry_attempt=ctx.retry_attempt,
                metadata=identity_metadata,
            )

            # 3. Check duplicate suppression / retry via reconciler
            if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                probe = post_condition_probe or kwargs.get("post_condition_probe") or all_args.get("__post_condition_probe")
                validator = post_condition_validator or kwargs.get("post_condition_validator") or all_args.get("__post_condition_validator")
                rec_decision = active_harness.reconciler.evaluate_request(
                    identity=identity,
                    current_state_probe=probe,
                    expected_post_condition=validator,
                    mutation_registry=getattr(active_harness, "mutation_registry", None),
                )
                if rec_decision.status == ReconciliationStatus.CONFLICT_DETECTED:
                    logger.error("Declarative tool conflict detected: %s", rec_decision.reason)
                    raise RuntimeError(f"CONFLICT_DETECTED: {rec_decision.reason}")
                if not rec_decision.should_execute_fn:
                    logger.info("Declarative tool duplicate suppressed by reconciler: %s", logical_id)
                    existing_rec = active_harness.mutation_registry.inspect_mutation(mutation_id) if hasattr(active_harness, "mutation_registry") else None
                    entry = active_harness.reconciler.get_entry(logical_id)
                    entry_epoch = getattr(entry, "epoch", 1) if entry else 1
                    rec_epoch = getattr(existing_rec, "epoch", 0) if existing_rec else 0
                    if hasattr(active_harness, "mutation_registry") and (
                        existing_rec is None
                        or getattr(existing_rec, "status", None) == MutationLifecycleState.REVERTED.value
                        or rec_epoch < entry_epoch
                    ):
                        eff_ops = []
                        if entry and getattr(entry, "recovery_ops", None):
                            for op_dict in entry.recovery_ops:
                                op_type = op_dict.get("op_type")
                                op_cls = OP_TYPE_MAP.get(op_type)
                                if op_cls and hasattr(op_cls, "from_dict"):
                                    eff_ops.append(op_cls.from_dict(op_dict))
                                elif op_cls:
                                    eff_ops.append(op_cls(**{k: v for k, v in op_dict.items() if k != "op_type"}))
                        if not eff_ops:
                            if inverse_fn:
                                def make_recon_runner(inv: Callable[..., Any], tgt: str, res: Any):
                                    def _runner(state: Any, wit: Any):
                                        w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                                        val = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                                        if val is None:
                                            val = wit.get("witness") if hasattr(wit, "get") else w_dict.get("witness")
                                        inv(val, res)
                                    return _runner

                                op = CustomRecoveryOp(
                                    name=f"Revert:{tool_name}:{logical_id}",
                                    inverse_fn=make_recon_runner(inverse_fn, res_addr.canonical_uri, rec_decision.cached_result),
                                )
                                eff_ops.append(op)
                            else:
                                eff_ops.append(
                                    CustomRecoveryOp(
                                        name=f"Revert:{tool_name}:{logical_id}",
                                        inverse_fn=None,
                                    )
                                )
                        prog = RecoveryProgram(operations=eff_ops)
                        wit_dict = entry.witness_data if entry and entry.witness_data else {res_addr.canonical_uri: None, "witness": None}
                        witness = Witness(mutation_id=mutation_id, data=wit_dict)
                        active_harness.record_external_protected_mutation(
                            mutation_id=mutation_id,
                            description=f"Declarative tool {tool_name} on {res_addr.canonical_uri}",
                            witness=witness,
                            recovery_program=prog,
                            declared_effects=[Effect(category=EffectCategory.RESOURCES, target=res_addr.canonical_uri, op_type=EffectOpType.UPDATE)],
                            identity=identity,
                            epoch=entry_epoch,
                            action_class=act_class,
                            metadata={"result": rec_decision.cached_result, "target": res_addr.canonical_uri, "action_class": str(act_class)},
                        )
                    elif existing_rec and existing_rec.recovery_program and inverse_fn:
                        from evoundo.recovery.operations import DriverRecoveryOp
                        def make_recon_runner(inv: Callable[..., Any], tgt: str, res: Any):
                            def _runner(state: Any, wit: Any):
                                w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                                val = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                                if val is None:
                                    val = wit.get("witness") if hasattr(wit, "get") else w_dict.get("witness")
                                inv(val, res)
                            return _runner
                        bound_args = tuple(args) if args else ()
                        bound_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("__")}
                        bound_clean = {k: v for k, v in all_args.items() if not k.startswith("__")}
                        for op in getattr(existing_rec.recovery_program, "operations", []):
                            if isinstance(op, CustomRecoveryOp) and not isinstance(op, DriverRecoveryOp):
                                if getattr(op, "inverse_fn", None) is None:
                                    op.inverse_fn = make_recon_runner(inverse_fn, res_addr.canonical_uri, rec_decision.cached_result)
                                if getattr(op, "verify_fn", None) is None and capture_fn is not None:
                                    def make_recon_verifier(cap, tgt, b_args, b_kwargs, b_clean):
                                        def _verify(state, wit):
                                            try:
                                                try:
                                                    cur = cap(*b_args, **b_kwargs)
                                                except TypeError:
                                                    try:
                                                        cur = cap(**b_clean)
                                                    except TypeError:
                                                        cur = cap()
                                                w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                                                exp = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                                                if exp is None and "witness" in w_dict:
                                                    exp = w_dict["witness"]
                                                return cur == exp
                                            except Exception:
                                                return False
                                        return _verify
                                    op.verify_fn = make_recon_verifier(capture_fn, res_addr.canonical_uri, bound_args, bound_kwargs, bound_clean)
                    cached_val = {"status": "SUCCESS", "cached": True, "logical_mutation_id": logical_id}
                    if isinstance(rec_decision.cached_result, dict):
                        cached_val.update(rec_decision.cached_result)
                    elif rec_decision.cached_result is not None:
                        cached_val["result"] = rec_decision.cached_result
                    cached_val["cached"] = True
                    return (True, cached_val, None)

            # 4. Derive pre-witness automatically via capture_fn or db_conn_fn
            witness_val = None
            if capture_fn is not None:
                try:
                    witness_val = capture_fn(*args, **kwargs)
                except Exception as e:
                    logger.error("Custom capture_fn failed for %s: %s", formatted_target, e)
                    if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                        try:
                            active_harness.reconciler.record_failed(logical_id, f"Witness capture failed: {e}", failure_stage=FailureStage.CAPTURE)
                        except Exception:
                            pass
                    raise WitnessCaptureError(f"Custom capture_fn failed for {formatted_target}: {e}") from e
            elif db_conn_fn and table and pk and attribute:
                pk_val = all_args.get(pk)
                try:
                    conn = db_conn_fn()
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT {attribute} FROM {table} WHERE {pk} = %s", (pk_val,))
                        row = cur.fetchone()
                        if row:
                            witness_val = row[0]
                except Exception as e:
                    logger.error("Automated witness capture failed for %s: %s", formatted_target, e)
                    if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                        try:
                            active_harness.reconciler.record_failed(logical_id, f"Witness capture failed: {e}", failure_stage=FailureStage.CAPTURE)
                        except Exception:
                            pass
                    raise WitnessCaptureError(f"Automated witness capture failed for {formatted_target}: {e}") from e

            if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                try:
                    active_harness.reconciler.record_witness(
                        logical_id,
                        {res_addr.canonical_uri: witness_val, "target": res_addr.canonical_uri, "witness": witness_val}
                    )
                except Exception as e:
                    logger.error("Failed to persist witness to journal for %s: %s", formatted_target, e)
                    if hasattr(active_harness.reconciler, "record_failed"):
                        try:
                            active_harness.reconciler.record_failed(logical_id, f"Witness persistence failed: {e}", failure_stage=FailureStage.CAPTURE)
                        except Exception:
                            pass
                    raise WitnessCaptureError(f"Failed to persist witness to journal for {formatted_target}: {e}") from e

            from evoundo.context import clone_context
            child_ctx = clone_context(
                ctx,
                active_harness=active_harness,
                active_mutation_id=mutation_id,
                parent_mutation_id=mutation_id,
                parent_logical_id=logical_id,
                logical_mutation_id=None,
                active_target=res_addr.canonical_uri,
                active_recovery_level="dependency_aware",
                active_witness={},
                active_effects=[],
                active_inverses=[],
                active_recovery_ops=[],
                active_inverse_fn=None,
            )

            clean_args = {k: v for k, v in all_args.items() if not k.startswith("__")}
            pack = (active_harness, logical_id, mutation_id, identity, res_addr, clean_args, witness_val, all_args, child_ctx)
            return (False, None, pack)

        def _run_error(active_harness: Any, logical_id: str, ctx: Any, e: Exception) -> None:
            if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                try:
                    active_harness.reconciler.record_failed(logical_id, str(e), failure_stage=FailureStage.EXECUTION)
                except Exception:
                    pass
            ctx.active_mutation_id = None
            ctx.active_target = None
            ctx.active_witness = {}
            ctx.active_inverse_fn = None
            ctx.active_recovery_level = None
            ctx.active_effects = []
            ctx.active_inverses = []
            ctx.active_recovery_ops = []

        def _run_post(
            active_harness: Any,
            logical_id: str,
            mutation_id: str,
            identity: Any,
            res_addr: Any,
            clean_args: Dict[str, Any],
            witness_val: Any,
            result: Any,
            all_args: Dict[str, Any],
            kwargs: Dict[str, Any],
            ctx: Any,
            args: Tuple[Any, ...] = (),
        ) -> Any:
            # Record mutation completion in reconciler journal IMMEDIATELY after external mutation succeeds
            rec_ops_data = [op.to_dict() for op in ctx.active_recovery_ops if hasattr(op, "to_dict")]
            if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                try:
                    active_harness.reconciler.record_mutation_executed(
                        logical_id, result=result, recovery_ops=rec_ops_data
                    )
                except Exception:
                    pass

            failure_hook = kwargs.get("__failure_injection_hook") or all_args.get("__failure_injection_hook")
            if failure_hook and hasattr(failure_hook, "matches") and failure_hook.matches("AFTER_EXTERNAL_MUTATION", logical_id):
                failure_hook.trigger("AFTER_EXTERNAL_MUTATION", logical_id)

            # 6. Build automated inverse operation
            eff_ops = list(ctx.active_recovery_ops)
            if not eff_ops:
                eff_inverse = inverse_fn
                if eff_inverse is None and ctx.active_inverse_fn:
                    eff_inverse = ctx.active_inverse_fn
                elif eff_inverse is None and db_conn_fn and table and pk and attribute:
                    pk_val = all_args.get(pk)
                    old_val = witness_val
                    def _auto_db_inverse(*a: Any, **kw: Any) -> None:
                        conn = db_conn_fn()
                        with conn.cursor() as cur:
                            cur.execute(f"UPDATE {table} SET {attribute} = %s WHERE {pk} = %s", (old_val, pk_val))
                        conn.commit()
                    eff_inverse = _auto_db_inverse

                if eff_inverse:
                    is_drv = (eff_inverse is ctx.active_inverse_fn)
                    def make_recovery_runner(inv, tgt, res, is_driver):
                        def _recovery_runner(state, wit):
                            w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                            if is_driver:
                                inv(w_dict, res)
                            else:
                                target_val = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                                if target_val is None and "witness" in w_dict:
                                    target_val = w_dict["witness"]
                                inv(target_val, res)
                        return _recovery_runner

                    # Add verification function if capture_fn is available
                    verify_fn = None
                    if capture_fn is not None:
                        bound_args = tuple(args) if args else ()
                        bound_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("__")}
                        bound_clean = dict(clean_args)
                        def make_verifier(cap, tgt, b_args, b_kwargs, b_clean):
                            def _verify(state, wit):
                                try:
                                    try:
                                        cur = cap(*b_args, **b_kwargs)
                                    except TypeError:
                                        try:
                                            cur = cap(**b_clean)
                                        except TypeError:
                                            cur = cap()
                                    w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                                    exp = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                                    if exp is None and "witness" in w_dict:
                                        exp = w_dict["witness"]
                                    return cur == exp
                                except Exception:
                                    return False
                            return _verify
                        verify_fn = make_verifier(capture_fn, res_addr.canonical_uri, bound_args, bound_kwargs, bound_clean)

                    op = CustomRecoveryOp(
                        name=f"Revert:{tool_name}:{logical_id}",
                        inverse_fn=make_recovery_runner(eff_inverse, res_addr.canonical_uri, result, is_drv),
                        verify_fn=verify_fn,
                    )
                    eff_ops.append(op)

            # 7. Record with EvoUndo harness
            prog = RecoveryProgram(operations=eff_ops)
            witness_data = {
                res_addr.canonical_uri: witness_val,
                "target": res_addr.canonical_uri,
                "witness": witness_val,
            }
            if ctx.active_witness:
                witness_data.update(ctx.active_witness)
            witness = Witness(mutation_id=mutation_id, data=witness_data)
            declared_effects = ctx.active_effects or [
                Effect(category=EffectCategory.RESOURCES, target=res_addr.canonical_uri, op_type=EffectOpType.UPDATE)
            ]

            cur_epoch = getattr(active_harness.reconciler.get_entry(logical_id), "epoch", 1) if hasattr(active_harness, "reconciler") and active_harness.reconciler else 1
            active_harness.record_external_protected_mutation(
                mutation_id=mutation_id,
                description=f"Declarative tool {tool_name} on {res_addr.canonical_uri}",
                witness=witness,
                recovery_program=prog,
                declared_effects=declared_effects,
                identity=identity,
                epoch=cur_epoch,
                action_class=act_class,
                metadata={"result": result, "surface": surface, "target": res_addr.canonical_uri, "action_class": str(act_class)},
            )

            if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                try:
                    active_harness.reconciler.record_witness(logical_id, witness_data)
                    active_harness.reconciler.record_committed(logical_id)
                except Exception:
                    pass

            ctx.active_mutation_id = None
            ctx.active_target = None
            ctx.active_witness = {}
            ctx.active_inverse_fn = None
            ctx.active_recovery_level = None
            ctx.active_effects = []
            ctx.active_inverses = []
            ctx.active_recovery_ops = []

            return result

        if is_coroutine:
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                is_cached, cached_val, pack = _run_pre(args, kwargs)
                if is_cached:
                    return cached_val
                (active_harness, logical_id, mutation_id, identity, res_addr, clean_args, witness_val, all_args, ctx) = pack
                from evoundo.context import current_evoundo_context
                token = current_evoundo_context.set(ctx)
                try:
                    result = await fn(**clean_args)
                    return _run_post(active_harness, logical_id, mutation_id, identity, res_addr, clean_args, witness_val, result, all_args, kwargs, ctx, args)
                except Exception as e:
                    _run_error(active_harness, logical_id, ctx, e)
                    raise
                finally:
                    current_evoundo_context.reset(token)
        else:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                is_cached, cached_val, pack = _run_pre(args, kwargs)
                if is_cached:
                    return cached_val
                (active_harness, logical_id, mutation_id, identity, res_addr, clean_args, witness_val, all_args, ctx) = pack
                from evoundo.context import current_evoundo_context
                token = current_evoundo_context.set(ctx)
                try:
                    result = fn(**clean_args)
                    return _run_post(active_harness, logical_id, mutation_id, identity, res_addr, clean_args, witness_val, result, all_args, kwargs, ctx, args)
                except Exception as e:
                    _run_error(active_harness, logical_id, ctx, e)
                    raise
                finally:
                    current_evoundo_context.reset(token)

        # Helper method to export directly to ToolDefinition for agent frameworks
        def to_tool_def() -> ToolDefinition:
            params: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}
            for p_name, param in sig.parameters.items():
                if p_name.startswith("__"):
                    continue
                params["properties"][p_name] = {"type": "string"}
                if param.default is inspect.Parameter.empty:
                    params["required"].append(p_name)

            return ToolDefinition(
                name=tool_name,
                description=doc,
                parameters=params,
                fn=wrapper,
                is_mutation=True,
                surface=surface,
                target=target,
                action_class=act_class,
                capture_fn=capture_fn,
                inverse_fn=inverse_fn,
            )

        wrapper.to_tool_def = to_tool_def
        wrapper.raw_fn = fn
        return wrapper

    return decorator
