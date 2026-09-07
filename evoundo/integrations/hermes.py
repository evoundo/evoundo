"""Hermes Autonomous Agent Framework EvoUndo Integration.

Provides official tool lifecycle hook interception (`pre_tool_call` with veto/blocking,
`post_tool_call`), session identity binding, subagent delegation lineage,
and scheduled task recovery for the Nous Research Hermes Agent runtime.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import functools
import inspect
import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from evoundo.core.harness import EvoUndoHarness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.identity import MutationIdentity
from evoundo.integrations.base import (
    AgentFrameworkAdapter,
    FrameworkContext,
    ToolClassification,
    _build_recovery_op,
)
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger
from evoundo.reconciliation.reconciler import (
    MutationReconciler,
    ReconciliationDecision,
    ReconciliationStatus,
)
from evoundo.recovery.operations import (
    BaseRecoveryOp,
    CustomRecoveryOp,
    DriverRecoveryOp,
    RecoveryProgram,
)
from evoundo.witness.stores import Witness

logger = logging.getLogger(__name__)


@dataclass
class HermesToolSpec:
    """Registration specification for an EvoUndo-protected Hermes tool."""
    tool_name: str
    surface: str = "custom"
    target: Optional[str] = None
    declared_effects: Optional[List[Effect]] = None
    capture_fn: Optional[Callable[..., Any]] = None
    inverse_fn: Optional[Callable[[Any, Any], Any]] = None
    post_condition_probe: Optional[Callable[[], Any]] = None
    post_condition_validator: Optional[Callable[[Any], bool]] = None
    conflict_checker: Optional[Callable[[str, Dict[str, Any]], Optional[str]]] = None


def _wrap_hermes_inverse(
    inverse_fn: Callable[..., Any],
    target_name: str,
    default_witness: Any,
    default_result: Any,
) -> Callable[..., Any]:
    """Wrap a user/tool inverse function for DriverRecoveryOp.apply(state, witness) invocation.

    Supports both (witness_val, result) where witness_val is extracted from witness["value"],
    witness.data, or witness directly, as well as standard (state, witness) signatures.
    """
    def wrapped(state_or_wit: Any, wit_or_res: Any) -> Any:
        is_state_first = (
            hasattr(state_or_wit, "config")
            or hasattr(state_or_wit, "env")
            or type(state_or_wit).__name__ == "HarnessState"
        )

        if is_state_first:
            state = state_or_wit
            witness = wit_or_res

            # Extract witness_val from witness object or dict
            w_val = None
            if hasattr(witness, "data"):
                data = getattr(witness, "data")
                if isinstance(data, dict):
                    if "value" in data:
                        w_val = data["value"]
                    elif target_name in data:
                        w_val = data[target_name]
                    elif "old_value" in data:
                        w_val = data["old_value"]
                    else:
                        w_val = data
                else:
                    w_val = data
            elif isinstance(witness, dict):
                if "value" in witness:
                    w_val = witness["value"]
                elif target_name in witness:
                    w_val = witness[target_name]
                elif "old_value" in witness:
                    w_val = witness["old_value"]
                elif "data" in witness and isinstance(witness["data"], dict):
                    d = witness["data"]
                    if "value" in d:
                        w_val = d["value"]
                    elif target_name in d:
                        w_val = d[target_name]
                    elif "old_value" in d:
                        w_val = d["old_value"]
                    else:
                        w_val = d
                else:
                    w_val = witness
            else:
                w_val = witness

            if w_val is None and default_witness is not None:
                w_val = default_witness

            res = default_result
        else:
            state = None
            witness = None
            w_val = state_or_wit
            res = wit_or_res

        # Check signature parameter names
        try:
            sig = inspect.signature(inverse_fn)
            params = list(sig.parameters.values())
            param_names = [p.name.lower() for p in params]
            if param_names and param_names[0] in ("state", "harness_state") and is_state_first:
                return inverse_fn(state, witness)
        except Exception:
            pass

        # Attempt invocation with (witness_val, result)
        try:
            return inverse_fn(w_val, res)
        except TypeError as te:
            import sys
            tb = sys.exc_info()[2]
            if tb and tb.tb_next is not None:
                raise
            if is_state_first:
                return inverse_fn(state, witness)
            raise

    return wrapped


def _build_hermes_recovery_op(
    surface: str,
    target_name: str,
    logical_id: str,
    witness_val: Any,
    inverse_fn: Optional[Callable[[Any, Any], Any]],
    result: Any,
) -> BaseRecoveryOp:
    wrapped_inv = (
        _wrap_hermes_inverse(inverse_fn, target_name, witness_val, result)
        if inverse_fn is not None
        else None
    )

    if surface == "redis":
        redis_url = os.environ.get("EVOUNDO_REDIS_URL", "redis://127.0.0.1:6379/0")
        key = target_name
        if key.startswith("redis://"):
            key = key[len("redis://"):]

        # Handle Redis hash field recovery (e.g. key contains "/field/")
        if "/field/" in key:
            hash_key, field_name = key.split("/field/", 1)
            if wrapped_inv is not None:
                hash_inv = wrapped_inv
            else:
                def _default_hash_inverse(state: Any, witness: Any) -> None:
                    from evoundo.recovery.driver_registry import DriverRegistry, _decode_b64
                    client = DriverRegistry.get_or_create_redis(redis_url)
                    old_val = _decode_b64(witness_val)
                    if old_val is None:
                        client.hdel(hash_key, field_name)
                    else:
                        client.hset(hash_key, field_name, old_val)
                hash_inv = _default_hash_inverse

            def _hash_verifier(state: Any, witness: Any) -> bool:
                from evoundo.recovery.driver_registry import DriverRegistry, _decode_b64
                try:
                    client = DriverRegistry.get_or_create_redis(redis_url)
                    curr = client.hget(hash_key, field_name)
                    old_val = _decode_b64(witness_val)
                    if old_val is None:
                        return curr is None
                    if isinstance(old_val, bytes):
                        return curr == old_val
                    curr_str = curr.decode("utf-8", errors="replace") if isinstance(curr, bytes) else str(curr)
                    return curr_str == str(old_val)
                except Exception:
                    return False

            return CustomRecoveryOp(
                name=f"Revert:redis:hash:{key}:{logical_id}",
                inverse_fn=hash_inv,
                verify_fn=_hash_verifier,
            )

        redis_w = {
            "exists": witness_val is not None,
            "old_value": witness_val,
        }
        return DriverRecoveryOp(
            driver_type="redis",
            target=f"redis://{key}",
            operation="SET",
            parameters={"redis_url": redis_url, "key": key},
            witness_data=redis_w,
            inverse_fn=wrapped_inv,
        )
    elif surface in ("mysql", "orm"):
        mysql_url = os.environ.get("EVOUNDO_MYSQL_URL", "mysql+pymysql://root:evoundo@127.0.0.1:3307/evoundo_test")
        table_name = target_name
        key_val = None
        if target_name.startswith("mysql://"):
            stripped = target_name[len("mysql://"):]
            parts = stripped.split("/")
            table_name = parts[0]
            if len(parts) > 1:
                key_val = parts[1]

        pk = None
        pk_cols = ["id"]
        if isinstance(result, dict):
            pk = result.get("id") or result.get("rec_id")
        elif isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    pk = parsed.get("id") or parsed.get("rec_id")
            except Exception:
                pass

        if pk is None and key_val is not None:
            pk = key_val
            pk_cols = ["key_name"]
        elif pk is None:
            pk = 1

        return DriverRecoveryOp(
            driver_type="orm",
            target=f"mysql://{table_name}",
            operation="INSERT",
            parameters={
                "db_url": mysql_url,
                "table_name": table_name,
                "pk": pk,
                "pk_cols": pk_cols,
            },
            witness_data=witness_val,
            inverse_fn=wrapped_inv,
        )
    elif wrapped_inv:
        return CustomRecoveryOp(
            name=f"Revert:{target_name}:{logical_id}",
            inverse_fn=wrapped_inv,
        )
    else:
        return CustomRecoveryOp(
            name=f"Protected:{target_name}:{logical_id}",
            inverse_fn=None,
        )


class HermesAdapter(AgentFrameworkAdapter):
    """Adapter for Hermes tool registry dispatch, official lifecycle hooks, and recovery."""

    def __init__(
        self,
        harness: Optional[EvoUndoHarness] = None,
        event_logger: Optional[StructuredEventLogger] = None,
        auto_register_hooks: bool = False,
    ):
        super().__init__(harness=harness, event_logger=event_logger)
        self._tool_specs: Dict[str, HermesToolSpec] = {}
        self._active_mutations: Dict[str, Dict[str, Any]] = {}
        self._suppressed_calls: List[str] = []
        self._blocked_calls: List[Dict[str, Any]] = []
        if auto_register_hooks:
            self.register_with_hermes()

    @property
    def framework_name(self) -> str:
        return "hermes"

    def classify_tool(self, tool_name: str, tool_callable: Optional[Callable[..., Any]] = None) -> ToolClassification:
        """Classify Hermes tools: read-only actions bypass mutation tracking."""
        read_prefixes = (
            "memory_read", "skill_inspect", "get_", "read_", "query_",
            "list_", "view_", "cat_", "find_", "inspect_", "describe_", "show_"
        )
        if tool_name.startswith(read_prefixes):
            return ToolClassification.READ_ONLY
        return ToolClassification.MUTATING

    def register_tool_spec(
        self,
        tool_name: str,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        conflict_checker: Optional[Callable[[str, Dict[str, Any]], Optional[str]]] = None,
    ) -> HermesToolSpec:
        """Register tool protection parameters for official lifecycle hooks."""
        spec = HermesToolSpec(
            tool_name=tool_name,
            surface=surface,
            target=target or tool_name,
            declared_effects=declared_effects or [
                Effect(category=EffectCategory.RESOURCES, target=target or tool_name, op_type=EffectOpType.UPDATE)
            ],
            capture_fn=capture_fn,
            inverse_fn=inverse_fn,
            post_condition_probe=post_condition_probe,
            post_condition_validator=post_condition_validator,
            conflict_checker=conflict_checker,
        )
        self._tool_specs[tool_name] = spec
        return spec

    def extract_context(self, *args: Any, **kwargs: Any) -> FrameworkContext:
        """Extract standardized FrameworkContext from Hermes hook arguments."""
        session_id = kwargs.get("session_id")
        task_id = kwargs.get("task_id") or kwargs.get("run_id")
        tool_name = kwargs.get("tool_name", "hermes_tool")
        tool_call_id = kwargs.get("tool_call_id") or kwargs.get("call_id")
        agent_id = kwargs.get("agent_id", "hermes_agent")
        turn_id = kwargs.get("turn_id", "")
        api_request_id = kwargs.get("api_request_id", "")
        logical_mutation_id = kwargs.get("logical_mutation_id") or kwargs.get("__logical_mutation_id")
        retry_attempt = int(kwargs.get("retry_attempt", 0))

        # Check in args dict if passed
        if args and isinstance(args[0], dict):
            arg_dict = args[0]
            if not session_id and "session_id" in arg_dict:
                session_id = arg_dict.get("session_id")
            if not task_id and "task_id" in arg_dict:
                task_id = arg_dict.get("task_id")
            if not tool_call_id and "tool_call_id" in arg_dict:
                tool_call_id = arg_dict.get("tool_call_id")
            if not logical_mutation_id and "__logical_mutation_id" in arg_dict:
                logical_mutation_id = arg_dict.get("__logical_mutation_id")
            if "retry_attempt" in arg_dict:
                retry_attempt = int(arg_dict.get("retry_attempt", 0))

        return FrameworkContext(
            framework_name=self.framework_name,
            session_id=session_id,
            run_id=task_id,
            thread_id=session_id or task_id,
            agent_id=agent_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            logical_mutation_id=logical_mutation_id,
            retry_attempt=retry_attempt,
            metadata={
                "turn_id": turn_id,
                "api_request_id": api_request_id,
                "subagent_id": kwargs.get("subagent_id"),
                **kwargs.get("metadata", {}),
            },
        )

    # ---------------------------------------------------------------------- #
    # Official Hermes Tool Lifecycle Hooks
    # ---------------------------------------------------------------------- #

    def pre_tool_call(
        self,
        tool_name: str,
        args: Optional[Dict[str, Any]] = None,
        task_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
        turn_id: str = "",
        api_request_id: str = "",
        middleware_trace: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """Official Hermes pre_tool_call hook handler.

        Enforces conflict detection, admission policy, duplicate retry suppression,
        and pre-mutation witness capture. If blocking is required (e.g. CONFLICT_DETECTED
        or DUPLICATE_SUPPRESSED), returns:
            {"action": "block", "message": "Reason for block"}
        which Hermes detects and prevents tool dispatch. Otherwise returns None or {"action": "allow"}.
        """
        tool_args = args if isinstance(args, dict) else {}
        classification = self.classify_tool(tool_name)
        if classification == ToolClassification.READ_ONLY:
            return {"action": "allow"}

        ctx = self.extract_context(
            tool_name=tool_name,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            **tool_args,
            **kwargs,
        )
        logical_id = ctx.derive_logical_id()
        spec = self._tool_specs.get(tool_name)
        target_name = (spec.target if spec else None) or ctx.tool_name or tool_name

        # 1. Conflict Detection & Veto Rules
        conflict_msg = None
        if spec and spec.conflict_checker:
            conflict_msg = spec.conflict_checker(tool_name, tool_args)
        elif tool_args.get("__force_conflict") or tool_args.get("force_conflict"):
            conflict_msg = tool_args.get("__force_conflict") or tool_args.get("force_conflict")
        elif kwargs.get("__force_conflict"):
            conflict_msg = kwargs.get("__force_conflict")

        if conflict_msg:
            block_payload = {
                "action": "block",
                "message": f"CONFLICT_DETECTED: {conflict_msg}",
            }
            self._blocked_calls.append({
                "logical_id": logical_id,
                "tool_name": tool_name,
                "reason": conflict_msg,
                "timestamp": time.time(),
            })
            self.event_logger.emit(
                event_type=EventType.ERROR,
                mutation_id=logical_id,
                message=f"Hermes pre_tool_call blocked: CONFLICT_DETECTED: {conflict_msg}",
                data={"tool_name": tool_name, "args": tool_args},
            )
            return block_payload

        # 2. Duplicate Suppression & Reconciliation
        identity = MutationIdentity(
            logical_mutation_id=logical_id,
            framework=self.framework_name,
            framework_run_id=ctx.session_id or ctx.run_id or "hermes_session",
            tool_name=target_name,
            tool_call_id=ctx.tool_call_id,
            retry_attempt=ctx.retry_attempt,
            metadata=ctx.metadata,
        )

        probe = spec.post_condition_probe if spec else None
        validator = spec.post_condition_validator if spec else None

        rec_decision = self.reconciler.evaluate_request(
            identity=identity,
            current_state_probe=probe,
            expected_post_condition=validator,
        )

        if not rec_decision.should_execute_fn:
            self._suppressed_calls.append(logical_id)
            self.event_logger.emit(
                event_type=EventType.DUPLICATE_MUTATION_SUPPRESSED,
                mutation_id=logical_id,
                message=f"Hermes retry duplicate suppressed for '{logical_id}'",
                data={"cached_result": rec_decision.cached_result},
            )
            # Ensure recovery program is preserved in harness for rollback
            if self.harness:
                entry = self.reconciler.get_entry(logical_id)
                wit_val = entry.witness_data.get(target_name) if entry else None
                witness = Witness(mutation_id=logical_id, data={target_name: wit_val})
                surface = spec.surface if spec else "custom"
                inv = spec.inverse_fn if spec else None
                op = _build_hermes_recovery_op(surface, target_name, logical_id, wit_val, inv, rec_decision.cached_result)
                prog = RecoveryProgram(operations=[op])
                self.harness.record_external_protected_mutation(
                    mutation_id=logical_id,
                    description=f"Protected Hermes tool '{target_name}'",
                    witness=witness,
                    recovery_program=prog,
                    declared_effects=spec.declared_effects if spec else [
                        Effect(category=EffectCategory.RESOURCES, target=target_name, op_type=EffectOpType.UPDATE)
                    ],
                    identity=identity,
                )
            return {
                "action": "block",
                "message": f"DUPLICATE_SUPPRESSED: Mutation '{logical_id}' already executed and committed. Cached result: {rec_decision.cached_result}",
                "cached_result": rec_decision.cached_result,
            }

        # 3. Capture Pre-State Witness
        witness_val = None
        if spec and spec.capture_fn:
            try:
                witness_val = spec.capture_fn(**tool_args)
            except TypeError:
                witness_val = spec.capture_fn(tool_args)
            except Exception as e:
                self.event_logger.emit(
                    event_type=EventType.ERROR,
                    mutation_id=logical_id,
                    message=f"Failed to capture witness for {target_name}: {e}",
                )

        self.reconciler.record_witness(logical_id, {target_name: witness_val})

        # Save active mutation state for post_tool_call
        self._active_mutations[logical_id] = {
            "spec": spec,
            "context": ctx,
            "identity": identity,
            "target_name": target_name,
            "witness_val": witness_val,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "start_time": time.time(),
        }

        return {"action": "allow"}

    def post_tool_call(
        self,
        tool_name: str,
        args: Optional[Dict[str, Any]] = None,
        result: Any = None,
        task_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
        turn_id: str = "",
        api_request_id: str = "",
        status: str = "ok",
        duration_ms: float = 0.0,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """Official Hermes post_tool_call hook handler.

        Updates journal upon execution, registers recovery programs with harness,
        and transitions mutation to COMMITTED.
        """
        tool_args = args if isinstance(args, dict) else {}
        ctx = self.extract_context(
            tool_name=tool_name,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            **tool_args,
            **kwargs,
        )
        logical_id = ctx.derive_logical_id()

        if status in ("blocked", "cancelled"):
            self.reconciler.record_failed(logical_id, f"Tool {status}: {error_message or error_type or 'blocked'}")
            self._active_mutations.pop(logical_id, None)
            return {"action": "ack", "status": status}

        if status == "error":
            self.reconciler.record_failed(logical_id, f"Tool execution failed: {error_message or error_type or str(result)}")
            self._active_mutations.pop(logical_id, None)
            return {"action": "ack", "status": "error"}

        active = self._active_mutations.pop(logical_id, None)
        spec = (active.get("spec") if active else None) or self._tool_specs.get(tool_name)
        target_name = (active.get("target_name") if active else None) or (spec.target if spec else None) or tool_name
        witness_val = active.get("witness_val") if active else None
        identity = (active.get("identity") if active else None) or MutationIdentity(
            logical_mutation_id=logical_id,
            framework=self.framework_name,
            framework_run_id=ctx.session_id or ctx.run_id or "hermes_session",
            tool_name=target_name,
            tool_call_id=ctx.tool_call_id,
            retry_attempt=ctx.retry_attempt,
            metadata=ctx.metadata,
        )

        declared_effects = (spec.declared_effects if spec else None) or [
            Effect(category=EffectCategory.RESOURCES, target=target_name, op_type=EffectOpType.UPDATE)
        ]

        # Record mutation execution
        self.reconciler.record_mutation_executed(
            logical_mutation_id=logical_id,
            result=result,
            effects=[e.to_dict() for e in declared_effects],
        )

        # Register recovery program in harness
        if self.harness:
            witness = Witness(mutation_id=logical_id, data={target_name: witness_val})
            surface = spec.surface if spec else "custom"
            inv = spec.inverse_fn if spec else None
            op = _build_hermes_recovery_op(surface, target_name, logical_id, witness_val, inv, result)
            prog = RecoveryProgram(operations=[op])
            self.harness.record_external_protected_mutation(
                mutation_id=logical_id,
                description=f"Protected Hermes tool '{target_name}'",
                witness=witness,
                recovery_program=prog,
                declared_effects=declared_effects,
                identity=identity,
            )

        # Record committed
        self.reconciler.record_committed(logical_id)
        return {"action": "ack", "mutation_id": logical_id, "status": "committed"}

    # ---------------------------------------------------------------------- #
    # Plugin & Shell Hook Dispatch Helpers
    # ---------------------------------------------------------------------- #

    def on_pre_tool_call(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Direct callback for Hermes invoke_hook('pre_tool_call', **kwargs)."""
        kw = dict(kwargs)
        tool_name = kw.pop("tool_name", "")
        args = kw.pop("args", None) or kw.pop("tool_input", None)
        task_id = kw.pop("task_id", "")
        session_id = kw.pop("session_id", "")
        tool_call_id = kw.pop("tool_call_id", "")
        turn_id = kw.pop("turn_id", "")
        api_request_id = kw.pop("api_request_id", "")
        middleware_trace = kw.pop("middleware_trace", None)
        return self.pre_tool_call(
            tool_name=tool_name,
            args=args,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            middleware_trace=middleware_trace,
            **kw,
        )

    def on_post_tool_call(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Direct callback for Hermes invoke_hook('post_tool_call', **kwargs)."""
        kw = dict(kwargs)
        tool_name = kw.pop("tool_name", "")
        args = kw.pop("args", None) or kw.pop("tool_input", None)
        result = kw.pop("result", None)
        task_id = kw.pop("task_id", "")
        session_id = kw.pop("session_id", "")
        tool_call_id = kw.pop("tool_call_id", "")
        turn_id = kw.pop("turn_id", "")
        api_request_id = kw.pop("api_request_id", "")
        status = kw.pop("status", "ok")
        duration_ms = float(kw.pop("duration_ms", 0.0))
        error_type = kw.pop("error_type", None)
        error_message = kw.pop("error_message", None)
        return self.post_tool_call(
            tool_name=tool_name,
            args=args,
            result=result,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            status=status,
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
            **kw,
        )

    def register_with_hermes(self, ctx_or_manager: Any = None) -> None:
        """Register lifecycle hooks into Hermes plugin manager or PluginContext."""
        target = ctx_or_manager
        if target is None:
            try:
                from hermes_cli.plugins import get_plugin_manager
                target = get_plugin_manager()
            except ImportError:
                logger.warning("hermes_cli.plugins not available to register hooks")
                return

        if hasattr(target, "register_hook"):
            target.register_hook("pre_tool_call", self.on_pre_tool_call)
            target.register_hook("post_tool_call", self.on_post_tool_call)
        elif hasattr(target, "_hooks"):
            target._hooks.setdefault("pre_tool_call", []).append(self.on_pre_tool_call)
            target._hooks.setdefault("post_tool_call", []).append(self.on_post_tool_call)
        logger.info("Registered EvoUndo HermesAdapter hooks with Hermes runtime")

    def handle_shell_hook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle stdin JSON payload conforming to Hermes agent/shell_hooks.py wire protocol."""
        event = payload.get("hook_event_name", "")
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})
        session_id = payload.get("session_id", "")
        extra = dict(payload.get("extra", {}) or {})

        if event == "pre_tool_call":
            task_id = extra.pop("task_id", "")
            tool_call_id = extra.pop("tool_call_id", "")
            turn_id = extra.pop("turn_id", "")
            api_request_id = extra.pop("api_request_id", "")
            resp = self.pre_tool_call(
                tool_name=tool_name,
                args=tool_input,
                session_id=session_id,
                task_id=task_id,
                tool_call_id=tool_call_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                **extra,
            )
            return resp or {}
        elif event == "post_tool_call":
            result = extra.pop("result", None)
            task_id = extra.pop("task_id", "")
            tool_call_id = extra.pop("tool_call_id", "")
            turn_id = extra.pop("turn_id", "")
            api_request_id = extra.pop("api_request_id", "")
            status = extra.pop("status", "ok")
            duration_ms = float(extra.pop("duration_ms", 0.0))
            error_type = extra.pop("error_type", None)
            error_message = extra.pop("error_message", None)
            resp = self.post_tool_call(
                tool_name=tool_name,
                args=tool_input,
                result=result,
                session_id=session_id,
                task_id=task_id,
                tool_call_id=tool_call_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                status=status,
                duration_ms=duration_ms,
                error_type=error_type,
                error_message=error_message,
                **extra,
            )
            return resp or {}
        return {}

    # ---------------------------------------------------------------------- #
    # Backward-Compatible Decorator Method
    # ---------------------------------------------------------------------- #

    def create_tool_hook(
        self,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Wrap a Hermes tool function with execution context and recovery guarantees.

        Maintains 100% backward compatibility with existing tests and integrations.
        """
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = target or getattr(fn, "__name__", "hermes_tool")

            # Also register spec in adapter for lifecycle hooks
            self.register_tool_spec(
                tool_name=tool_name,
                surface=surface,
                target=tool_name,
                declared_effects=declared_effects,
                capture_fn=capture_fn,
                inverse_fn=inverse_fn,
                post_condition_probe=post_condition_probe,
                post_condition_validator=post_condition_validator,
            )

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                hermes_meta: Dict[str, Any] = kwargs.pop("__hermes_context", {}) or {}
                ctx = FrameworkContext(
                    framework_name=self.framework_name,
                    session_id=hermes_meta.get("session_id", kwargs.pop("session_id", None)),
                    run_id=hermes_meta.get("task_id", kwargs.pop("task_id", None)),
                    agent_id=hermes_meta.get("agent_id", kwargs.pop("agent_id", "hermes_agent")),
                    tool_name=tool_name,
                    tool_call_id=hermes_meta.get("call_id", kwargs.pop("call_id", None)),
                    logical_mutation_id=hermes_meta.get("logical_mutation_id", kwargs.pop("__logical_mutation_id", None)),
                    retry_attempt=int(hermes_meta.get("retry_attempt", kwargs.pop("retry_attempt", 0))),
                    metadata={
                        "subagent_id": hermes_meta.get("subagent_id"),
                        "cron_task": hermes_meta.get("is_cron", False),
                        **hermes_meta.get("metadata", {}),
                    },
                )
                return self.execute_protected_tool(
                    tool_fn=fn,
                    context=ctx,
                    tool_args=args,
                    tool_kwargs=kwargs,
                    surface=surface,
                    target=tool_name,
                    declared_effects=declared_effects or [
                        Effect(category=EffectCategory.RESOURCES, target=tool_name, op_type=EffectOpType.UPDATE)
                    ],
                    capture_fn=capture_fn,
                    inverse_fn=inverse_fn,
                    post_condition_probe=post_condition_probe,
                    post_condition_validator=post_condition_validator,
                )

            setattr(wrapper, "_is_evoundo_protected", True)
            setattr(wrapper, "_evoundo_adapter", self)
            return wrapper

        return decorator


# ---------------------------------------------------------------------- #
# Hermes Plugin Module Surface
# ---------------------------------------------------------------------- #

_global_hermes_adapter: Optional[HermesAdapter] = None


def get_default_adapter() -> HermesAdapter:
    global _global_hermes_adapter
    if _global_hermes_adapter is None:
        _global_hermes_adapter = HermesAdapter()
    return _global_hermes_adapter


def register(ctx: Any) -> None:
    """Plugin registration entry point for Hermes Agent."""
    adapter = get_default_adapter()
    adapter.register_with_hermes(ctx)


def on_pre_tool_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Global hook handler for pre_tool_call."""
    return get_default_adapter().on_pre_tool_call(**kwargs)


def on_post_tool_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Global hook handler for post_tool_call."""
    return get_default_adapter().on_post_tool_call(**kwargs)


if __name__ == "__main__":
    raw_input = sys.stdin.read()
    if raw_input.strip():
        try:
            in_payload = json.loads(raw_input)
            out_resp = get_default_adapter().handle_shell_hook(in_payload)
            if out_resp:
                sys.stdout.write(json.dumps(out_resp) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            logger.error("Hermes shell hook error: %s", exc)
