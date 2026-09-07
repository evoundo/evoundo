"""Agent runtime utilizing EvoUndo Harness for execution and self-evolution."""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
from evoundo.admission.policies import AdmissionDecision, CapabilityResult
from evoundo.core.harness import EvoUndoHarness
from evoundo.core.mutation import MutationBuilder
from evoundo.core.state import HarnessState


class EvoAgent:
    """Agent that executes tasks using harness components and self-evolves safely."""

    def __init__(self, name: str = "EvoAgent", harness: Optional[EvoUndoHarness] = None):
        self.name = name
        self.harness = harness or EvoUndoHarness()

    def run(self, action: str, **kwargs: Any) -> Any:
        """Run an action through the harness middleware pipeline and tool registry."""
        context: Dict[str, Any] = {"action": action, "args": kwargs, "agent": self.name}

        # 1. Execute middleware pipeline in priority order
        for middleware in self.harness.current_state.middleware:
            if middleware.enabled:
                context = middleware.process(context)

        # 2. Check if a tool matches the requested action or routing
        tool = self.harness.current_state.get_tool(action)
        if tool:
            return tool.execute(**kwargs)

        # 3. Check default routing config
        default_router = self.harness.current_state.get_config("tool_routing", {})
        if action in default_router:
            routed_tool_name = default_router[action]
            routed_tool = self.harness.current_state.get_tool(routed_tool_name)
            if routed_tool:
                return routed_tool.execute(**kwargs)

        raise ValueError(f"No tool or routing handler available for action: '{action}'")

    def self_evolve(
        self,
        description: str,
        mutate_fn: Callable[[MutationBuilder], None],
        capability_evaluator: Optional[Callable[[HarnessState], CapabilityResult]] = None,
    ) -> AdmissionDecision:
        """Propose, evaluate, and admit a self-evolution mutation."""
        with self.harness.mutation(description=description, proposer=self.name) as candidate:
            mutate_fn(candidate)

        decision = self.harness.admit(candidate, capability_evaluator=capability_evaluator)
        return decision

    def revert_evolution(self, mutation_id: str, reason: str = "") -> HarnessState:
        """Revert a previously admitted self-evolution."""
        return self.harness.revert(mutation_id, reason=reason)
