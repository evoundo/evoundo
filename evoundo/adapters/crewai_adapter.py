"""CrewAI multi-agent fleet harness adapter."""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
from evoundo.adapters.in_memory import InMemoryHarnessAdapter
from evoundo.core.state import HarnessState, MiddlewareDescriptor, ToolDescriptor


class CrewAIAdapter(InMemoryHarnessAdapter):
    """Adapter for CrewAI multi-agent crews, tasks, and tool allocations."""

    def __init__(self, initial_state: Optional[HarnessState] = None):
        super().__init__(initial_state=initial_state)

    def register_agent_role(self, role_id: str, agent_config: Dict[str, Any]) -> None:
        """Register a CrewAI agent role configuration."""
        self._state.set_config(f"crew_agent_{role_id}", agent_config)

    def register_crew_tool(self, tool_name: str, fn: Callable[..., Any], description: str = "") -> None:
        """Register a shared crew tool."""
        self._state.register_tool(ToolDescriptor(
            name=tool_name,
            description=description or f"CrewAI tool: {tool_name}",
            fn=fn,
        ))

    def add_crew_interceptor(self, interceptor_id: str, handler: Callable[..., Any], priority: int = 50) -> None:
        """Add a crew-level middleware interceptor (e.g. rate limiters, memory sync)."""
        self._state.add_middleware(MiddlewareDescriptor(
            id=interceptor_id,
            name=f"CrewInterceptor:{interceptor_id}",
            priority=priority,
            fn=handler,
        ))
