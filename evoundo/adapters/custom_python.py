"""Custom Python Agent Adapter for user-defined agent classes."""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
from evoundo.adapters.in_memory import InMemoryHarnessAdapter
from evoundo.core.state import HarnessState, ToolDescriptor


class CustomPythonAgentAdapter(InMemoryHarnessAdapter):
    """Adapter wrapping custom user-defined Python agents."""

    def __init__(self, agent_instance: Any, initial_state: Optional[HarnessState] = None):
        super().__init__(initial_state=initial_state)
        self.agent = agent_instance
        # Sync tools from agent if present
        if hasattr(self.agent, "tools") and isinstance(self.agent.tools, dict):
            for name, fn in self.agent.tools.items():
                if callable(fn):
                    self._state.register_tool(ToolDescriptor(name=name, description=f"Agent tool {name}", fn=fn))
                elif isinstance(fn, ToolDescriptor):
                    self._state.register_tool(fn)

    def sync_to_agent(self) -> None:
        """Sync updated harness tools and configuration back to the agent instance."""
        if hasattr(self.agent, "tools") and isinstance(self.agent.tools, dict):
            self.agent.tools = {name: tool.fn for name, tool in self._state.tools.items() if tool.fn}
        if hasattr(self.agent, "config") and isinstance(self.agent.config, dict):
            self.agent.config = dict(self._state.config)
