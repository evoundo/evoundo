"""
Microsoft Agent Framework EvoUndo Integration.

Provides function middleware, workflow checkpoint/resume hooks,
and state reconciliation for the Microsoft Agent Framework runtime.
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


class MicrosoftAgentFrameworkAdapter(AgentFrameworkAdapter):
    """Adapter for Microsoft Agent Framework function middleware and workflows."""

    @property
    def framework_name(self) -> str:
        return "microsoft_agent_framework"

    def classify_tool(self, tool_name: str, tool_callable: Optional[Callable[..., Any]] = None) -> ToolClassification:
        if tool_name.startswith(("get_", "search_", "read_", "query_", "fetch_", "list_")):
            return ToolClassification.READ_ONLY
        return ToolClassification.MUTATING

    def create_function_middleware(
        self,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Create function middleware intercepting Microsoft Agent tool invocations."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = target or getattr(fn, "__name__", "ms_agent_tool")

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                conversation_id = kwargs.pop("__ms_conversation_id", None) or kwargs.pop("conversation_id", None)
                agent_id = kwargs.pop("__ms_agent_id", None) or kwargs.pop("agent_id", "ms_agent")
                call_id = kwargs.pop("__ms_tool_call_id", None) or kwargs.pop("tool_call_id", None)
                logical_id = kwargs.pop("__logical_mutation_id", None)

                ctx = FrameworkContext(
                    framework_name=self.framework_name,
                    session_id=str(conversation_id) if conversation_id else None,
                    agent_id=str(agent_id) if agent_id else "ms_agent",
                    tool_name=tool_name,
                    tool_call_id=str(call_id) if call_id else None,
                    logical_mutation_id=str(logical_id) if logical_id else None,
                    metadata={"runtime": "microsoft_agent_framework"},
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
