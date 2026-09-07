"""Mutation proposal models, builders, and fluent context managers."""

from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING
from evoundo.core.state import (
    FileDescriptor,
    HarnessState,
    ListenerDescriptor,
    MiddlewareDescriptor,
    ResourceDescriptor,
    ToolDescriptor,
)
from evoundo.effects.contracts import EffectCategory, EffectContract
from evoundo.recovery.operations import RecoveryProgram
from evoundo.witness.manager import WitnessSchema
from evoundo.witness.stores import Witness

if TYPE_CHECKING:
    from evoundo.core.harness import EvoUndoHarness


class ProposalStatus(str, Enum):
    """Lifecycle status of a mutation proposal."""
    PENDING = "PENDING"
    EVALUATING = "EVALUATING"
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"
    REVERTED = "REVERTED"


@dataclass
class MutationProposal:
    """Strongly-typed proposal for agent self-evolution."""
    mutation_id: str = field(default_factory=lambda: f"mut_{uuid.uuid4().hex[:8]}")
    description: str = ""
    forward_mutation: Optional[Callable[[HarnessState], Any]] = None
    witness_spec: Optional[WitnessSchema] = None
    recovery_program: Optional[RecoveryProgram] = None
    effect_contract: EffectContract = field(default_factory=EffectContract)
    proposer: str = "agent"
    timestamp: float = field(default_factory=time.time)
    status: ProposalStatus = ProposalStatus.PENDING
    metadata: Dict[str, Any] = field(default_factory=dict)


class MutationBuilder:
    """Fluent context manager for proposing and staging self-mutations.
    
    Example:
        with harness.mutation(description="Add search tool") as candidate:
            candidate.register_tool(search_tool)
            candidate.set_config("search_limit", 10)
    """

    def __init__(
        self,
        harness: Optional[EvoUndoHarness] = None,
        description: str = "",
        mutation_id: Optional[str] = None,
        proposer: str = "agent",
    ):
        self.harness = harness
        self.mutation_id = mutation_id or f"mut_{uuid.uuid4().hex[:8]}"
        self.description = description
        self.proposer = proposer
        self.effect_contract = EffectContract()
        self._staged_operations: List[Callable[[HarnessState], None]] = []
        self._proposal: Optional[MutationProposal] = None

    def __enter__(self) -> MutationBuilder:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None:
            self.build_proposal()

    # --- Fluent Mutation Surface APIs ---

    def set_config(self, key: str, value: Any) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.CONFIG, key)
        self._staged_operations.append(lambda s: s.set_config(key, value))
        return self

    def delete_config(self, key: str) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.CONFIG, key)
        self._staged_operations.append(lambda s: s.delete_config(key))
        return self

    def register_tool(self, tool: ToolDescriptor) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.TOOLS, tool.name)
        self._staged_operations.append(lambda s: s.register_tool(tool))
        return self

    def remove_tool(self, tool_name: str) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.TOOLS, tool_name)
        self._staged_operations.append(lambda s: s.remove_tool(tool_name))
        return self

    def add_middleware(self, middleware: MiddlewareDescriptor) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.MIDDLEWARE, middleware.id)
        self._staged_operations.append(lambda s: s.add_middleware(middleware))
        return self

    def remove_middleware(self, middleware_id: str) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.MIDDLEWARE, middleware_id)
        self._staged_operations.append(lambda s: s.remove_middleware(middleware_id))
        return self

    def add_listener(self, event: str, listener: ListenerDescriptor) -> MutationBuilder:
        target = f"{event}:{listener.id}"
        self.effect_contract.declare(EffectCategory.EVENT_LISTENERS, target)
        self._staged_operations.append(lambda s: s.add_listener(event, listener))
        return self

    def remove_listener(self, event: str, listener_id: str) -> MutationBuilder:
        target = f"{event}:{listener_id}"
        self.effect_contract.declare(EffectCategory.EVENT_LISTENERS, target)
        self._staged_operations.append(lambda s: s.remove_listener(event, listener_id))
        return self

    def write_file(self, path: str, content: str, mode: str = "text") -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.FILES, path)
        self._staged_operations.append(lambda s: s.write_file(path, content, mode=mode))
        return self

    def delete_file(self, path: str) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.FILES, path)
        self._staged_operations.append(lambda s: s.delete_file(path))
        return self

    def register_resource(self, resource: ResourceDescriptor) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.RESOURCES, resource.id)
        self._staged_operations.append(lambda s: s.register_resource(resource))
        return self

    def close_resource(self, resource_id: str) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.RESOURCES, resource_id)
        self._staged_operations.append(lambda s: s.close_resource(resource_id))
        return self

    def set_prompt(self, name: str, template: str) -> MutationBuilder:
        self.effect_contract.declare(EffectCategory.PROMPTS, name)
        self._staged_operations.append(lambda s: s.set_prompt(name, template))
        return self

    def build_proposal(self) -> MutationProposal:
        """Construct the complete MutationProposal from staged actions and contract."""
        ops = list(self._staged_operations)

        def forward_fn(state: HarnessState) -> None:
            for op in ops:
                op(state)

        recovery_prog = RecoveryProgram.synthesize_from_contract(self.effect_contract)

        self._proposal = MutationProposal(
            mutation_id=self.mutation_id,
            description=self.description,
            forward_mutation=forward_fn,
            witness_spec=None,
            recovery_program=recovery_prog,
            effect_contract=self.effect_contract,
            proposer=self.proposer,
        )
        return self._proposal

    @property
    def proposal(self) -> MutationProposal:
        if self._proposal is None:
            return self.build_proposal()
        return self._proposal
