"""Universal protection layer for EvoUndo agents and tools."""
from evoundo.protection.decorator import protect, ProtectedToolWrapper
from evoundo.protection.declarative import protect_tool, ToolDefinition

__all__ = ["protect", "ProtectedToolWrapper", "protect_tool", "ToolDefinition"]

