"""State equivalence validator under declared contract policies."""

from __future__ import annotations
import json
from typing import Any, Dict, List, Tuple
from evoundo.core.state import HarnessState
from evoundo.effects.contracts import EffectCategory, EffectContract, EquivalencePolicy
from evoundo.effects.diff import StateDiffer


class StateEquivalenceChecker:
    """Evaluates whether recovered state s_rec is observationally equivalent to s_orig under C_e."""

    @classmethod
    def check_equivalence(
        cls,
        state_a: HarnessState,
        state_b: HarnessState,
        contract: EffectContract,
    ) -> Tuple[bool, List[str], float]:
        """Check equivalence under contract policies.
        
        Returns:
            (is_equivalent, divergence_reasons, similarity_score)
        """
        divergences: List[str] = []

        # 1. Diff states
        effects = StateDiffer.diff(state_a, state_b)
        if not effects:
            return True, [], 1.0

        for eff in effects:
            policy = contract.get_policy(eff.category)
            is_equiv = cls._evaluate_policy(eff.category, eff.old_value, eff.new_value, policy)
            if not is_equiv:
                divergences.append(
                    f"[{eff.category.value}] {eff.target} diverged ({eff.op_type.value}): "
                    f"orig={eff.old_value} vs rec={eff.new_value} (policy={policy.value})"
                )

        total_elements = max(1, len(state_a.config) + len(state_a.tools) + len(state_a.middleware) + len(state_a.files))
        similarity_score = max(0.0, 1.0 - (len(divergences) / total_elements))

        return len(divergences) == 0, divergences, similarity_score

    @classmethod
    def _evaluate_policy(
        cls,
        category: EffectCategory,
        val_a: Any,
        val_b: Any,
        policy: EquivalencePolicy,
    ) -> bool:
        if policy == EquivalencePolicy.EXACT:
            return val_a == val_b

        if policy == EquivalencePolicy.ORDER_INSENSITIVE:
            if isinstance(val_a, dict) and isinstance(val_b, dict):
                return val_a == val_b
            if isinstance(val_a, list) and isinstance(val_b, list):
                return sorted(str(x) for x in val_a) == sorted(str(x) for x in val_b)

        if policy == EquivalencePolicy.SET_EQUIVALENT:
            if isinstance(val_a, (list, set, tuple)) and isinstance(val_b, (list, set, tuple)):
                return set(val_a) == set(val_b)

        if policy == EquivalencePolicy.NORMALIZED:
            str_a = json.dumps(val_a, sort_keys=True, default=str).strip()
            str_b = json.dumps(val_b, sort_keys=True, default=str).strip()
            return str_a == str_b

        return val_a == val_b
