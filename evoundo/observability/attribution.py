"""Cognitive Attribution Envelope for Operator Studio Visibility.

Provides structured, auditable operational attribution for mutations without
persisting hidden chain-of-thought, private model reasoning, or raw scratchpad thoughts.

Answers operator audit questions:
- Why did this agent make this change? (user_goal)
- What information was it acting on? (triggering_observations)
- Which earlier mutation caused this decision? (causal_parent_mutation_ids)
- Which memories were derived from it? (derived_memory_ids)
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CognitiveAttributionEnvelope:
    """Auditable operational context attached to an EvoUndo mutation."""
    agent_name: str
    agent_runtime: str
    run_id: str
    user_goal: str
    tool_name: str
    tool_call_id: str
    target_address: str
    parent_run_id: Optional[str] = None
    subagent_id: Optional[str] = None
    triggering_observations: Optional[str] = None
    causal_parent_mutation_ids: List[str] = field(default_factory=list)
    derived_memory_ids: List[str] = field(default_factory=list)
    policy_verdict: str = "PERMITTED"
    recovery_reason: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        # Strict privacy enforcement: ensure no hidden chain-of-thought leaked into context
        private_keys = ("thinking", "chain_of_thought", "private_scratchpad", "inner_monologue")
        for k in private_keys:
            if hasattr(self, k):
                delattr(self, k)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize envelope for journal storage and Operator Studio UI."""
        return {
            "agent_name": self.agent_name,
            "agent_runtime": self.agent_runtime,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "subagent_id": self.subagent_id,
            "user_goal": self.user_goal,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "target_address": self.target_address,
            "triggering_observations": self.triggering_observations,
            "causal_parent_mutation_ids": list(self.causal_parent_mutation_ids),
            "derived_memory_ids": list(self.derived_memory_ids),
            "policy_verdict": self.policy_verdict,
            "recovery_reason": self.recovery_reason,
            "timestamp": self.timestamp,
        }
