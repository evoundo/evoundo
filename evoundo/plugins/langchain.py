"""LangChain and LangGraph official plugin for EvoUndo."""

from __future__ import annotations
import time
from typing import Any, Dict, List, Optional
from evoundo.core.harness import EvoUndoHarness
from evoundo.core.state import MiddlewareDescriptor, ToolDescriptor


class EvoUndoCallbackHandler:
    """LangChain-compatible BaseCallbackHandler for EvoUndo telemetry and effect tracking."""

    def __init__(self, harness: Optional[EvoUndoHarness] = None):
        self.harness = harness or EvoUndoHarness()
        self.call_history: List[Dict[str, Any]] = []

    def on_tool_start(self, serialized: Dict[str, Any], input_str: str, **kwargs: Any) -> None:
        tool_name = serialized.get("name", "unknown_tool")
        self.call_history.append({
            "event": "tool_start",
            "tool": tool_name,
            "input": input_str,
            "timestamp": time.time(),
        })

    def on_tool_end(self, output: str, **kwargs: Any) -> None:
        self.call_history.append({
            "event": "tool_end",
            "output": output,
            "timestamp": time.time(),
        })

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self.call_history.append({
            "event": "tool_error",
            "error": str(error),
            "timestamp": time.time(),
        })


class EvoUndoLangGraphWrapper:
    """Wraps LangGraph state graphs to enforce recoverability-constrained evolution on nodes."""

    def __init__(self, harness: Optional[EvoUndoHarness] = None):
        self.harness = harness or EvoUndoHarness()
        self.nodes: Dict[str, Any] = {}

    def add_node(self, node_id: str, node_fn: Any, priority: int = 50) -> None:
        """Add a node while registering it as a recoverable middleware surface component."""
        self.nodes[node_id] = node_fn
        self.harness.current_state.add_middleware(MiddlewareDescriptor(
            id=f"langgraph_{node_id}",
            name=f"LangGraphNode:{node_id}",
            priority=priority,
            fn=node_fn,
        ))

    def remove_node(self, node_id: str) -> bool:
        """Selectively remove a node from the graph."""
        if node_id in self.nodes:
            del self.nodes[node_id]
            return self.harness.current_state.remove_middleware(f"langgraph_{node_id}")
        return False

    def invoke(self, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        """Execute state through registered graph nodes in priority order."""
        current = dict(initial_state)
        for m in sorted(self.harness.current_state.middleware, key=lambda x: x.priority):
            if m.id.startswith("langgraph_"):
                res = m.fn(current)
                if isinstance(res, dict):
                    current.update(res)
        return current
