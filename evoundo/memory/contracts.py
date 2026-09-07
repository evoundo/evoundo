"""Memory and World-State Synchronization Contracts for EvoUndo.

Defines the formal relationship:
    Mutation (M) -> Observation (O) -> Memory Entry (MEM)

When an external mutation is reverted or invalidated, dependent memory entries
are transitioned to STALE or INVALIDATED states according to configured Invalidation Actions,
preventing autonomous agents from operating under cognitive dissonance or stale beliefs.
"""

from __future__ import annotations
import enum
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


class StaleMemoryAccessError(RuntimeError):
    """Raised when an autonomous agent attempts to read an invalidated or stale memory entry."""
    def __init__(self, key: str, status: str, mutation_id: Optional[str] = None):
        super().__init__(
            f"StaleMemoryAccessError: Memory entry '{key}' has status '{status}' "
            f"(derived from reverted/invalidated mutation '{mutation_id or 'unknown'}'). "
            f"Agent must execute REQUIRE_REFRESH or RECOMPUTE before accessing this belief."
        )
        self.key = key
        self.status = status
        self.mutation_id = mutation_id


class MemoryStatus(str, enum.Enum):
    """Lifecycle status of an agent memory/belief entry."""
    VALID = "VALID"
    STALE = "STALE"
    INVALIDATED = "INVALIDATED"
    RECOMPUTE_REQUIRED = "RECOMPUTE_REQUIRED"


class MemoryInvalidationAction(str, enum.Enum):
    """Behavior to apply to derived memory when its parent mutation is reverted."""
    INVALIDATE = "INVALIDATE"          # Reject access; raise StaleMemoryAccessError
    RECOMPUTE = "RECOMPUTE"            # Flag for planner re-computation
    RESTORE_PREVIOUS = "RESTORE_PREVIOUS"  # Restore prior pre-mutation snapshot value
    REQUIRE_REFRESH = "REQUIRE_REFRESH"    # Require new tool observation before access


@dataclass
class MemoryEntry:
    """Individual tracked belief or fact in an autonomous agent's memory."""
    memory_id: str
    key: str
    value: Any
    status: MemoryStatus = MemoryStatus.VALID
    derived_from_mutation_id: Optional[str] = None
    derived_from_observation_id: Optional[str] = None
    invalidation_action: MemoryInvalidationAction = MemoryInvalidationAction.INVALIDATE
    previous_value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "key": self.key,
            "value": self.value,
            "status": self.status.value,
            "derived_from_mutation_id": self.derived_from_mutation_id,
            "derived_from_observation_id": self.derived_from_observation_id,
            "invalidation_action": self.invalidation_action.value,
            "previous_value": self.previous_value,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class MemoryDependencyGraph:
    """Tracks causal dependencies between physical mutations and agent memory entries,
    including multi-hop derived memory DAGs (M -> MEM_1 -> MEM_2 -> ...)."""
    # mutation_id -> set of root memory_ids
    mutation_to_memories: Dict[str, Set[str]] = field(default_factory=dict)
    # memory_id -> mutation_id
    memory_to_mutation: Dict[str, str] = field(default_factory=dict)
    # observation_id -> memory_id
    observation_to_memory: Dict[str, Set[str]] = field(default_factory=dict)
    # parent_memory_id -> set of child derived memory_ids
    memory_to_derived_memories: Dict[str, Set[str]] = field(default_factory=dict)
    # child_memory_id -> set of parent memory_ids
    derived_memory_to_parents: Dict[str, Set[str]] = field(default_factory=dict)

    def link(self, memory_id: str, mutation_id: str, observation_id: Optional[str] = None) -> None:
        """Register a causal dependency from mutation to derived root memory."""
        self.mutation_to_memories.setdefault(mutation_id, set()).add(memory_id)
        self.memory_to_mutation[memory_id] = mutation_id
        if observation_id:
            self.observation_to_memory.setdefault(observation_id, set()).add(memory_id)

    def link_derived_memory(self, child_memory_id: str, parent_memory_id: str) -> None:
        """Register a causal link from parent memory to a derived child belief/hypothesis."""
        self.memory_to_derived_memories.setdefault(parent_memory_id, set()).add(child_memory_id)
        self.derived_memory_to_parents.setdefault(child_memory_id, set()).add(parent_memory_id)

    def get_dependent_memories(self, mutation_id: str) -> Set[str]:
        """Return 1-hop direct memory IDs derived from the specified mutation."""
        return set(self.mutation_to_memories.get(mutation_id, set()))

    def get_all_dependent_memories_recursive(self, mutation_id: str, max_depth: int = 20) -> Set[str]:
        """Recursively retrieve all direct and multi-hop derived memory IDs descending from mutation_id.
        
        Guarantees:
        - Cycle detection: maintains visited and recursion_stack to handle cyclic graphs safely.
        - Bounded depth: restricts traversal to max_depth to prevent runaway recursions.
        - Preserves DAG traversal across branching child nodes.
        """
        root_memories = self.mutation_to_memories.get(mutation_id, set())
        all_affected: Set[str] = set()
        visited: Set[str] = set()

        def _traverse(current_id: str, depth: int, active_stack: Set[str]) -> None:
            if depth > max_depth:
                return
            if current_id in active_stack:
                # Cycle detected: break loop safely
                return
            if current_id in visited:
                return

            visited.add(current_id)
            all_affected.add(current_id)
            active_stack.add(current_id)

            # Traverse child derived memories
            children = self.memory_to_derived_memories.get(current_id, set())
            for child_id in children:
                _traverse(child_id, depth + 1, active_stack)

            active_stack.remove(current_id)

        for root_id in root_memories:
            _traverse(root_id, depth=1, active_stack=set())

        return all_affected

    def remove_mutation(self, mutation_id: str) -> None:
        """Clear links for a mutation once processed."""
        mem_ids = self.mutation_to_memories.pop(mutation_id, set())
        for m_id in mem_ids:
            self.memory_to_mutation.pop(m_id, None)
