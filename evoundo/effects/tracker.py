"""Independent effect tracker and contract conformance auditor."""

from __future__ import annotations
from typing import List, Optional, Set, Tuple
from evoundo.core.state import HarnessState
from evoundo.effects.contracts import (
    Effect,
    EffectAuditReport,
    EffectCategory,
    EffectContract,
)
from evoundo.effects.diff import StateDiffer
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger


class EffectTracker:
    """Independently observes state transitions, diffs states, and verifies contract containment: E_obs ⊆ C_e."""

    def __init__(self, event_logger: Optional[StructuredEventLogger] = None):
        self.event_logger = event_logger or default_event_logger

    def audit(
        self,
        pre_state: HarnessState,
        post_state: HarnessState,
        contract: EffectContract,
        mutation_id: Optional[str] = None,
    ) -> EffectAuditReport:
        """Independently audit state change against declared effect contract."""
        detailed_effects = StateDiffer.diff(pre_state, post_state)
        observed_pairs: Set[Tuple[EffectCategory, str]] = {e.key_tuple for e in detailed_effects}
        declared_pairs = contract.all_declared()

        hidden_effects = observed_pairs - declared_pairs
        unrealized_effects = declared_pairs - observed_pairs
        is_clean = len(hidden_effects) == 0

        # Emit observability events
        for effect in detailed_effects:
            self.event_logger.emit(
                event_type=EventType.EFFECT_OBSERVED,
                mutation_id=mutation_id,
                message=f"Observed effect on [{effect.category.value}] {effect.target} ({effect.op_type.value})",
                data=effect.to_dict(),
            )

        if not is_clean:
            for hidden in hidden_effects:
                self.event_logger.emit(
                    event_type=EventType.UNDECLARED_EFFECT_DETECTED,
                    mutation_id=mutation_id,
                    message=f"Undeclared effect detected on [{hidden[0].value}] {hidden[1]}",
                    data={"category": hidden[0].value, "target": hidden[1]},
                )

        return EffectAuditReport(
            declared_effects=declared_pairs,
            observed_effects=observed_pairs,
            hidden_effects=hidden_effects,
            unrealized_effects=unrealized_effects,
            is_clean=is_clean,
            detailed_effects=detailed_effects,
        )

    def validate_containment(
        self,
        pre_state: HarnessState,
        post_state: HarnessState,
        contract: EffectContract,
        mutation_id: Optional[str] = None,
    ) -> Tuple[bool, List[str], EffectAuditReport]:
        """Verify that observed effects strictly conform to the contract (E_obs ⊆ C_e)."""
        report = self.audit(pre_state, post_state, contract, mutation_id=mutation_id)
        if not report.is_clean:
            reasons = [
                f"Undeclared mutation on [{cat.value}] {target}"
                for cat, target in sorted(report.hidden_effects, key=lambda x: (x[0].value, x[1]))
            ]
            return False, reasons, report
        return True, [], report
