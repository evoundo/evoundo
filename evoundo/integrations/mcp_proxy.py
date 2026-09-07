"""
EvoUndo Model Context Protocol (MCP) Middleware & Proxy Integration.

Provides protocol-level recovery, crash reconciliation, and mutation idempotency
for any MCP client/server communicating over JSON-RPC tool calls.
"""

from __future__ import annotations
import json
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.core.harness import EvoUndoHarness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.integrations.base import (
    AgentFrameworkAdapter,
    FrameworkContext,
    ToolClassification,
)


class EvoUndoMCPMiddleware(AgentFrameworkAdapter):
    """Protocol-level middleware intercepting MCP tools/call requests."""

    @property
    def framework_name(self) -> str:
        return "mcp"

    def classify_tool(self, tool_name: str, tool_callable: Optional[Callable[..., Any]] = None) -> ToolClassification:
        if tool_name.startswith(("get_", "search_", "read_", "fetch_", "list_", "query_")):
            return ToolClassification.READ_ONLY
        return ToolClassification.MUTATING

    def handle_tool_call(
        self,
        tool_name: str,
        tool_arguments: Dict[str, Any],
        handler_fn: Callable[..., Any],
        client_id: Optional[str] = "mcp_client",
        request_id: Optional[str] = None,
        logical_mutation_id: Optional[str] = None,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        side_effect_detector: Optional[Callable[[], List[Effect]]] = None,
    ) -> Dict[str, Any]:
        """Process an MCP tools/call request through EvoUndo reliability lifecycle."""
        tool_target = target or tool_name
        ctx = FrameworkContext(
            framework_name=self.framework_name,
            tenant_id=client_id,
            agent_id=client_id,
            tool_name=tool_name,
            tool_call_id=str(request_id) if request_id else None,
            logical_mutation_id=logical_mutation_id,
            metadata={"protocol": "model_context_protocol", "client_id": client_id, "arguments": tool_arguments},
        )

        result = self.execute_protected_tool(
            tool_fn=handler_fn,
            context=ctx,
            tool_args=(),
            tool_kwargs=tool_arguments,
            surface=surface,
            target=tool_target,
            declared_effects=declared_effects or [Effect(category=EffectCategory.RESOURCES, target=tool_target, op_type=EffectOpType.UPDATE)],
            capture_fn=capture_fn,
            inverse_fn=inverse_fn,
            post_condition_probe=post_condition_probe,
            post_condition_validator=post_condition_validator,
            side_effect_detector=side_effect_detector,
        )

        return {
            "content": [{"type": "text", "text": json.dumps(result) if not isinstance(result, str) else result}],
            "isError": False,
            "_evoundo_meta": {
                "logical_mutation_id": ctx.derive_logical_id(),
                "status": "RECONCILED",
            }
        }
