"""Multi-Agent Causal Tree (Gap 16 Formalization).

Coordinating directed causal trees between parent orchestrator agents and child specialist
sub-agents. Distinguishes between hypothesis-contingent actions (which must be rolled back
if an incident diagnosis is disproven) and independent actions (e.g. organic load scaling,
which must be preserved).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import logging
import time
from typing import Any, Dict, List, Optional, Set

from evoundo.recovery import RecoveryTransactionGroup

logger = logging.getLogger("evoundo.core.causal_tree")


class CausalContingency(str, Enum):
    HYPOTHESIS_DEPENDENT = "HYPOTHESIS_DEPENDENT"
    INDEPENDENT = "INDEPENDENT"


@dataclass
class CausalNode:
    action_id: str
    parent_id: str
    child_agent: str
    action_type: str
    mutation_id: Optional[str] = None
    contingency: CausalContingency = CausalContingency.HYPOTHESIS_DEPENDENT
    target_resource: str = ""
    status: str = "EXECUTED"
    created_at: float = field(default_factory=time.time)


class MultiAgentCausalTree:
    """Directed causal lineage tree coordinating multi-agent actions and selective recovery."""

    def __init__(self) -> None:
        self._parents: Dict[str, Dict[str, Any]] = {}
        self._nodes: Dict[str, CausalNode] = {}
        self._parent_to_actions: Dict[str, List[str]] = {}

    def register_parent(self, parent_id: str, context: Optional[Dict[str, Any]] = None) -> None:
        """Register an orchestrator parent root node (e.g. an incident or goal)."""
        self._parents[parent_id] = context or {}
        if parent_id not in self._parent_to_actions:
            self._parent_to_actions[parent_id] = []

    def register_child_action(
        self,
        action_id: str,
        parent_id: str,
        child_agent: str,
        action_type: str,
        mutation_id: Optional[str] = None,
        contingency: CausalContingency = CausalContingency.HYPOTHESIS_DEPENDENT,
        target_resource: str = "",
    ) -> CausalNode:
        """Register an action executed by a child sub-agent under a parent node."""
        if parent_id not in self._parents:
            self.register_parent(parent_id)

        node = CausalNode(
            action_id=action_id,
            parent_id=parent_id,
            child_agent=child_agent,
            action_type=action_type,
            mutation_id=mutation_id,
            contingency=contingency,
            target_resource=target_resource,
        )
        self._nodes[action_id] = node
        self._parent_to_actions[parent_id].append(action_id)
        logger.info(
            "Registered child action %s (agent=%s, type=%s, contingency=%s) under parent %s",
            action_id, child_agent, action_type, contingency.value, parent_id
        )
        return node

    def get_nodes(self, parent_id: str) -> List[CausalNode]:
        """Get all child action nodes under a parent in registration order."""
        action_ids = self._parent_to_actions.get(parent_id, [])
        return [self._nodes[aid] for aid in action_ids if aid in self._nodes]

    def get_contingent_mutations(
        self,
        parent_id: str,
        preserve_action_ids: Optional[Set[str]] = None,
    ) -> List[str]:
        """Get mutation IDs that should be rolled back, preserving independent/specified actions."""
        preserve = preserve_action_ids or set()
        mutations = []
        for node in self.get_nodes(parent_id):
            if node.action_id in preserve:
                logger.info("Preserving action %s by explicit request", node.action_id)
                continue
            if node.contingency == CausalContingency.INDEPENDENT:
                logger.info("Preserving action %s due to INDEPENDENT contingency", node.action_id)
                continue
            if node.mutation_id and node.status == "EXECUTED":
                mutations.append(node.mutation_id)
        return mutations

    def rollback_contingent_actions(
        self,
        parent_id: str,
        harness: Any,
        reason: str = "Rollback contingent hypothesis tree",
        preserve_action_ids: Optional[Set[str]] = None,
    ) -> Any:
        """Execute atomic/grouped rollback of contingent mutations while preserving independent ones."""
        preserve = preserve_action_ids or set()
        nodes = self.get_nodes(parent_id)

        group = RecoveryTransactionGroup(group_id=f"grp_tree_{parent_id}", description=reason)
        mutation_to_action: Dict[str, CausalNode] = {}

        for node in nodes:
            if node.action_id in preserve or node.contingency == CausalContingency.INDEPENDENT:
                node.status = "PRESERVED"
                continue
            if node.mutation_id and node.status == "EXECUTED":
                group.add_mutation(node.mutation_id)
                mutation_to_action[node.mutation_id] = node

        group.commit()
        result = group.revert(harness, reason=reason)

        for mid in result.reverted_mutation_ids:
            if mid in mutation_to_action:
                mutation_to_action[mid].status = "REVERTED"

        return result
