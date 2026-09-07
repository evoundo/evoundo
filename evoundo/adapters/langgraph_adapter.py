"""LangGraph and node-graph agent harness adapter."""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
from evoundo.adapters.in_memory import InMemoryHarnessAdapter
from evoundo.core.state import HarnessState, MiddlewareDescriptor, ToolDescriptor


class LangGraphAdapter(InMemoryHarnessAdapter):
    """Adapter for graph-based and node-orchestrated agent execution pipelines."""

    def __init__(self, initial_state: Optional[HarnessState] = None):
        super().__init__(initial_state=initial_state)

    def register_node(self, node_id: str, handler: Callable[..., Any], priority: int = 100) -> None:
        """Register a graph node as a pipeline middleware component."""
        self._state.add_middleware(MiddlewareDescriptor(
            id=node_id,
            name=f"Node:{node_id}",
            priority=priority,
            fn=handler,
            metadata={"type": "graph_node"},
        ))

    def remove_node(self, node_id: str) -> bool:
        """Remove a graph node from the pipeline."""
        return self._state.remove_middleware(node_id)
