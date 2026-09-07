"""Generic MemoryAdapter interface and reference in-memory implementation.

Provides the foundational integration contract for agent memory frameworks
(Mem0, LangGraph checkpoints, Redis, SQL, Vector stores, and custom working memory).
"""

from __future__ import annotations
import abc
import copy
import logging
import uuid
from typing import Any, Dict, List, Optional, Set

from evoundo.memory.contracts import (
    MemoryDependencyGraph,
    MemoryEntry,
    MemoryInvalidationAction,
    MemoryStatus,
    StaleMemoryAccessError,
)

logger = logging.getLogger("evoundo.memory.adapter")


class MemoryAdapter(abc.ABC):
    """Abstract contract for memory providers integrating with EvoUndo world-state recovery."""

    @abc.abstractmethod
    def record_write(
        self,
        key: str,
        value: Any,
        mutation_id: Optional[str] = None,
        observation_id: Optional[str] = None,
        invalidation_action: MemoryInvalidationAction = MemoryInvalidationAction.INVALIDATE,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEntry:
        """Record or update a memory/belief with its provenance."""
        pass

    @abc.abstractmethod
    def record_dependency(self, memory_id: str, mutation_id: str) -> None:
        """Link an existing memory entry to an external mutation ID."""
        pass

    @abc.abstractmethod
    def invalidate(self, mutation_id: str) -> List[MemoryEntry]:
        """Invalidate all memory entries derived from the reverted mutation."""
        pass

    @abc.abstractmethod
    def get(self, key: str, allow_stale: bool = False) -> Any:
        """Retrieve a memory value; raises StaleMemoryAccessError if status is invalid/stale."""
        pass

    @abc.abstractmethod
    def verify(self, key: str) -> bool:
        """Verify whether the specified memory key is in a VALID state."""
        pass

    def link_derived(self, child_key_or_id: str, parent_key_or_id: str) -> None:
        """Link a derived child memory entry to its parent belief/memory."""
        pass


class DefaultMemoryAdapter(MemoryAdapter):
    """Standard in-memory implementation of the MemoryAdapter contract."""

    def __init__(self):
        self._entries: Dict[str, MemoryEntry] = {}  # key -> MemoryEntry
        self._entries_by_id: Dict[str, MemoryEntry] = {}  # memory_id -> MemoryEntry
        self.graph = MemoryDependencyGraph()

    def record_write(
        self,
        key: str,
        value: Any,
        mutation_id: Optional[str] = None,
        observation_id: Optional[str] = None,
        invalidation_action: MemoryInvalidationAction = MemoryInvalidationAction.INVALIDATE,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEntry:
        prev_entry = self._entries.get(key)
        prev_val = prev_entry.value if prev_entry else None

        mem_id = f"mem_{uuid.uuid4().hex[:10]}"
        entry = MemoryEntry(
            memory_id=mem_id,
            key=key,
            value=value,
            status=MemoryStatus.VALID,
            derived_from_mutation_id=mutation_id,
            derived_from_observation_id=observation_id,
            invalidation_action=invalidation_action,
            previous_value=prev_val,
            metadata=metadata or {},
        )

        self._entries[key] = entry
        self._entries_by_id[mem_id] = entry

        if mutation_id:
            self.graph.link(mem_id, mutation_id, observation_id)

        logger.debug("Memory write: key='%s', mem_id='%s', mutation_id='%s'", key, mem_id, mutation_id)
        return entry

    def record_dependency(self, memory_id: str, mutation_id: str) -> None:
        entry = self._entries_by_id.get(memory_id)
        if entry:
            entry.derived_from_mutation_id = mutation_id
            self.graph.link(memory_id, mutation_id)

    def link_derived(self, child_key_or_id: str, parent_key_or_id: str) -> None:
        """Link a derived child belief or deduction to its parent memory entry."""
        child_entry = self._entries.get(child_key_or_id) or self._entries_by_id.get(child_key_or_id)
        parent_entry = self._entries.get(parent_key_or_id) or self._entries_by_id.get(parent_key_or_id)
        child_id = child_entry.memory_id if child_entry else child_key_or_id
        parent_id = parent_entry.memory_id if parent_entry else parent_key_or_id
        self.graph.link_derived_memory(child_id, parent_id)
        logger.debug("Linked derived memory %s -> parent %s", child_id, parent_id)

    def invalidate(self, mutation_id: str) -> List[MemoryEntry]:
        """Apply configured invalidation actions to all memory entries derived from mutation_id
        transitively across multi-hop dependencies."""
        affected_ids = self.graph.get_all_dependent_memories_recursive(mutation_id)
        affected_entries: List[MemoryEntry] = []

        for m_id in affected_ids:
            entry = self._entries_by_id.get(m_id)
            if not entry:
                continue

            action = entry.invalidation_action

            if action == MemoryInvalidationAction.INVALIDATE:
                entry.status = MemoryStatus.INVALIDATED
                logger.warning("Memory entry '%s' (key='%s') INVALIDATED due to revert of %s", m_id, entry.key, mutation_id)

            elif action == MemoryInvalidationAction.STALE:
                entry.status = MemoryStatus.STALE
                logger.warning("Memory entry '%s' (key='%s') marked STALE due to revert of %s", m_id, entry.key, mutation_id)

            elif action == MemoryInvalidationAction.REQUIRE_REFRESH:
                entry.status = MemoryStatus.STALE
                logger.warning("Memory entry '%s' (key='%s') REQUIRES_REFRESH due to revert of %s", m_id, entry.key, mutation_id)

            elif action == MemoryInvalidationAction.RECOMPUTE:
                entry.status = MemoryStatus.RECOMPUTE_REQUIRED
                logger.warning("Memory entry '%s' (key='%s') marked RECOMPUTE_REQUIRED due to revert of %s", m_id, entry.key, mutation_id)

            elif action == MemoryInvalidationAction.RESTORE_PREVIOUS:
                entry.value = entry.previous_value
                entry.status = MemoryStatus.VALID
                logger.info("Memory entry '%s' (key='%s') RESTORE_PREVIOUS applied: value=%s", m_id, entry.key, entry.value)

            affected_entries.append(entry)

        return affected_entries

    def get(self, key: str, allow_stale: bool = False) -> Any:
        entry = self._entries.get(key)
        if not entry:
            return None

        if entry.status in (MemoryStatus.INVALIDATED, MemoryStatus.STALE, MemoryStatus.RECOMPUTE_REQUIRED) and not allow_stale:
            raise StaleMemoryAccessError(key=key, status=entry.status.value, mutation_id=entry.derived_from_mutation_id)

        return entry.value

    def verify(self, key: str) -> bool:
        entry = self._entries.get(key)
        if not entry:
            return False
        return entry.status == MemoryStatus.VALID

    def get_entry(self, key: str) -> Optional[MemoryEntry]:
        return self._entries.get(key)

    def list_entries(self) -> List[MemoryEntry]:
        return list(self._entries.values())
