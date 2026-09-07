"""Effect contracts, categories, equivalence policies, and audit reports."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class EffectCategory(str, Enum):
    """Supported state surfaces for self-modification."""
    CONFIG = "config"
    TOOLS = "tools"
    MIDDLEWARE = "middleware"
    EVENT_LISTENERS = "event_listeners"
    FILES = "files"
    RESOURCES = "resources"
    PROMPTS = "prompts"
    ROUTING = "routing"
    MEMORY = "memory"
    PERMISSIONS = "permissions"


class EquivalencePolicy(str, Enum):
    """Equivalence policies for comparing pre- and post-recovery states."""
    EXACT = "exact"                          # Byte-for-byte / key-for-key exact equality
    SET_EQUIVALENT = "set_equivalent"        # Unordered set equality (ignoring order in sequences)
    ORDER_INSENSITIVE = "order_insensitive"  # Map/list key order invariance
    NORMALIZED = "normalized"                # Strip volatile metadata, whitespace, timestamps


class EffectOpType(str, Enum):
    """Operation types on state entities."""
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    REORDER = "REORDER"


@dataclass
class Effect:
    """Strongly-typed individual observed or declared state mutation effect."""
    category: EffectCategory
    target: str
    op_type: EffectOpType
    old_value: Any = None
    new_value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def key_tuple(self) -> Tuple[EffectCategory, str]:
        return (self.category, self.target)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value if isinstance(self.category, EffectCategory) else str(self.category),
            "target": self.target,
            "op_type": self.op_type.value if isinstance(self.op_type, EffectOpType) else str(self.op_type),
            "old_value": self.old_value,
            "new_value": self.new_value,
            "metadata": self.metadata,
        }


@dataclass
class EffectContract:
    """Declared effect contract $C_e$ for a mutation proposal.
    
    Declares the explicit subset of harness surfaces and targets the mutation will alter.
    """
    config: Set[str] = field(default_factory=set)
    tools: Set[str] = field(default_factory=set)
    middleware: Set[str] = field(default_factory=set)
    event_listeners: Set[str] = field(default_factory=set)
    files: Set[str] = field(default_factory=set)
    resources: Set[str] = field(default_factory=set)
    prompts: Set[str] = field(default_factory=set)
    routing: Set[str] = field(default_factory=set)
    memory: Set[str] = field(default_factory=set)
    permissions: Set[str] = field(default_factory=set)

    # Per-category equivalence policies (default EXACT across all dimensions)
    policies: Dict[str, EquivalencePolicy] = field(default_factory=lambda: {
        cat.value: EquivalencePolicy.EXACT for cat in EffectCategory
    })

    def declare(self, category: EffectCategory, target: str) -> None:
        """Add a declared target to the contract."""
        attr = category.value if isinstance(category, EffectCategory) else str(category)
        if hasattr(self, attr):
            getattr(self, attr).add(target)
        else:
            raise ValueError(f"Unknown effect category: {category}")

    def set_policy(self, category: EffectCategory, policy: EquivalencePolicy) -> None:
        cat_key = category.value if isinstance(category, EffectCategory) else str(category)
        self.policies[cat_key] = policy

    def get_policy(self, category: EffectCategory | str) -> EquivalencePolicy:
        cat_key = category.value if isinstance(category, EffectCategory) else str(category)
        return self.policies.get(cat_key, EquivalencePolicy.EXACT)

    def all_declared(self) -> Set[Tuple[EffectCategory, str]]:
        """Return the complete set of (category, target) tuples declared."""
        declared: Set[Tuple[EffectCategory, str]] = set()
        for cat in EffectCategory:
            attr = cat.value
            if hasattr(self, attr):
                targets = getattr(self, attr)
                for t in targets:
                    declared.add((cat, t))
        return declared

    def is_declared(self, category: EffectCategory | str, target: str) -> bool:
        """Check if a specific (category, target) effect is permitted."""
        cat_enum = category if isinstance(category, EffectCategory) else EffectCategory(category)
        return (cat_enum, target) in self.all_declared()


@dataclass
class EffectAuditReport:
    """Independent verification report contrasting declared effects against observed diffs."""
    declared_effects: Set[Tuple[EffectCategory, str]]
    observed_effects: Set[Tuple[EffectCategory, str]]
    hidden_effects: Set[Tuple[EffectCategory, str]]
    unrealized_effects: Set[Tuple[EffectCategory, str]]
    is_clean: bool
    detailed_effects: List[Effect] = field(default_factory=list)

    @property
    def hidden_effect_strings(self) -> List[str]:
        return sorted([f"[{cat.value}] {tgt}" for cat, tgt in self.hidden_effects])

    @property
    def observed_effect_strings(self) -> List[str]:
        return sorted([f"[{cat.value}] {tgt}" for cat, tgt in self.observed_effects])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "declared_count": len(self.declared_effects),
            "observed_count": len(self.observed_effects),
            "hidden_effects": self.hidden_effect_strings,
            "unrealized_effects": sorted([f"[{cat.value}] {tgt}" for cat, tgt in self.unrealized_effects]),
            "is_clean": self.is_clean,
            "detailed_effects": [e.to_dict() for e in self.detailed_effects],
        }
