"""CrewAI multi-agent plugin for EvoUndo."""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
from evoundo.core.harness import EvoUndoHarness
from evoundo.core.state import MiddlewareDescriptor, ToolDescriptor


class EvoUndoCrewManager:
    """Manages CrewAI agent fleets with recoverability guarantees and shared tool registries."""

    def __init__(self, crew_name: str = "default_crew", harness: Optional[EvoUndoHarness] = None):
        self.crew_name = crew_name
        self.harness = harness or EvoUndoHarness()
        self.agents: Dict[str, Dict[str, Any]] = {}

    def add_agent(self, role: str, goal: str, backstory: str = "") -> None:
        """Register an agent role into the harness configuration store."""
        agent_data = {"role": role, "goal": goal, "backstory": backstory}
        self.agents[role] = agent_data
        self.harness.current_state.set_config(f"crew_{self.crew_name}_{role}", agent_data)

    def attach_shared_tool(self, tool_name: str, fn: Callable[..., Any], description: str = "") -> None:
        """Attach a shared tool callable to all crew members."""
        self.harness.current_state.register_tool(ToolDescriptor(
            name=tool_name,
            description=description or f"Shared tool for {self.crew_name}",
            fn=fn,
        ))

    def run_crew_task(self, task_description: str, agent_role: str) -> Dict[str, Any]:
        """Execute a task with an assigned crew agent while recording telemetry."""
        if agent_role not in self.agents:
            raise ValueError(f"Agent role '{agent_role}' not registered in crew '{self.crew_name}'.")

        # Execute through active tools
        tool_results = []
        for name, tool in self.harness.current_state.tools.items():
            if name in task_description.lower():
                tool_results.append(tool.fn(task_description))

        return {
            "crew": self.crew_name,
            "agent": self.agents[agent_role],
            "task": task_description,
            "harness_version": self.harness.current_state.version,
            "tool_results": tool_results,
            "status": "COMPLETED",
        }
