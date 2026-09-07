"""Microsoft AutoGen conversational agent plugin for EvoUndo."""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
from evoundo.core.harness import EvoUndoHarness
from evoundo.core.state import ToolDescriptor


class EvoUndoConversableAgent:
    """AutoGen-compatible ConversableAgent wrapper with EvoUndo recoverability control."""

    def __init__(self, name: str, system_message: str = "", harness: Optional[EvoUndoHarness] = None):
        self.name = name
        self.system_message = system_message
        self.harness = harness or EvoUndoHarness()
        self.harness.current_state.set_config(f"autogen_agent_{name}", {"system_message": system_message})

    def register_for_execution(self, name: str, description: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a tool function in the EvoUndo Tool Registry."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.harness.current_state.register_tool(ToolDescriptor(
                name=name,
                description=description or fn.__doc__ or f"AutoGen tool: {name}",
                fn=fn,
            ))
            return fn
        return decorator

    def generate_reply(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """Generate conversational reply through the active agent harness state."""
        last_message = messages[-1]["content"] if messages else ""
        return {
            "agent": self.name,
            "reply": f"[{self.name} v{self.harness.current_state.version}] Processed: {last_message}",
            "active_tools": list(self.harness.current_state.tools.keys()),
        }
