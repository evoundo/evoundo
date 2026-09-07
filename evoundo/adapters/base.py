"""Abstract base adapter decoupling EvoUndo control plane from agent framework implementations."""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional
from evoundo.admission.policies import CapabilityResult
from evoundo.core.state import HarnessState
from evoundo.effects.contracts import Effect, EffectCategory


class HarnessAdapter(ABC):
    """Abstract adapter defining standard state, mutation, effect, and evaluation operations."""

    @abstractmethod
    def get_state(self) -> HarnessState:
        """Extract strongly-typed HarnessState from the underlying agent environment."""
        pass

    @abstractmethod
    def apply_mutation(self, mutation_fn: Callable[[HarnessState], None]) -> None:
        """Apply a mutation function to the agent runtime state."""
        pass

    @abstractmethod
    def capture_surface(self, category: EffectCategory, target: str) -> Any:
        """Capture recovery-relevant pre-state for a specific target on a state surface."""
        pass

    @abstractmethod
    def restore_surface(self, category: EffectCategory, target: str, value: Any) -> None:
        """Restore a specific target on a state surface using pre-state witness value."""
        pass

    @abstractmethod
    def observe_effects(self, pre_state: HarnessState, post_state: HarnessState) -> List[Effect]:
        """Compute observed dynamic state effects between pre- and post-mutation states."""
        pass

    @abstractmethod
    def evaluate_capability(
        self,
        state: HarnessState,
        probe_tasks: Optional[List[Any]] = None,
    ) -> CapabilityResult:
        """Evaluate task success and benchmark performance metrics on a candidate state."""
        pass


BaseHarnessAdapter = HarnessAdapter
