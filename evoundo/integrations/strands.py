"""
AWS Strands Agents EvoUndo Integration.

Provides BeforeToolCall and AfterToolCall lifecycle interception,
swarm/graph agent coordination tracking, and failure recovery for AWS Strands Agents.
"""

from __future__ import annotations
import functools
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.core.harness import EvoUndoHarness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.integrations.base import (
    AgentFrameworkAdapter,
    FrameworkContext,
    ToolClassification,
)


class StrandsAgentsAdapter(AgentFrameworkAdapter):
    """Adapter for AWS Strands Agent runtimes, Swarms, and Graph execution."""

    @property
    def framework_name(self) -> str:
        return "strands"

    def classify_tool(self, tool_name: str, tool_callable: Optional[Callable[..., Any]] = None) -> ToolClassification:
        if tool_name.startswith(("get_", "search_", "read_", "query_", "fetch_", "list_")):
            return ToolClassification.READ_ONLY
        return ToolClassification.MUTATING

    def create_tool_hook(
        self,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Wrap tool with BeforeToolCall and AfterToolCall semantics."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = target or getattr(fn, "__name__", "strands_tool")

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                thread_id = kwargs.pop("__strands_thread_id", None) or kwargs.pop("thread_id", None)
                agent_id = kwargs.pop("__strands_agent_id", None) or kwargs.pop("agent_id", "strands_agent")
                call_id = kwargs.pop("__strands_call_id", None) or kwargs.pop("call_id", None)
                logical_id = kwargs.pop("__logical_mutation_id", None)

                ctx = FrameworkContext(
                    framework_name=self.framework_name,
                    thread_id=str(thread_id) if thread_id else None,
                    agent_id=str(agent_id) if agent_id else "strands_agent",
                    tool_name=tool_name,
                    tool_call_id=str(call_id) if call_id else None,
                    logical_mutation_id=str(logical_id) if logical_id else None,
                    metadata={"runtime": "aws_strands_agents"},
                )

                return self.execute_protected_tool(
                    tool_fn=fn,
                    context=ctx,
                    tool_args=args,
                    tool_kwargs=kwargs,
                    surface=surface,
                    target=tool_name,
                    declared_effects=declared_effects or [Effect(category=EffectCategory.RESOURCES, target=tool_name, op_type=EffectOpType.UPDATE)],
                    capture_fn=capture_fn,
                    inverse_fn=inverse_fn,
                    post_condition_probe=post_condition_probe,
                    post_condition_validator=post_condition_validator,
                )

            setattr(wrapper, "_is_evoundo_protected", True)
            setattr(wrapper, "_evoundo_adapter", self)
            return wrapper

        return decorator
