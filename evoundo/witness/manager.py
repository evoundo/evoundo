"""Witness manager capturing localized pre-mutation state slices."""

from __future__ import annotations
import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from evoundo.core.state import HarnessState
from evoundo.effects.contracts import EffectCategory, EffectContract
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger
from evoundo.witness.stores import BaseWitnessStore, InMemoryWitnessStore, Witness


class WitnessCaptureError(RuntimeError):
    """Raised when pre-mutation witness capture fails, blocking execution to maintain recoverability."""
    pass


@dataclass
class WitnessSchema:
    """Specification of required and optional keys in a witness."""
    name: str = "default_schema"
    required_keys: List[str] = field(default_factory=list)
    description: str = ""

    def validate(self, witness: Witness) -> Tuple[bool, List[str]]:
        missing = [k for k in self.required_keys if k not in witness.data]
        if missing:
            return False, [f"Missing required witness key: {k}" for k in missing]
        return True, []


class WitnessManager:
    """Captures and validates minimal state witnesses before mutations execute."""

    def __init__(
        self,
        store: Optional[BaseWitnessStore] = None,
        event_logger: Optional[StructuredEventLogger] = None,
    ):
        self.store = store or InMemoryWitnessStore()
        self.event_logger = event_logger or default_event_logger

    def capture_for_contract(
        self,
        state: HarnessState,
        contract: EffectContract,
        mutation_id: str,
        schema: Optional[WitnessSchema] = None,
    ) -> Witness:
        """Capture only the pre-state elements declared in the effect contract."""
        data: Dict[str, Any] = {}

        # 1. Config surface
        for key in contract.config:
            existed = key in state.config
            data[f"config:{key}:existed"] = existed
            if existed:
                data[f"config:{key}:value"] = copy.deepcopy(state.config[key])

        # 2. Tools surface
        for tool_name in contract.tools:
            tool = state.get_tool(tool_name)
            existed = tool is not None
            data[f"tools:{tool_name}:existed"] = existed
            if tool:
                data[f"tools:{tool_name}:descriptor"] = copy.deepcopy(tool)

        # 3. Middleware surface
        for mid in contract.middleware:
            m = state.get_middleware(mid)
            existed = m is not None
            data[f"middleware:{mid}:existed"] = existed
            if m:
                data[f"middleware:{mid}:descriptor"] = copy.deepcopy(m)
                # record original index and priority
                for idx, item in enumerate(state.middleware):
                    if item.id == mid:
                        data[f"middleware:{mid}:index"] = idx
                        break

        # 4. Event listeners surface
        for target in contract.event_listeners:
            # target format: "event_name:listener_id" or "event_name"
            if ":" in target:
                event, lid = target.split(":", 1)
                listeners = state.event_listeners.get(event, [])
                matching = [l for l in listeners if l.id == lid]
                existed = len(matching) > 0
                data[f"listener:{event}:{lid}:existed"] = existed
                if existed:
                    data[f"listener:{event}:{lid}:descriptor"] = copy.deepcopy(matching[0])
            else:
                event = target
                existed = event in state.event_listeners
                data[f"listener:{event}:existed"] = existed
                if existed:
                    data[f"listener:{event}:descriptors"] = copy.deepcopy(state.event_listeners[event])

        # 5. Files surface
        for path in contract.files:
            content = state.read_file(path)
            existed = content is not None
            data[f"files:{path}:existed"] = existed
            if existed:
                data[f"files:{path}:content"] = content
                f_desc = state.files.get(path)
                if f_desc:
                    data[f"files:{path}:mode"] = f_desc.mode

        # 6. Resources surface
        for rid in contract.resources:
            r = state.resources.get(rid)
            existed = r is not None
            data[f"resources:{rid}:existed"] = existed
            if r:
                data[f"resources:{rid}:descriptor"] = copy.deepcopy(r)

        # 7. Prompts surface
        for p_name in contract.prompts:
            p_val = state.get_prompt(p_name)
            existed = p_val is not None
            data[f"prompts:{p_name}:existed"] = existed
            if existed:
                data[f"prompts:{p_name}:value"] = p_val

        witness = Witness(
            mutation_id=mutation_id,
            data=data,
            schema_name=schema.name if schema else None,
        )

        # Validate schema if provided
        if schema:
            valid, errs = schema.validate(witness)
            if not valid:
                raise ValueError(f"Witness validation failed against schema '{schema.name}': {', '.join(errs)}")

        self.store.save_witness(witness)

        self.event_logger.emit(
            event_type=EventType.WITNESS_CAPTURED,
            mutation_id=mutation_id,
            message=f"Captured witness with {len(data)} items for contract",
            data={"keys_captured": list(data.keys()), "schema": schema.name if schema else None},
        )

        return witness

    def save_witness(self, witness: Witness) -> None:
        self.store.save_witness(witness)

    def lookup(self, mutation_id: str, tenant_id: Optional[str] = None) -> Optional[Witness]:
        witness = self.store.get_witness(mutation_id)
        if witness is None:
            return None
        w_tenant = getattr(witness, "tenant_id", "default")
        if tenant_id is not None and tenant_id != w_tenant:
            from evoundo.governance import TenantAccessDeniedError
            raise TenantAccessDeniedError(
                f"Multi-Tenancy Violation: Requesting tenant '{tenant_id}' denied access to witness "
                f"for mutation '{mutation_id}' belonging to tenant '{w_tenant}'"
            )
        return witness

    def get_witness(self, mutation_id: str, tenant_id: Optional[str] = None) -> Optional[Witness]:
        return self.lookup(mutation_id, tenant_id=tenant_id)

    def delete_witness(self, mutation_id: str, tenant_id: Optional[str] = None) -> bool:
        w = self.lookup(mutation_id, tenant_id=tenant_id)
        if w is None:
            return False
        return self.store.delete_witness(mutation_id)
