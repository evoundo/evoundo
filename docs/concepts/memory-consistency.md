# Agent Memory & Recovery Consistency

Autonomous agents don't just mutate external systems—they build long-term and working memory based on what they observe. If an external mutation is reverted, the agent's internal memory may become dangerously stale.

---

## 1. The Stale Memory Problem

```text
1. Agent creates database table `orders_v2`.
2. Agent stores in working memory: "Table orders_v2 exists and has schema X."
3. Agent plans subsequent operations based on this belief.
4. An external monitor or operator reverts mutation 1 (drops table `orders_v2`).
5. 💥 Agent attempts to query `orders_v2` and crashes because its internal memory is out of sync with physical reality!
```

---

## 2. Memory Invalidation Hooks

EvoUndo provides memory adapters (`evoundo.memory.adapter.MemoryAdapter`) that link agent memory items to specific mutation IDs or resource addresses.

```python
from evoundo.core.harness import EvoUndoHarness
from evoundo.memory.adapter import MemoryAdapter

class AgentWorkingMemoryAdapter(MemoryAdapter):
    def __init__(self, agent_memory):
        self.memory = agent_memory

    def on_mutation_reverted(self, mutation_id: str, resource: str):
        # Invalidate memory keys associated with the reverted resource
        stale_keys = [k for k, v in self.memory.items() if v.get("derived_from") == mutation_id]
        for k in stale_keys:
            del self.memory[k]
            print(f"Invalidated stale agent memory: {k}")

# Register adapter with the active harness
harness = EvoUndoHarness.get_instance()
harness.register_memory_adapter(AgentWorkingMemoryAdapter(agent.working_memory))
```

When `harness.revert(mutation_id)` completes, EvoUndo automatically notifies all registered memory adapters, ensuring that:
- Stale beliefs are purged.
- In-flight contexts are refreshed.
- Subsequent agent decisions are based on current, verified reality.
