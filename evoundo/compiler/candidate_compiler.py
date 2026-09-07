"""Candidate compiler for translating declarative specs and LLM outputs into executable proposals."""

from __future__ import annotations
import copy
import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.compiler.schemas import DeclarativeMutationSpec, OpSpec
from evoundo.core.mutation import MutationProposal, ProposalStatus
from evoundo.core.state import (
    FileDescriptor,
    HarnessState,
    ListenerDescriptor,
    MiddlewareDescriptor,
    ResourceDescriptor,
    ToolDescriptor,
)
from evoundo.effects.contracts import EffectCategory, EffectContract, EquivalencePolicy
from evoundo.recovery.operations import (
    BaseRecoveryOp,
    CloseResourceOp,
    DeleteCreatedFileOp,
    RecoveryProgram,
    RemoveConfigOp,
    RemoveListenerOp,
    RemoveMiddlewareOp,
    RemoveToolOp,
    RestoreConfigOp,
    RestoreFileOp,
    RestoreListenerOp,
    RestoreMiddlewareOp,
    RestorePromptOp,
    RestoreResourceOp,
    RestoreToolOp,
)


class CandidateCompiler:
    """Compiles declarative specifications and raw text/JSON into verified MutationProposal objects."""

    # Primitives supported at L0 (Scalar configuration & Tool registry)
    L0_PRIMITIVES = {
        "set_config", "delete_config", "restore_config", "remove_config",
        "register_tool", "remove_tool", "restore_tool",
        "set_prompt", "restore_prompt",
    }

    # Extended primitives requiring L1
    L1_PRIMITIVES = {
        "add_middleware", "remove_middleware", "restore_middleware",
        "add_listener", "remove_listener", "restore_listener",
        "write_file", "delete_file", "restore_file", "delete_created_file",
        "register_resource", "close_resource", "restore_resource",
    }

    @classmethod
    def parse_json_from_text(cls, text: str) -> Optional[Dict[str, Any]]:
        """Extract and parse JSON dictionary from LLM textual responses."""
        if not text:
            return None
        text_clean = text.strip()
        if text_clean.startswith("{") and text_clean.endswith("}"):
            try:
                return json.loads(text_clean)
            except Exception:
                pass
        m_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m_block:
            try:
                return json.loads(m_block.group(1))
            except Exception:
                pass
        m_brace = re.search(r"(\{.*\})", text, re.DOTALL)
        if m_brace:
            try:
                return json.loads(m_brace.group(1))
            except Exception:
                pass
        return None

    @classmethod
    def compile_spec(
        cls,
        spec: Union[DeclarativeMutationSpec, Dict[str, Any]],
        recovery_language: str = "L1",
    ) -> Tuple[Optional[MutationProposal], Optional[str]]:
        """Compile a declarative spec into an executable MutationProposal with language enforcement.

        Returns:
            (proposal, error_message)
        """
        try:
            if isinstance(spec, dict):
                spec_obj = cls._dict_to_spec(spec)
            else:
                spec_obj = spec

            lang = (recovery_language or spec_obj.recovery_language).upper()
            all_known = cls.L0_PRIMITIVES | cls.L1_PRIMITIVES

            # 1. Enforce Valid Operation Types
            for op in spec_obj.forward_ops + spec_obj.recovery_ops:
                op_type = op.op_type.lower()
                if op_type not in all_known:
                    return None, f"INVALID_OP_TYPE: Unrecognized operation '{op.op_type}'. Must be one of {sorted(list(all_known))}."

            # 2. Enforce Language Boundaries
            if lang == "L0":
                for op in spec_obj.forward_ops + spec_obj.recovery_ops:
                    op_type = op.op_type.lower()
                    if op_type in cls.L1_PRIMITIVES or (op.surface and op.surface.lower() in ("middleware", "files", "resources", "listeners")):
                        return None, (
                            f"LANGUAGE_GATE_REJECTION: Primitive '{op_type}' (surface '{op.surface}') "
                            f"requires L1 expressivity, but requested recovery language is L0."
                        )

            # 2. Build Effect Contract
            contract = EffectContract()
            for cat_name, targets in spec_obj.declared_effects.items():
                cat_enum = None
                try:
                    cat_enum = EffectCategory(cat_name.lower())
                except Exception:
                    pass
                if cat_enum:
                    for t in targets:
                        contract.declare(cat_enum, t)

            # Auto-infer undeclared targets from forward_ops if contract empty
            for op in spec_obj.forward_ops:
                op_t = op.op_type.lower()
                surf = (op.surface or "").lower()
                if "config" in op_t or surf == "config":
                    contract.declare(EffectCategory.CONFIG, op.target)
                elif "tool" in op_t or surf == "tools":
                    contract.declare(EffectCategory.TOOLS, op.target)
                elif "middleware" in op_t or surf == "middleware":
                    contract.declare(EffectCategory.MIDDLEWARE, op.target)
                elif "listener" in op_t or surf == "listeners":
                    contract.declare(EffectCategory.EVENT_LISTENERS, op.target)
                elif "file" in op_t or surf == "files":
                    contract.declare(EffectCategory.FILES, op.target)
                elif "resource" in op_t or surf == "resources":
                    contract.declare(EffectCategory.RESOURCES, op.target)
                elif "prompt" in op_t or surf == "prompts":
                    contract.declare(EffectCategory.PROMPTS, op.target)

            if contract.tools:
                contract.set_policy(EffectCategory.TOOLS, EquivalencePolicy.ORDER_INSENSITIVE)
            if contract.middleware:
                contract.set_policy(EffectCategory.MIDDLEWARE, EquivalencePolicy.ORDER_INSENSITIVE)

            # 3. Compile Forward Mutation Function
            forward_ops = list(spec_obj.forward_ops)
            def forward_fn(state: HarnessState) -> None:
                for op in forward_ops:
                    cls._execute_forward_op(state, op)

            # 4. Synthesize or Compile Recovery Program
            recovery_prog = RecoveryProgram.synthesize_from_contract(contract)

            # 5. Build Final Proposal
            proposal = MutationProposal(
                mutation_id=spec_obj.mutation_id or f"mut_{uuid.uuid4().hex[:8]}",
                description=spec_obj.description,
                forward_mutation=forward_fn,
                recovery_program=recovery_prog,
                effect_contract=contract,
                metadata={
                    "recovery_language": lang,
                    "complexity_level": spec_obj.complexity_level,
                    "expected_capability_delta": spec_obj.expected_capability_delta,
                },
            )
            return proposal, None

        except Exception as e:
            return None, f"Compilation error: {str(e)}"

    @classmethod
    def _execute_forward_op(cls, state: HarnessState, op: OpSpec) -> None:
        op_t = op.op_type.lower()
        if op_t == "set_config":
            state.set_config(op.target, op.value)
        elif op_t == "delete_config":
            state.delete_config(op.target)
        elif op_t == "register_tool":
            if isinstance(op.value, ToolDescriptor):
                state.register_tool(op.value)
            elif isinstance(op.value, dict):
                state.register_tool(ToolDescriptor(
                    name=op.target or op.value.get("name", "unnamed"),
                    description=op.value.get("description", ""),
                    fn=op.value.get("fn", lambda *a, **kw: None),
                    parameters_schema=op.value.get("parameters_schema", {}),
                    version=op.value.get("version", "1.0.0"),
                ))
            else:
                state.register_tool(ToolDescriptor(
                    name=op.target,
                    description=str(op.value or ""),
                    fn=lambda *a, **kw: op.value,
                ))
        elif op_t == "remove_tool":
            state.remove_tool(op.target)
        elif op_t == "add_middleware":
            if isinstance(op.value, MiddlewareDescriptor):
                state.add_middleware(op.value)
            elif isinstance(op.value, dict):
                state.add_middleware(MiddlewareDescriptor(
                    id=op.target or op.value.get("id", f"mw_{uuid.uuid4().hex[:6]}"),
                    name=op.value.get("name", op.target),
                    priority=op.value.get("priority", 100),
                    fn=op.value.get("fn", lambda ctx: ctx),
                    enabled=op.value.get("enabled", True),
                ))
            else:
                state.add_middleware(MiddlewareDescriptor(
                    id=op.target,
                    name=op.target,
                    priority=100,
                    fn=lambda ctx: ctx,
                ))
        elif op_t == "remove_middleware":
            state.remove_middleware(op.target)
        elif op_t == "add_listener":
            evt = op.target
            lid = op.metadata.get("listener_id", f"list_{uuid.uuid4().hex[:6]}")
            if ":" in evt:
                evt, lid = evt.split(":", 1)
            cb = op.value if callable(op.value) else (lambda *a, **kw: None)
            state.add_listener(evt, ListenerDescriptor(id=lid, event=evt, callback=cb))
        elif op_t == "remove_listener":
            evt = op.target
            lid = op.metadata.get("listener_id", "")
            if ":" in evt:
                evt, lid = evt.split(":", 1)
            state.remove_listener(evt, lid)
        elif op_t == "write_file":
            content = op.value if isinstance(op.value, str) else json.dumps(op.value)
            state.write_file(op.target, content)
        elif op_t == "delete_file":
            state.delete_file(op.target)
        elif op_t == "register_resource":
            if isinstance(op.value, ResourceDescriptor):
                state.register_resource(op.value)
            else:
                state.register_resource(ResourceDescriptor(
                    id=op.target,
                    resource_type=op.metadata.get("resource_type", "socket"),
                    descriptor={"val": op.value},
                ))
        elif op_t == "close_resource":
            state.close_resource(op.target)
        elif op_t == "set_prompt":
            state.set_prompt(op.target, str(op.value or ""))

    @classmethod
    def _dict_to_spec(cls, d: Dict[str, Any]) -> DeclarativeMutationSpec:
        cid = d.get("mutation_id") or d.get("candidate_id") or f"mut_{uuid.uuid4().hex[:8]}"
        desc = d.get("description") or d.get("rationale") or ""
        comp = d.get("complexity_level", 1)
        lang = d.get("recovery_language", "L1")

        def _to_op(item: Any) -> OpSpec:
            if isinstance(item, OpSpec):
                return item
            if isinstance(item, dict):
                return OpSpec(
                    op_type=item.get("op_type") or item.get("action") or "unknown",
                    target=item.get("target") or item.get("key") or item.get("name") or "unknown",
                    value=item.get("value"),
                    witness_key=item.get("witness_key"),
                    surface=item.get("surface"),
                    metadata=item.get("metadata", {}),
                )
            return OpSpec(op_type="unknown", target=str(item))

        raw_f_ops = d.get("forward_ops") or d.get("operations") or []
        f_ops = [_to_op(x) for x in raw_f_ops]
        c_ops = [_to_op(x) for x in d.get("capture_ops", [])]
        r_ops = [_to_op(x) for x in d.get("recovery_ops", [])]

        decl = d.get("declared_effects", {})
        if isinstance(decl, list):
            decl_dict = {}
            for item in decl:
                if "[" in item and "]" in item:
                    parts = item.split("]", 1)
                    cat = parts[0].replace("[", "").strip()
                    tgt = parts[1].strip()
                    decl_dict.setdefault(cat, []).append(tgt)
            decl = decl_dict

        return DeclarativeMutationSpec(
            mutation_id=cid,
            description=desc,
            complexity_level=comp,
            capture_ops=c_ops,
            forward_ops=f_ops,
            recovery_ops=r_ops,
            declared_effects=decl,
            expected_capability_delta=d.get("expected_capability_delta", 0.0),
            recovery_language=lang,
            metadata=d.get("metadata", {}),
        )
