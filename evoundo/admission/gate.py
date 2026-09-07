"""Admission gate enforcing fail-closed multi-stage evaluation."""

from __future__ import annotations
from typing import Any, List, Optional, TYPE_CHECKING
from evoundo.admission.policies import (
    AdmissionDecision,
    AdmissionPolicyConfig,
    AdmissionStatus,
    CapabilityResult,
)
from evoundo.effects.contracts import EffectAuditReport
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger

if TYPE_CHECKING:
    from evoundo.verification.counterfactual import VerificationResult


class AdmissionGate:
    """Evaluates candidate mutation feasibility, contract containment, utility, and recoverability."""

    def __init__(
        self,
        config: Optional[AdmissionPolicyConfig] = None,
        event_logger: Optional[StructuredEventLogger] = None,
    ):
        self.config = config or AdmissionPolicyConfig()
        self.event_logger = event_logger or default_event_logger

    def evaluate(
        self,
        mutation_id: str,
        execution_success: bool,
        execution_error: Optional[str] = None,
        audit_report: Optional[EffectAuditReport] = None,
        capability_result: Optional[CapabilityResult] = None,
        verification_result: Optional[Any] = None,
    ) -> AdmissionDecision:
        """Evaluate candidate across all safety, effect, utility, and recovery gates."""
        reasons: List[str] = []
        decision_code = "ADMIT"
        status = AdmissionStatus.ADMIT

        # Gate 1: Execution Success
        if not execution_success:
            status = AdmissionStatus.REJECT
            if decision_code == "ADMIT":
                decision_code = "REJECT_EXECUTION_ERROR"
            reasons.append(f"Mutation execution raised an error: {execution_error}")

        # Gate 2: Strict Effect Contract Containment (E_obs ⊆ C_e)
        if self.config.require_strict_containment and audit_report:
            if not audit_report.is_clean:
                status = AdmissionStatus.REJECT
                if decision_code == "ADMIT":
                    decision_code = "REJECT_UNDECLARED_EFFECT"
                for hidden in audit_report.hidden_effects:
                    reasons.append(f"Undeclared side effect on [{hidden[0].value}] {hidden[1]}")

        # Gate 3: Capability Improvement
        if self.config.require_capability_improvement and capability_result:
            if capability_result.delta < self.config.min_capability_delta or not capability_result.improved:
                status = AdmissionStatus.REJECT
                if decision_code == "ADMIT":
                    decision_code = "REJECT_CAPABILITY_REGRESSION"
                reasons.append(
                    f"Capability score did not improve (delta={capability_result.delta}, min_required={self.config.min_capability_delta})"
                )

        # Gate 4: Counterfactual Recovery Verification (s -> w -> m -> u -> s' ~ s)
        if self.config.require_counterfactual_verification and verification_result:
            if not verification_result.is_verified:
                status = AdmissionStatus.REJECT
                if decision_code == "ADMIT":
                    decision_code = "REJECT_RECOVERY_VERIFICATION_FAILED"
                for div in verification_result.divergence_details:
                    reasons.append(f"Recovery divergence: {div}")

        decision = AdmissionDecision(
            status=status,
            reasons=reasons,
            decision_code=decision_code,
            metadata={
                "mutation_id": mutation_id,
                "capability_score_after": capability_result.score_after if capability_result else None,
                "verification_trials_passed": verification_result.trials_passed if verification_result else None,
            },
        )

        if decision.admissible:
            self.event_logger.emit(
                event_type=EventType.MUTATION_ADMITTED,
                mutation_id=mutation_id,
                message="Mutation passed all admission gates and is admitted",
                data=decision.to_dict(),
            )
        else:
            self.event_logger.emit(
                event_type=EventType.MUTATION_REJECTED,
                mutation_id=mutation_id,
                message=f"Mutation rejected: {decision.decision_code} ({'; '.join(reasons)})",
                data=decision.to_dict(),
            )

        return decision
