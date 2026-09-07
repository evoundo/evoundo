"""
OpenAI Agents SDK EvoUndo Integration.

Provides native FunctionTool wrappers, session context propagation,
multi-agent handoff tracking, and agent-as-tool protection for the OpenAI Agents SDK.
"""

from __future__ import annotations
import functools
import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.core.harness import EvoUndoHarness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.integrations.base import (
    AgentFrameworkAdapter,
    FrameworkContext,
    ToolClassification,
)


class OpenAIAgentsSDKAdapter(AgentFrameworkAdapter):
    """Adapter for OpenAI Agents SDK agent runtimes and FunctionTool instances."""

    @property
    def framework_name(self) -> str:
        return "openai_agents_sdk"

    def classify_tool(self, tool_name: str, tool_callable: Optional[Callable[..., Any]] = None) -> ToolClassification:
        if tool_name.startswith(("get_", "search_", "read_", "query_", "fetch_", "list_")):
            return ToolClassification.READ_ONLY
        return ToolClassification.MUTATING

    def protect_function_tool(
        self,
        tool_callable: Optional[Callable[..., Any]] = None,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        side_effect_detector: Optional[Callable[[], List[Effect]]] = None,
    ) -> Callable[..., Any]:
        """Wrap an OpenAI Agents SDK FunctionTool or python callable."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = target or getattr(fn, "__name__", "openai_tool")

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                session_id = kwargs.pop("__openai_session_id", None) or kwargs.pop("session_id", None)
                agent_id = kwargs.pop("__openai_agent_id", None) or kwargs.pop("agent_id", "openai_agent")
                tool_call_id = kwargs.pop("__openai_tool_call_id", None) or kwargs.pop("tool_call_id", None)
                logical_id = kwargs.pop("__logical_mutation_id", None)

                ctx = FrameworkContext(
                    framework_name=self.framework_name,
                    session_id=str(session_id) if session_id else None,
                    agent_id=str(agent_id) if agent_id else "openai_agent",
                    tool_name=tool_name,
                    tool_call_id=str(tool_call_id) if tool_call_id else None,
                    logical_mutation_id=str(logical_id) if logical_id else None,
                    metadata={"runtime": "openai_agents_sdk"},
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
                    side_effect_detector=side_effect_detector,
                )

            setattr(wrapper, "_is_evoundo_protected", True)
            setattr(wrapper, "_evoundo_adapter", self)
            return wrapper

        if tool_callable is not None:
            return decorator(tool_callable)
        return decorator


class OpenAIAgentHandoff:
    """Helper to track mutation identity across OpenAI multi-agent handoffs."""

    def __init__(self, adapter: OpenAIAgentsSDKAdapter):
        self.adapter = adapter

    def handoff(
        self,
        from_agent: str,
        to_agent: str,
        context: FrameworkContext,
    ) -> FrameworkContext:
        return self.adapter.on_handoff(from_agent, to_agent, context)
