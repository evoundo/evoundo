"""
Anthropic Tool-Use & Claude Agent Runtime EvoUndo Integration.

Provides client-side tool execution interception, tool_use block dispatching,
and crash-safe mutation reconciliation for Anthropic/Claude agents.
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


class AnthropicToolUseAdapter(AgentFrameworkAdapter):
    """Adapter intercepting Anthropic tool_use blocks in developer client applications."""

    @property
    def framework_name(self) -> str:
        return "anthropic"

    def classify_tool(self, tool_name: str, tool_callable: Optional[Callable[..., Any]] = None) -> ToolClassification:
        if tool_name.startswith(("get_", "search_", "read_", "query_", "fetch_", "list_")):
            return ToolClassification.READ_ONLY
        return ToolClassification.MUTATING

    def dispatch_tool_use(
        self,
        tool_use_block: Dict[str, Any],
        handler_fn: Callable[..., Any],
        session_id: Optional[str] = "anthropic_session",
        logical_mutation_id: Optional[str] = None,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
    ) -> Dict[str, Any]:
        """Dispatch an Anthropic tool_use block through the EvoUndo reliability lifecycle."""
        tool_name = tool_use_block.get("name", "anthropic_tool")
        tool_id = tool_use_block.get("id", "toolu_01")
        tool_input = tool_use_block.get("input", {})
        tool_target = target or tool_name

        ctx = FrameworkContext(
            framework_name=self.framework_name,
            session_id=session_id,
            agent_id="claude_agent",
            tool_name=tool_name,
            tool_call_id=tool_id,
            logical_mutation_id=logical_mutation_id,
            metadata={"runtime": "anthropic_messages_api", "tool_use_id": tool_id},
        )

        result = self.execute_protected_tool(
            tool_fn=handler_fn,
            context=ctx,
            tool_args=(),
            tool_kwargs=tool_input,
            surface=surface,
            target=tool_target,
            declared_effects=declared_effects or [Effect(category=EffectCategory.RESOURCES, target=tool_target, op_type=EffectOpType.UPDATE)],
            capture_fn=capture_fn,
            inverse_fn=inverse_fn,
            post_condition_probe=post_condition_probe,
            post_condition_validator=post_condition_validator,
        )

        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": str(result),
            "_evoundo_meta": {"logical_mutation_id": ctx.derive_logical_id(), "status": "RECONCILED"},
        }
