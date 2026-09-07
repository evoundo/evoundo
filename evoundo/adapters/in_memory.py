"""In-memory reference adapter for EvoUndo Harness."""

from __future__ import annotations
import copy
from typing import Any, Callable, Dict, List, Optional
from evoundo.adapters.base import HarnessAdapter
from evoundo.admission.policies import CapabilityResult
from evoundo.core.state import HarnessState
from evoundo.effects.contracts import Effect, EffectCategory
from evoundo.effects.diff import StateDiffer


class InMemoryHarnessAdapter(HarnessAdapter):
    """Standard in-memory harness adapter for local state management."""

    def __init__(self, initial_state: Optional[HarnessState] = None):
        self._state = initial_state or HarnessState()

    def get_state(self) -> HarnessState:
        return self._state.clone()

    def apply_mutation(self, mutation_fn: Callable[[HarnessState], None]) -> None:
        mutation_fn(self._state)

    def capture_surface(self, category: EffectCategory, target: str) -> Any:
        if category == EffectCategory.CONFIG:
            return copy.deepcopy(self._state.config.get(target))
        elif category == EffectCategory.TOOLS:
            return copy.deepcopy(self._state.tools.get(target))
        elif category == EffectCategory.MIDDLEWARE:
            return copy.deepcopy(self._state.get_middleware(target))
        elif category == EffectCategory.FILES:
            return copy.deepcopy(self._state.files.get(target))
        elif category == EffectCategory.RESOURCES:
            return copy.deepcopy(self._state.resources.get(target))
        elif category == EffectCategory.PROMPTS:
            return copy.deepcopy(self._state.prompts.get(target))
        return None

    def restore_surface(self, category: EffectCategory, target: str, value: Any) -> None:
        if category == EffectCategory.CONFIG:
            if value is not None:
                self._state.set_config(target, value)
            else:
                self._state.delete_config(target)
        elif category == EffectCategory.TOOLS:
            if value is not None:
                self._state.register_tool(value)
            else:
                self._state.remove_tool(target)
        elif category == EffectCategory.MIDDLEWARE:
            if value is not None:
                self._state.add_middleware(value)
            else:
                self._state.remove_middleware(target)
        elif category == EffectCategory.FILES:
            if value is not None:
                self._state.write_file(target, value.content if hasattr(value, "content") else str(value))
            else:
                self._state.delete_file(target)
        elif category == EffectCategory.RESOURCES:
            if value is not None:
                self._state.register_resource(value)
            else:
                self._state.close_resource(target)

    def observe_effects(self, pre_state: HarnessState, post_state: HarnessState) -> List[Effect]:
        return StateDiffer.diff(pre_state, post_state)

    def evaluate_capability(
        self,
        state: HarnessState,
        probe_tasks: Optional[List[Any]] = None,
    ) -> CapabilityResult:
        # Default probe capability evaluation
        score = 1.0 if len(state.tools) > 0 or len(state.config) > 0 else 0.5
        return CapabilityResult(
            improved=True,
            score_before=1.0,
            score_after=score,
            delta=0.0,
        )
