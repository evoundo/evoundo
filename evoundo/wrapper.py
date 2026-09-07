"""
EvoUndo Agent Proxy & Wrapper Layer.

Provides wrapper and proxy interfaces across autonomous agent runtimes:
    from evoundo import wrap

    # Wrap agent instance:
    agent = wrap(my_agent)

    # Or wrap a list of tools directly:
    tools = wrap([search_tool, update_config, delete_user])

    # Or class / function decorator:
    @evoundo
    class SREAgent: ...
"""

from __future__ import annotations
import functools
import inspect
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from evoundo.actions.classifier import ActionClass
from evoundo.context import execution_context, get_current_context
from evoundo.core.harness import EvoUndoHarness
from evoundo.decorator import get_default_harness
from evoundo.effects.address import ResourceAddress
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.identity import MutationIdentity, derive_canonical_identity
try:
    from evoundo.protection.declarative import ToolDefinition
except ImportError:
    ToolDefinition = None
from evoundo.protection.declarative import protect_tool
from evoundo.reconciliation.reconciler import (
    ReconciliationStatus,
    FailureStage,
    MutationLifecycleState,
)
from evoundo.recovery.operations import CustomRecoveryOp, RecoveryProgram, OP_TYPE_MAP
from evoundo.witness import Witness, WitnessCaptureError

logger = logging.getLogger("evoundo.wrapper")

# Common parameter names that indicate a file target
FILE_PARAM_CANDIDATES = (
    "path",
    "filepath",
    "file_path",
    "filename",
    "file_name",
    "file",
    "target_path",
    "target_file",
    "dest",
    "destination",
)


def _auto_detect_file_target(args_dict: Dict[str, Any]) -> Optional[Path]:
    """Detect if any arguments point to a filesystem path."""
    for key, val in args_dict.items():
        if key.lower() in FILE_PARAM_CANDIDATES and isinstance(val, (str, Path)):
            try:
                p = Path(val).resolve()
                return p
            except Exception:
                pass
    return None


def wrap_tool(
    fn: Callable[..., Any],
    *,
    target: Optional[str] = None,
    action_class: Union[ActionClass, str] = ActionClass.REVERSIBLE,
    surface: Optional[str] = None,
    capture_fn: Optional[Callable[..., Any]] = None,
    inverse_fn: Optional[Callable[..., Any]] = None,
    harness: Optional[EvoUndoHarness] = None,
    **kwargs: Any,
) -> Callable[..., Any]:
    """Wrap any callable tool with transparent EvoUndo state recoverability and deduplication.

    Automatically detects file targets and captures pre-mutation snapshots without
    requiring manual capture_fn / inverse_fn lambdas.
    """
    if getattr(fn, "_evoundo_wrapped", False):
        return fn

    sig = inspect.signature(fn)
    tool_name = getattr(fn, "__name__", "tool")
    tool_doc = getattr(fn, "__doc__", "") or f"Protected {tool_name}"

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        active_harness = harness or get_default_harness()
        explicit_logical_id = kwargs.pop("__logical_mutation_id", None)

        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        all_args = dict(bound.arguments)
        if explicit_logical_id is not None:
            all_args["__logical_mutation_id"] = explicit_logical_id

        # 1. Detect target resource & auto-infer file witness if applicable
        file_path = _auto_detect_file_target(all_args)
        derived_surface = surface or ("filesystem" if file_path else "general")
        resolved_target = target or (f"file://{file_path}" if file_path else f"tool://{tool_name}")
        res_addr = ResourceAddress.parse(resolved_target)

        # Build effective inverse early to support safe retry recovery
        eff_inverse = inverse_fn
        if eff_inverse is None and file_path is not None:
            fp_target = Path(file_path)

            def _auto_file_inverse(wit: Any, res: Any) -> None:
                if wit is None:
                    if fp_target.exists():
                        fp_target.unlink()
                else:
                    fp_target.parent.mkdir(parents=True, exist_ok=True)
                    if isinstance(wit, bytes):
                        fp_target.write_bytes(wit)
                    elif isinstance(wit, str):
                        fp_target.write_bytes(wit.encode("utf-8"))
                    else:
                        fp_target.write_bytes(bytes(wit))

            eff_inverse = _auto_file_inverse

        from evoundo.context import get_current_context
        ctx = get_current_context()

        # 2. Derive canonical logical ID for crash / retry reconciliation
        logical_id = derive_canonical_identity(
            framework="agent_wrapper",
            tool_name=tool_name,
            arguments=all_args,
            platform_run_id=ctx.run_id,
            subagent_id=ctx.agent_id,
            tool_call_id=ctx.tool_call_id,
            explicit_logical_id=all_args.get("__logical_mutation_id") or ctx.logical_mutation_id,
        )
        mutation_id = f"mut_{logical_id}" if not logical_id.startswith("mut_") else logical_id

        identity = MutationIdentity(
            logical_mutation_id=logical_id,
            framework="agent_wrapper",
            framework_run_id=ctx.run_id,
            tool_name=tool_name,
            tool_call_id=ctx.tool_call_id,
            retry_attempt=ctx.retry_attempt,
            metadata={"surface": derived_surface, "target": res_addr.canonical_uri},
        )

        # 3. Duplicate suppression across restarts / retries via reconciler & registry
        if hasattr(active_harness, "reconciler") and active_harness.reconciler:
            probe = kwargs.get("post_condition_probe") or all_args.get("__post_condition_probe")
            validator = kwargs.get("post_condition_validator") or all_args.get("__post_condition_validator")
            rec_decision = active_harness.reconciler.evaluate_request(
                identity=identity,
                current_state_probe=probe,
                expected_post_condition=validator,
                mutation_registry=getattr(active_harness, "mutation_registry", None),
            )
            if rec_decision.status == ReconciliationStatus.CONFLICT_DETECTED:
                logger.error("Wrapped tool conflict detected: %s", rec_decision.reason)
                raise RuntimeError(f"CONFLICT_DETECTED: {rec_decision.reason}")
            if not rec_decision.should_execute_fn:
                logger.info("Wrapped tool duplicate suppressed by reconciler: %s", logical_id)
                # If registry missed this mutation due to crash before registration, reconstruct it:
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
                        if eff_inverse is not None:
                            def make_recon_runner(inv: Callable[..., Any], tgt: str, res: Any):
                                def _runner(state: Any, wit: Any):
                                    w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                                    val = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                                    if val is None:
                                        val = wit.get("witness") if hasattr(wit, "get") else w_dict.get("witness")
                                    inv(val, res)
                                return _runner

                            eff_ops.append(
                                CustomRecoveryOp(
                                    name=f"Revert:{tool_name}:{logical_id}",
                                    inverse_fn=make_recon_runner(eff_inverse, res_addr.canonical_uri, rec_decision.cached_result),
                                )
                            )
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
                        description=f"Wrapped tool {tool_name} on {res_addr.canonical_uri}",
                        witness=witness,
                        recovery_program=prog,
                        declared_effects=[Effect(category=EffectCategory.RESOURCES, target=res_addr.canonical_uri, op_type=EffectOpType.UPDATE)],
                        identity=identity,
                        epoch=entry_epoch,
                        metadata={"result": rec_decision.cached_result, "target": res_addr.canonical_uri},
                    )
                elif existing_rec and existing_rec.recovery_program and eff_inverse is not None:
                    from evoundo.recovery.operations import DriverRecoveryOp
                    def make_recon_runner(inv: Callable[..., Any], tgt: str, res: Any):
                        def _runner(state: Any, wit: Any):
                            w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                            val = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                            if val is None:
                                val = wit.get("witness") if hasattr(wit, "get") else w_dict.get("witness")
                            inv(val, res)
                        return _runner
                    for op in getattr(existing_rec.recovery_program, "operations", []):
                        if isinstance(op, CustomRecoveryOp) and not isinstance(op, DriverRecoveryOp) and getattr(op, "inverse_fn", None) is None:
                            op.inverse_fn = make_recon_runner(eff_inverse, res_addr.canonical_uri, rec_decision.cached_result)
                cached_val = {"status": "SUCCESS", "cached": True, "logical_mutation_id": logical_id}
                if isinstance(rec_decision.cached_result, dict):
                    cached_val.update(rec_decision.cached_result)
                elif rec_decision.cached_result is not None:
                    cached_val["result"] = rec_decision.cached_result
                cached_val["cached"] = True
                return cached_val

        # 4. Pre-state witness capture
        witness_val = None
        existed_before = False
        if capture_fn is not None:
            try:
                witness_val = capture_fn(*args, **kwargs)
            except Exception as e:
                logger.error("Custom capture_fn failed for %s: %s", resolved_target, e)
                if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                    try:
                        active_harness.reconciler.record_failed(logical_id, f"Witness capture failed: {e}", failure_stage=FailureStage.CAPTURE)
                    except Exception:
                        pass
                raise WitnessCaptureError(f"Custom capture_fn failed for {resolved_target}: {e}") from e
        elif file_path is not None:
            existed_before = file_path.exists()
            if existed_before:
                try:
                    witness_val = file_path.read_bytes()
                except Exception as e:
                    logger.error("Failed to snapshot file %s: %s", file_path, e)
                    if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                        try:
                            active_harness.reconciler.record_failed(logical_id, f"Witness capture failed: {e}", failure_stage=FailureStage.CAPTURE)
                        except Exception:
                            pass
                    raise WitnessCaptureError(f"Failed to snapshot file {file_path}: {e}") from e

        if hasattr(active_harness, "reconciler") and active_harness.reconciler:
            try:
                active_harness.reconciler.record_witness(
                    logical_id,
                    {resolved_target: witness_val, "target": resolved_target, "witness": witness_val, "existed_before": existed_before}
                )
            except Exception as e:
                logger.error("Failed to persist witness to journal for %s: %s", resolved_target, e)
                if hasattr(active_harness.reconciler, "record_failed"):
                    try:
                        active_harness.reconciler.record_failed(logical_id, f"Witness persistence failed: {e}", failure_stage=FailureStage.CAPTURE)
                    except Exception:
                        pass
                raise WitnessCaptureError(f"Failed to persist witness to journal for {resolved_target}: {e}") from e

        # 5. Forward execution with isolated context
        from evoundo.context import get_current_context
        ctx = get_current_context()
        prev_harness = getattr(ctx, "active_harness", None)
        prev_recovery_ops = list(ctx.active_recovery_ops)
        prev_mutation_id = ctx.active_mutation_id
        prev_target = ctx.active_target
        prev_witness = dict(ctx.active_witness)
        prev_inverse_fn = ctx.active_inverse_fn
        prev_recovery_level = ctx.active_recovery_level
        prev_effects = list(ctx.active_effects)
        prev_inverses = list(ctx.active_inverses)

        ctx.active_harness = active_harness
        ctx.active_recovery_ops = []
        ctx.active_mutation_id = mutation_id
        ctx.active_target = res_addr.canonical_uri
        ctx.active_witness = {}
        ctx.active_inverse_fn = None
        ctx.active_recovery_level = "dependency_aware"
        ctx.active_effects = []
        ctx.active_inverses = []

        try:
            clean_args = {k: v for k, v in all_args.items() if not k.startswith("__")}
            try:
                result = fn(**clean_args)
            except Exception as e:
                if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                    try:
                        active_harness.reconciler.record_failed(logical_id, str(e), failure_stage=FailureStage.EXECUTION)
                    except Exception:
                        pass
                raise e

            # Record mutation completion in journal IMMEDIATELY after external write succeeds!
            rec_ops_data = [op.to_dict() for op in ctx.active_recovery_ops if hasattr(op, "to_dict")]
            if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                try:
                    active_harness.reconciler.record_mutation_executed(logical_id, result=result, recovery_ops=rec_ops_data)
                except Exception:
                    pass

            failure_hook = kwargs.get("__failure_injection_hook") or all_args.get("__failure_injection_hook")
            if failure_hook and hasattr(failure_hook, "matches") and failure_hook.matches("AFTER_EXTERNAL_MUTATION", logical_id):
                failure_hook.trigger("AFTER_EXTERNAL_MUTATION", logical_id)

            # 6. Synthesize inverse operation
            eff_ops: List[Any] = list(ctx.active_recovery_ops)
            if not eff_ops and eff_inverse is not None:
                def make_recovery_runner(inv: Callable[..., Any], tgt: str, res: Any):
                    def _recovery_runner(state: Any, wit: Any):
                        w_dict = wit.data if hasattr(wit, "data") else (wit if isinstance(wit, dict) else {})
                        target_val = wit.get(tgt) if hasattr(wit, "get") else w_dict.get(tgt)
                        if target_val is None and "witness" in w_dict:
                            target_val = w_dict["witness"]
                        inv(target_val, res)
                    return _recovery_runner

                verify_fn = None
                if capture_fn is not None:
                    bound_args = tuple(args) if args else ()
                    bound_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("__")}
                    bound_clean = {k: v for k, v in all_args.items() if not k.startswith("__")}
                    def make_wrapper_verifier(cap, tgt, b_args, b_kwargs, b_clean):
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
                    verify_fn = make_wrapper_verifier(capture_fn, res_addr.canonical_uri, bound_args, bound_kwargs, bound_clean)

                eff_ops.append(
                    CustomRecoveryOp(
                        name=f"Revert:{tool_name}:{logical_id}",
                        inverse_fn=make_recovery_runner(eff_inverse, res_addr.canonical_uri, result),
                        verify_fn=verify_fn,
                    )
                )

            # 7. Record with EvoUndo harness
            res_addr = ResourceAddress.parse(resolved_target)
            identity = MutationIdentity(
                logical_mutation_id=logical_id,
                framework="agent_wrapper",
                tool_name=tool_name,
                metadata={"surface": derived_surface, "target": res_addr.canonical_uri},
            )
            prog = RecoveryProgram(operations=eff_ops)
            witness_data = {
                res_addr.canonical_uri: witness_val,
                "target": res_addr.canonical_uri,
                "witness": witness_val,
                "existed_before": existed_before,
            }
            witness = Witness(mutation_id=mutation_id, data=witness_data)
            declared_effects = [
                Effect(category=EffectCategory.RESOURCES, target=res_addr.canonical_uri, op_type=EffectOpType.UPDATE)
            ]

            cur_epoch = getattr(active_harness.reconciler.get_entry(logical_id), "epoch", 1) if hasattr(active_harness, "reconciler") and active_harness.reconciler else 1
            active_harness.record_external_protected_mutation(
                mutation_id=mutation_id,
                description=f"Wrapped tool {tool_name} on {res_addr.canonical_uri}",
                witness=witness,
                recovery_program=prog,
                declared_effects=declared_effects,
                identity=identity,
                epoch=cur_epoch,
                metadata={"result": result, "target": res_addr.canonical_uri},
            )

            if hasattr(active_harness, "reconciler") and active_harness.reconciler:
                try:
                    active_harness.reconciler.record_witness(logical_id, witness_data)
                    active_harness.reconciler.record_committed(logical_id)
                except Exception:
                    pass

            return result
        finally:
            ctx.active_harness = prev_harness
            ctx.active_recovery_ops = prev_recovery_ops
            ctx.active_mutation_id = prev_mutation_id
            ctx.active_target = prev_target
            ctx.active_witness = prev_witness
            ctx.active_inverse_fn = prev_inverse_fn
            ctx.active_recovery_level = prev_recovery_level
            ctx.active_effects = prev_effects
            ctx.active_inverses = prev_inverses

    # Export ToolDefinition compatibility for LangGraph / OpenAI
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
            description=tool_doc,
            parameters=params,
            fn=wrapper,
            is_mutation=True,
            surface="general",
            target=f"tool://{tool_name}",
            action_class=action_class,
            capture_fn=capture_fn,
            inverse_fn=inverse_fn,
        )

    wrapper.to_tool_def = to_tool_def
    wrapper.raw_fn = fn
    wrapper._evoundo_wrapped = True
    return wrapper


def wrap(agent_or_tools: Any, **kwargs: Any) -> Any:
    """Frictionless entrypoint to wrap an agent, a list of tools, or a single tool.

    Examples:
        # Wrap an agent (LangGraph, CrewAI, OpenAI Agents, custom classes):
        agent = wrap(my_agent)

        # Wrap a list of tool functions:
        tools = wrap([create_file, update_db, send_msg])

        # Wrap a single function:
        tool = wrap(my_function)
    """
    # 1. Single function / callable
    if callable(agent_or_tools) and not hasattr(agent_or_tools, "tools"):
        return wrap_tool(agent_or_tools, **kwargs)

    # 2. List or sequence of tools
    if isinstance(agent_or_tools, (list, tuple)):
        return [wrap_tool(t, **kwargs) if callable(t) else t for t in agent_or_tools]

    # 3. Agent object with `.tools` attribute (LangChain, CrewAI, OpenAI, Smolagents, etc.)
    if hasattr(agent_or_tools, "tools"):
        raw_tools = getattr(agent_or_tools, "tools")
        if isinstance(raw_tools, list):
            agent_or_tools.tools = [wrap_tool(t, **kwargs) if callable(t) else t for t in raw_tools]
        elif isinstance(raw_tools, dict):
            agent_or_tools.tools = {k: wrap_tool(v, **kwargs) if callable(v) else v for k, v in raw_tools.items()}

        # Wrap execution entrypoints if present
        for entrypoint_name in ("invoke", "run", "__call__", "execute"):
            if hasattr(agent_or_tools, entrypoint_name):
                orig_entrypoint = getattr(agent_or_tools, entrypoint_name)
                if not getattr(orig_entrypoint, "_evoundo_wrapped", False):
                    @functools.wraps(orig_entrypoint)
                    def _wrapped_entrypoint(*e_args: Any, **e_kwargs: Any) -> Any:
                        return orig_entrypoint(*e_args, **e_kwargs)
                    _wrapped_entrypoint._evoundo_wrapped = True
                    setattr(agent_or_tools, entrypoint_name, _wrapped_entrypoint)

        return agent_or_tools

    # 4. Fallback: return as-is
    return agent_or_tools


def evoundo(cls_or_fn: Any = None, **kwargs: Any) -> Any:
    """Decorator syntax for classes and functions:

    @evoundo
    class SREAgent:
        def __init__(self):
            self.tools = [update_config]

    @evoundo
    def modify_server(path: str, data: str):
        ...
    """
    def _decorator(target: Any) -> Any:
        if inspect.isclass(target):
            orig_init = target.__init__
            @functools.wraps(orig_init)
            def _wrapped_init(self: Any, *args: Any, **init_kwargs: Any) -> None:
                orig_init(self, *args, **init_kwargs)
                wrap(self, **kwargs)
            target.__init__ = _wrapped_init
            return target
        return wrap_tool(target, **kwargs)

    if cls_or_fn is not None:
        return _decorator(cls_or_fn)
    return _decorator
