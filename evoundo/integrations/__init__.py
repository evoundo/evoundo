"""EvoUndo Real Platform Interception Bridges and Protocol Proxies."""

from evoundo.integrations.claude_code import (
    ClaudeCodeAdapter,
    ClaudeCodeMcpServer,
    ClaudeCodeMcpClient,
    ClaudeCodeMCPBridge,
)
from evoundo.integrations.mcp_proxy import EvoUndoMCPMiddleware
from evoundo.integrations.cursor import CursorAdapter
from evoundo.integrations.openclaw import OpenClawAdapter
from evoundo.integrations.codex import CodexAdapter
from evoundo.integrations.hermes import HermesAdapter
from evoundo.integrations.omnigent import OmnigentAdapter
from evoundo.integrations.base import FrameworkContext, ToolClassification

__all__ = [
    "ClaudeCodeAdapter",
    "ClaudeCodeMcpServer",
    "ClaudeCodeMcpClient",
    "ClaudeCodeMCPBridge",
    "EvoUndoMCPMiddleware",
    "CursorAdapter",
    "OpenClawAdapter",
    "CodexAdapter",
    "HermesAdapter",
    "OmnigentAdapter",
    "FrameworkContext",
    "ToolClassification",
]
