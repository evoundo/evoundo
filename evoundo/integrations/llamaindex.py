"""
LlamaIndex Agent & Workflow EvoUndo Integration.

Provides native FunctionTool wrappers, query engine callback hooks,
and crash-safe mutation reconciliation for LlamaIndex.
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


class LlamaIndexAdapter(AgentFrameworkAdapter):
    """Adapter for LlamaIndex FunctionTools, QueryEngines, and Workflows."""

    @property
    def framework_name(self) -> str:
        return "llamaindex"

    def classify_tool(self, tool_name: str, tool_callable: Optional[Callable[..., Any]] = None) -> ToolClassification:
        if tool_name.startswith(("get_", "search_", "read_", "query_", "fetch_", "list_")):
            return ToolClassification.READ_ONLY
        return ToolClassification.MUTATING

    def protect_tool(
        self,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Wrap a LlamaIndex tool callable with EvoUndo lifecycle protection."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = target or getattr(fn, "__name__", "llamaindex_tool")

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                session_id = kwargs.pop("__llama_session_id", None) or kwargs.pop("session_id", None)
                agent_id = kwargs.pop("__llama_agent_id", None) or kwargs.pop("agent_id", "llamaindex_agent")
                call_id = kwargs.pop("__llama_call_id", None) or kwargs.pop("call_id", None)
                logical_id = kwargs.pop("__logical_mutation_id", None)

                ctx = FrameworkContext(
                    framework_name=self.framework_name,
                    session_id=str(session_id) if session_id else None,
                    agent_id=str(agent_id) if agent_id else "llamaindex_agent",
                    tool_name=tool_name,
                    tool_call_id=str(call_id) if call_id else None,
                    logical_mutation_id=str(logical_id) if logical_id else None,
                    metadata={"runtime": "llamaindex_workflows"},
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
