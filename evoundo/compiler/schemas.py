"""Declarative mutation schemas and data models for EvoUndo candidate compilation."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OpSpec:
    """A single declarative operation in a self-evolution proposal."""
    op_type: str                  # e.g., set_config, register_tool, add_middleware, write_file
    target: str                   # target key, name, or path
    value: Any = None             # new value, code/schema, or payload
    witness_key: Optional[str] = None
    surface: Optional[str] = None # config, tools, middleware, listeners, files, resources, prompts
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {"op_type": self.op_type, "target": self.target}
        if self.value is not None:
            d["value"] = self.value
        if self.witness_key is not None:
            d["witness_key"] = self.witness_key
        if self.surface is not None:
            d["surface"] = self.surface
        if self.metadata:
            d["metadata"] = self.metadata
        return d


@dataclass
class DeclarativeMutationSpec:
    """Complete declarative specification of a candidate self-evolution."""
    mutation_id: str
    description: str
    complexity_level: int = 1
    capture_ops: List[OpSpec] = field(default_factory=list)
    forward_ops: List[OpSpec] = field(default_factory=list)
    recovery_ops: List[OpSpec] = field(default_factory=list)
    declared_effects: Dict[str, List[str]] = field(default_factory=dict)
    expected_capability_delta: float = 0.0
    recovery_language: str = "L1"  # "L0" or "L1"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "description": self.description,
            "complexity_level": self.complexity_level,
            "capture_ops": [op.to_dict() for op in self.capture_ops],
            "forward_ops": [op.to_dict() for op in self.forward_ops],
            "recovery_ops": [op.to_dict() for op in self.recovery_ops],
            "declared_effects": self.declared_effects,
            "expected_capability_delta": self.expected_capability_delta,
            "recovery_language": self.recovery_language,
            "metadata": self.metadata,
        }
