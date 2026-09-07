"""
Google Agent Development Kit (ADK) & Google GenAI EvoUndo Integration.

Provides native tool wrapping, lifecycle callbacks, and multi-agent handoff
propagation for Google ADK and Google GenAI agent runtimes.
"""

from __future__ import annotations
import functools
import inspect
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.core.harness import EvoUndoHarness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.integrations.base import (
    AgentFrameworkAdapter,
    FrameworkContext,
    ToolClassification,
)


class GoogleADKAdapter(AgentFrameworkAdapter):
    """Adapter bridging Google ADK agent execution into EvoUndo lifecycle."""

    @property
    def framework_name(self) -> str:
        return "google_adk"

    def classify_tool(self, tool_name: str, tool_callable: Optional[Callable[..., Any]] = None) -> ToolClassification:
        if tool_name.startswith(("search", "get_", "read_", "fetch_", "list_", "query_")):
            return ToolClassification.READ_ONLY
        return ToolClassification.MUTATING

    def extract_context(self, *args: Any, **kwargs: Any) -> FrameworkContext:
        """Extract Google ADK context from invocation kwargs or execution metadata."""
        adk_session = kwargs.get("__adk_session_id") or kwargs.get("session_id")
        adk_agent = kwargs.get("__adk_agent_id") or kwargs.get("agent_id") or "adk_root_agent"
        tool_call_id = kwargs.get("__adk_tool_call_id") or kwargs.get("tool_call_id")
        logical_id = kwargs.get("__logical_mutation_id")

        return FrameworkContext(
            framework_name=self.framework_name,
            session_id=str(adk_session) if adk_session else None,
            agent_id=str(adk_agent) if adk_agent else "adk_agent",
            tool_call_id=str(tool_call_id) if tool_call_id else None,
            logical_mutation_id=str(logical_id) if logical_id else None,
            metadata={"adk_runtime": "google_genai_adk"},
        )

    def protect_tool(
        self,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        side_effect_detector: Optional[Callable[[], List[Effect]]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to wrap Google ADK tools with EvoUndo protection."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_target = target or getattr(fn, "__name__", "adk_tool")

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                # Extract ADK control kwargs
                session_id = kwargs.pop("__adk_session_id", None)
                agent_id = kwargs.pop("__adk_agent_id", None) or "adk_agent"
                tool_call_id = kwargs.pop("__adk_tool_call_id", None)
                logical_id = kwargs.pop("__logical_mutation_id", None)

                ctx = FrameworkContext(
                    framework_name=self.framework_name,
                    session_id=str(session_id) if session_id else None,
                    agent_id=str(agent_id) if agent_id else "adk_agent",
                    tool_name=tool_target,
                    tool_call_id=str(tool_call_id) if tool_call_id else None,
                    logical_mutation_id=str(logical_id) if logical_id else None,
                    metadata={"source": "adk_protected_tool"},
                )

                return self.execute_protected_tool(
                    tool_fn=fn,
                    context=ctx,
                    tool_args=args,
                    tool_kwargs=kwargs,
                    surface=surface,
                    target=tool_target,
                    declared_effects=declared_effects or [Effect(category=EffectCategory.RESOURCES, target=tool_target, op_type=EffectOpType.UPDATE)],
                    capture_fn=capture_fn,
                    inverse_fn=inverse_fn,
                    post_condition_probe=post_condition_probe,
                    post_condition_validator=post_condition_validator,
                    side_effect_detector=side_effect_detector,
                )

            setattr(wrapper, "_is_evoundo_protected", True)
            setattr(wrapper, "_evoundo_adapter", self)
            return wrapper

        return decorator


class EvoUndoADKPlugin:
    """Google ADK Plugin for automatic tool interception and agent-level telemetry."""

    def __init__(self, harness: Optional[EvoUndoHarness] = None):
        self.harness = harness or EvoUndoHarness()
        self.adapter = GoogleADKAdapter(harness=self.harness)

    def wrap_tool(
        self,
        tool_fn: Callable[..., Any],
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        side_effect_detector: Optional[Callable[[], List[Effect]]] = None,
    ) -> Callable[..., Any]:
        """Wrap an existing Google ADK tool function with EvoUndo recovery."""
        decorator = self.adapter.protect_tool(
            surface=surface,
            target=target,
            declared_effects=declared_effects,
            capture_fn=capture_fn,
            inverse_fn=inverse_fn,
            post_condition_probe=post_condition_probe,
            post_condition_validator=post_condition_validator,
            side_effect_detector=side_effect_detector,
        )
        return decorator(tool_fn)
