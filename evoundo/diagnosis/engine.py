"""Structured Diagnostic Engine providing D0 (coarse) and D1 (exact-address) recovery analysis."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set
from evoundo.effects.contracts import Effect, EffectAuditReport, EffectCategory
from evoundo.verification.counterfactual import VerificationResult


class FailureType(str, Enum):
    """Formal taxonomy of recovery and self-evolution failure modes."""
    WRONG_PRIOR_VALUE = "wrong_prior_value"                        # Hardcoded or incorrect assumed prior value
    MISSING_PRIOR_EXISTENCE_STATE = "missing_prior_existence_state"# Failed to check if key/tool/resource pre-existed
    INCOMPLETE_WITNESS = "incomplete_witness"                      # Information destroyed without capture in witness
    INCOMPLETE_RECOVERY = "incomplete_recovery"                    # Recovery omitted one or more modified surfaces
    OVER_RECOVERY = "over_recovery"                                # Recovery deleted pre-existing baseline state
    HIDDEN_EFFECT = "hidden_effect"                                # Forward mutation touched undeclared resources
    RESOURCE_LEAK = "resource_leak"                                # Spawned resource was not terminated/released
    NON_IDEMPOTENT_RECOVERY = "non_idempotent_recovery"            # Recovery invocation damages state when repeated
    CONTRACT_MISMATCH = "contract_mismatch"                        # Declared contract violates observed behavior
    EXECUTION_ERROR = "execution_error"                            # Exception raised during forward or recovery execution
    UNKNOWN = "unknown"


@dataclass
class RepairAction:
    """Structured recommendation for repairing mutation, recovery, witness, or contract."""
    target_component: str  # "contract", "witness", "recovery", "mutation"
    action_type: str       # "declare_effect", "capture_state", "condition_on_existence", "add_cleanup"
    target_key: str
    surface: str = "config"
    description: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_component": self.target_component,
            "action_type": self.action_type,
            "target_key": self.target_key,
            "surface": self.surface,
            "description": self.description,
            "details": self.details,
        }


@dataclass
class DiagnosticReport:
    """Diagnostic report capturing failure root causes and repair actions across D0/D1 feedback modes."""
    mutation_id: str
    failure_labels: List[FailureType] = field(default_factory=list)
    hidden_effects: List[str] = field(default_factory=list)
    residual_divergences: List[str] = field(default_factory=list)
    leaked_resources: List[str] = field(default_factory=list)
    recommended_repairs: List[RepairAction] = field(default_factory=list)
    execution_error: Optional[str] = None
    trials_passed: int = 0
    total_trials: int = 0

    @property
    def is_clean(self) -> bool:
        return len(self.failure_labels) == 0 and len(self.hidden_effects) == 0 and not self.execution_error

    def to_d0_prompt(self) -> str:
        """Format D0 (Coarse Subsystem) feedback without disclosing exact state addresses."""
        subsystems = set()
        for h in self.hidden_effects:
            subsystems.add(h.split(":")[0] if ":" in h else "general")
        for div in self.residual_divergences:
            subsystems.add(div.split("]")[0].replace("[", "").strip() if "]" in div else "general")
        
        labels_str = ", ".join(f.value for f in self.failure_labels) or "VERIFICATION_FAILURE"
        subsystems_str = ", ".join(sorted(subsystems)) or "harness_runtime"
        return (
            f"[D0 Coarse Feedback]\n"
            f"Diagnosis: {labels_str}\n"
            f"Affected Subsystems: {subsystems_str}\n"
            f"Guidance: Review declared contract coverage and ensure all pre-existing states are preserved."
        )

    def to_d1_prompt(self) -> str:
        """Format D1 (Exact Address) feedback with canonical state addresses and detailed traces."""
        lines = ["[D1 Exact Address Feedback]"]
        if self.failure_labels:
            lines.append(f"Failure Classification: {', '.join(f.value for f in self.failure_labels)}")
        if self.hidden_effects:
            lines.append("Undeclared Hidden Effects (Contract Violations):")
            for h in self.hidden_effects:
                lines.append(f"  - {h}")
        if self.residual_divergences:
            lines.append("Residual State Divergences:")
            for d in self.residual_divergences:
                lines.append(f"  - {d}")
        if self.recommended_repairs:
            lines.append("Recommended Repair Actions:")
            for r in self.recommended_repairs:
                lines.append(f"  - [{r.target_component.upper()}] {r.action_type} on '{r.target_key}': {r.description}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "failure_labels": [f.value for f in self.failure_labels],
            "hidden_effects": self.hidden_effects,
            "residual_divergences": self.residual_divergences,
            "leaked_resources": self.leaked_resources,
            "recommended_repairs": [r.to_dict() for r in self.recommended_repairs],
            "execution_error": self.execution_error,
            "trials_passed": self.trials_passed,
            "total_trials": self.total_trials,
        }


class DiagnosticEngine:
    """Performs deep root-cause analysis on mutation execution, contract audit, and recovery traces."""

    @classmethod
    def diagnose(
        cls,
        mutation_id: str,
        execution_success: bool = True,
        execution_error: Optional[str] = None,
        audit_report: Optional[EffectAuditReport] = None,
        verification_result: Optional[VerificationResult] = None,
    ) -> DiagnosticReport:
        """Analyze failure traces and produce a structured DiagnosticReport."""
        labels: Set[FailureType] = set()
        hidden: List[str] = []
        residuals: List[str] = []
        leaks: List[str] = []
        repairs: List[RepairAction] = []

        # 1. Check Execution Errors
        if not execution_success or execution_error:
            labels.add(FailureType.EXECUTION_ERROR)
            repairs.append(RepairAction(
                target_component="mutation",
                action_type="fix_syntax_or_runtime_error",
                target_key="forward_mutation",
                description=f"Runtime error during execution: {execution_error}",
            ))

        # 2. Check Contract Violations / Undeclared Hidden Effects
        if audit_report and not audit_report.is_clean:
            labels.add(FailureType.HIDDEN_EFFECT)
            for cat, target in audit_report.hidden_effects:
                hidden_str = f"[{cat.value}] {target}"
                hidden.append(hidden_str)
                repairs.append(RepairAction(
                    target_component="contract",
                    action_type="declare_effect",
                    target_key=target,
                    surface=cat.value,
                    description=f"Expand effect contract to declare [{cat.value}] {target}",
                ))

        # 3. Check Recovery Divergences
        if verification_result and not verification_result.is_verified:
            for div in verification_result.divergence_details:
                residuals.append(div)
                div_lower = div.lower()

                if "resource" in div_lower or "socket" in div_lower:
                    labels.add(FailureType.RESOURCE_LEAK)
                    repairs.append(RepairAction(
                        target_component="recovery",
                        action_type="add_cleanup",
                        target_key="managed_resource",
                        surface="resources",
                        description="Add explicit teardown or close_resource operation",
                    ))
                elif "diverged" in div_lower and "orig=none" in div_lower:
                    labels.add(FailureType.MISSING_PRIOR_EXISTENCE_STATE)
                    repairs.append(RepairAction(
                        target_component="witness",
                        action_type="condition_on_existence",
                        target_key=div.split(" ")[1] if " " in div else "target",
                        description="Capture pre-mutation existence state to remove newly added element upon recovery",
                    ))
                elif "diverged" in div_lower:
                    labels.add(FailureType.WRONG_PRIOR_VALUE)
                    repairs.append(RepairAction(
                        target_component="witness",
                        action_type="capture_state",
                        target_key=div.split(" ")[1] if " " in div else "target",
                        description="Capture pre-mutation value in witness before forward mutation",
                    ))
                else:
                    labels.add(FailureType.INCOMPLETE_RECOVERY)
                    repairs.append(RepairAction(
                        target_component="recovery",
                        action_type="add_missing_cleanup",
                        target_key=div,
                        description=f"Add missing inverse recovery step for {div}",
                    ))

        if not labels and (not execution_success or (verification_result and not verification_result.is_verified)):
            labels.add(FailureType.UNKNOWN)

        return DiagnosticReport(
            mutation_id=mutation_id,
            failure_labels=sorted(list(labels), key=lambda x: x.value),
            hidden_effects=hidden,
            residual_divergences=residuals,
            leaked_resources=leaks,
            recommended_repairs=repairs,
            execution_error=execution_error,
            trials_passed=verification_result.trials_passed if verification_result else 0,
            total_trials=verification_result.total_trials if verification_result else 0,
        )
