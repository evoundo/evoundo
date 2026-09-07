"""Working memory and state synchronization for autonomous agents.

Tracks agent conversational history, internal beliefs, scratchpad memory, and mutation-to-belief
mappings. Evaluates memory-state synchronization and desynchronization when mutations are
reverted externally or self-corrected.

Implements the MemoryAdapter contract for bi-directional world-state synchronization.
"""

from __future__ import annotations
import copy
import logging
from typing import Any, Dict, List, Optional

from evoundo.memory.adapter import DefaultMemoryAdapter, MemoryAdapter
from evoundo.memory.contracts import (
    MemoryEntry,
    MemoryInvalidationAction,
    MemoryStatus,
    StaleMemoryAccessError,
)

logger = logging.getLogger("evoundo.memory.working_memory")


class AgentWorkingMemory(MemoryAdapter):
    """Manages working memory, conversational scratchpad, and memory synchronization for agents."""

    def __init__(self, system_prompt: str, goal: str):
        self.system_prompt = system_prompt
        self.goal = goal
        self.messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Task Goal: {goal}"},
        ]
        self.beliefs: Dict[str, Any] = {}
        # Mapping mutation_id -> snapshot of beliefs before mutation
        self._belief_snapshots: Dict[str, Dict[str, Any]] = {}
        # Mapping mutation_id -> message index where tool was recorded
        self._mutation_message_indices: Dict[str, int] = {}
        # Internal memory adapter engine
        self.adapter = DefaultMemoryAdapter()

    def add_assistant_message(self, content: Optional[str] = None, tool_calls: Optional[List[Dict[str, Any]]] = None) -> None:
        msg: Dict[str, Any] = {"role": "assistant"}
        if content is not None:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, tool_name: str, result: str, mutation_id: Optional[str] = None) -> None:
        idx = len(self.messages)
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": str(result),
        })
        if mutation_id:
            self._mutation_message_indices[mutation_id] = idx

    def snapshot_beliefs(self, mutation_id: str) -> None:
        """Capture pre-mutation snapshot of agent's internal beliefs."""
        self._belief_snapshots[mutation_id] = copy.deepcopy(self.beliefs)

    def record_write(
        self,
        key: str,
        value: Any,
        mutation_id: Optional[str] = None,
        observation_id: Optional[str] = None,
        invalidation_action: MemoryInvalidationAction = MemoryInvalidationAction.INVALIDATE,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEntry:
        """Record belief in adapter and update scratchpad dict."""
        entry = self.adapter.record_write(
            key=key,
            value=value,
            mutation_id=mutation_id,
            observation_id=observation_id,
            invalidation_action=invalidation_action,
            metadata=metadata,
        )
        self.beliefs[key] = value
        return entry

    def record_dependency(self, memory_id: str, mutation_id: str) -> None:
        self.adapter.record_dependency(memory_id, mutation_id)

    def link_derived_belief(self, child_key_or_id: str, parent_key_or_id: str) -> None:
        """Link a derived belief to a parent belief in working memory."""
        self.adapter.link_derived(child_key_or_id, parent_key_or_id)

    def update_belief(
        self,
        key: str,
        value: Any,
        mutation_id: Optional[str] = None,
        invalidation_action: MemoryInvalidationAction = MemoryInvalidationAction.INVALIDATE,
    ) -> None:
        """Update an internal fact or belief in the agent's scratchpad with provenance tracking."""
        self.record_write(
            key=key,
            value=value,
            mutation_id=mutation_id,
            invalidation_action=invalidation_action,
        )

    def get(self, key: str, allow_stale: bool = False) -> Any:
        """Retrieve belief from adapter; raises StaleMemoryAccessError if invalidated."""
        val = self.adapter.get(key, allow_stale=allow_stale)
        if val is None:
            return self.beliefs.get(key)
        return val

    def get_belief(self, key: str, default: Any = None, allow_stale: bool = False) -> Any:
        """Get belief value with stale access enforcement."""
        try:
            val = self.get(key, allow_stale=allow_stale)
            return val if val is not None else default
        except StaleMemoryAccessError:
            if allow_stale:
                return self.beliefs.get(key, default)
            raise

    def invalidate(self, mutation_id: str) -> List[MemoryEntry]:
        """Invalidate dependent memory entries and update beliefs and conversational context."""
        affected = self.adapter.invalidate(mutation_id)
        for entry in affected:
            if entry.invalidation_action == MemoryInvalidationAction.RESTORE_PREVIOUS:
                self.beliefs[entry.key] = entry.value
            else:
                self.beliefs.pop(entry.key, None)

            # Inject cognitive notification into conversational context so LLM knows state reverted
            self.messages.append({
                "role": "system",
                "content": (
                    f"[SYSTEM NOTIFICATION: Mutation '{mutation_id}' was reverted by operator/system. "
                    f"Derived belief '{entry.key}' is now {entry.status.value}. "
                    f"Current physical world-state must be re-observed.]"
                ),
            })
            logger.info("Injected system notification for memory invalidation on key '%s'", entry.key)

        return affected

    def verify(self, key: str) -> bool:
        return self.adapter.verify(key)

    def rollback_memory(self, mutation_id: str) -> bool:
        """Revert internal beliefs to the state prior to mutation_id."""
        # 1. Trigger adapter invalidation
        self.invalidate(mutation_id)

        # 2. Restore snapshot if present
        if mutation_id in self._belief_snapshots:
            snapshot = copy.deepcopy(self._belief_snapshots[mutation_id])
            # Clean up keys created after snapshot
            keys_to_remove = set(self.beliefs.keys()) - set(snapshot.keys())
            for k in keys_to_remove:
                self.beliefs.pop(k, None)
                if hasattr(self.adapter, "_entries"):
                    self.adapter._entries.pop(k, None)
                if hasattr(self.adapter, "graph"):
                    self.adapter.graph.remove_key(k) if hasattr(self.adapter.graph, "remove_key") else None

            # Restore snapshot values to beliefs and synchronize adapter
            self.beliefs = snapshot
            for k, v in self.beliefs.items():
                self.adapter.record_write(key=k, value=v)

            logger.info("Rolled back working memory beliefs for mutation %s: %s", mutation_id, list(self.beliefs.keys()))
            return True
        logger.warning("No memory snapshot found for mutation %s", mutation_id)
        return False

    def detect_memory_desync(self, key: str, physical_value: Any) -> bool:
        """Returns True if agent's internal belief diverges from physical external reality."""
        belief_val = self.beliefs.get(key)
        is_desynced = (belief_val != physical_value)
        if is_desynced:
            logger.warning(
                "Memory Desynchronization Detected for '%s': belief=%s vs physical=%s",
                key, belief_val, physical_value
            )
        return is_desynced

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "message_count": len(self.messages),
            "beliefs": copy.deepcopy(self.beliefs),
            "tracked_mutations": list(self._belief_snapshots.keys()),
            "adapter_entries": [e.to_dict() for e in self.adapter.list_entries()],
        }
