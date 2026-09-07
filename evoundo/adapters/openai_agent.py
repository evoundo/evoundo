"""OpenAI-style tool agent adapter."""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
from evoundo.adapters.in_memory import InMemoryHarnessAdapter
from evoundo.core.state import HarnessState, ToolDescriptor


class OpenAIToolAgentAdapter(InMemoryHarnessAdapter):
    """Adapter for agents using OpenAI function/tool calling specifications."""

    def __init__(self, tools_spec: Optional[List[Dict[str, Any]]] = None, initial_state: Optional[HarnessState] = None):
        super().__init__(initial_state=initial_state)
        if tools_spec:
            for spec in tools_spec:
                fn_info = spec.get("function", spec)
                name = fn_info.get("name", "tool")
                desc = fn_info.get("description", "")
                params = fn_info.get("parameters", {})
                self._state.register_tool(ToolDescriptor(
                    name=name,
                    description=desc,
                    parameters_schema=params,
                    metadata={"openai_spec": spec},
                ))

    def export_tools_spec(self) -> List[Dict[str, Any]]:
        """Export current active tools in OpenAI function calling JSON schema format."""
        specs = []
        for name, tool in self._state.tools.items():
            specs.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema or {"type": "object", "properties": {}},
                },
            })
        return specs
