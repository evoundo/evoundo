"""Counterfactual verifier supporting Smoke Verification and Paper-Faithful Verification (Qdev=10, Qhid=40)."""

from __future__ import annotations
import copy
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from evoundo.core.state import (
    FileDescriptor,
    HarnessState,
    ListenerDescriptor,
    MiddlewareDescriptor,
    ResourceDescriptor,
    ToolDescriptor,
)
from evoundo.effects.contracts import EffectAuditReport, EffectContract
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger
from evoundo.recovery.engine import RecoveryEngine
from evoundo.recovery.operations import RecoveryProgram
from evoundo.recovery.snapshots import RecoveryStrategy
from evoundo.verification.equivalence import StateEquivalenceChecker
from evoundo.verification.stats import wilson_score_lower_bound
from evoundo.witness.manager import WitnessManager
from evoundo.witness.stores import Witness


@dataclass
class VerificationResult:
    """Rigorous result of counterfactual recovery verification across trial splits."""
    is_verified: bool
    counterfactual_passed: bool
    mode: str = "smoke"                  # "smoke" | "paper_faithful"
    trials_passed: int = 0
    total_trials: int = 0
    empirical_rate: float = 1.0
    wilson_lcb: float = 0.0              # True 95% Wilson Score Lower Confidence Bound
    tau_R: float = 0.85                  # Admission threshold for paper-faithful mode
    dev_passed: int = 0
    dev_total: int = 0
    dev_rate: float = 1.0
    hidden_passed: int = 0
    hidden_total: int = 0
    hidden_rate: float = 1.0
    divergence_details: List[str] = field(default_factory=list)
    similarity_score: float = 1.0
    recovery_latency_ms: float = 0.0
    audit_report: Optional[EffectAuditReport] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_verified": self.is_verified,
            "mode": self.mode,
            "counterfactual_passed": self.counterfactual_passed,
            "trials_passed": self.trials_passed,
            "total_trials": self.total_trials,
            "empirical_rate": round(self.empirical_rate, 4),
            "wilson_lcb": round(self.wilson_lcb, 6),
            "tau_R": self.tau_R,
            "dev_passed": self.dev_passed,
            "dev_total": self.dev_total,
            "hidden_passed": self.hidden_passed,
            "hidden_total": self.hidden_total,
            "divergence_details": self.divergence_details,
            "similarity_score": round(self.similarity_score, 4),
            "recovery_latency_ms": round(self.recovery_latency_ms, 3),
            "metadata": self.metadata,
        }


class CounterfactualVerifier:
    """Evaluates counterfactual recoverability across synthetic base and perturbed state distributions."""

    def __init__(
        self,
        event_logger: Optional[StructuredEventLogger] = None,
        tau_R: float = 0.85,
    ):
        self.event_logger = event_logger or default_event_logger
        self.tau_R = tau_R

    def generate_counterfactual_states(
        self,
        base_state: HarnessState,
        mode: str = "smoke",
    ) -> Tuple[List[HarnessState], List[HarnessState]]:
        """Generate structured counterfactual pre-states partitioned into development and hidden sets."""
        if mode == "smoke":
            # Fast 3-state smoke evaluation (1 base + 2 perturbations)
            dev_states: List[HarnessState] = [base_state.clone()]
            for i in range(2):
                s = base_state.clone()
                s.set_config(f"_smoke_probe_key_{i}", f"val_{i}")
                s.register_tool(ToolDescriptor(
                    name=f"_smoke_probe_tool_{i}",
                    description="Smoke probe tool",
                    fn=lambda x: x,
                ))
                dev_states.append(s)
            return dev_states, []

        elif mode == "paper_faithful":
            # Paper-faithful evaluation: |Q_dev| = 10, |Q_hid| = 40 (20 IID, 20 OOD)
            dev_states = []
            hidden_states = []

            # 1. Generate Q_dev (10 states)
            for i in range(10):
                s = base_state.clone()
                s.set_config(f"dev_config_{i}", i * 10)
                if i % 2 == 0:
                    s.register_tool(ToolDescriptor(name=f"dev_tool_{i}", description=f"Dev Tool {i}", fn=lambda x: x))
                if i % 3 == 0:
                    s.add_middleware(MiddlewareDescriptor(id=f"dev_mw_{i}", name=f"DevMW_{i}", priority=50 + i))
                dev_states.append(s)

            # 2. Generate Q_hid IID (20 states)
            for i in range(20):
                s = base_state.clone()
                s.set_config(f"hid_iid_cfg_{i}", f"iid_val_{i}")
                s.register_tool(ToolDescriptor(name=f"hid_iid_tool_{i}", description=f"IID Tool {i}", fn=lambda x: x))
                s.write_file(f"iid/file_{i}.txt", f"content_{i}")
                hidden_states.append(s)

            # 3. Generate Q_hid OOD (20 boundary & multi-surface collision states)
            for i in range(20):
                s = base_state.clone()
                # Boundary collisions (pre-existing keys with same or different types)
                s.set_config("cache_ttl_sec", 100 + i)
                s.set_config("timeout_sec", 5 + i * 2)
                s.register_tool(ToolDescriptor(name="cache_mw", description="Colliding tool name", fn=lambda x: x))
                s.register_resource(ResourceDescriptor(id=f"socket:port_{9000 + i}", resource_type="socket"))
                s.add_listener("on_query", ListenerDescriptor(id=f"ood_listener_{i}", event="on_query"))
                hidden_states.append(s)

            return dev_states, hidden_states

        else:
            raise ValueError(f"Unknown verification mode: {mode}")

    def verify(
        self,
        base_state: HarnessState,
        forward_mutation_fn: Callable[[HarnessState], Any],
        contract: EffectContract,
        witness_manager: WitnessManager,
        recovery_engine: RecoveryEngine,
        recovery_program: RecoveryProgram,
        mutation_id: str,
        mode: str = "smoke",
    ) -> VerificationResult:
        """Execute counterfactual trial suite under either Smoke or Paper-Faithful mode."""
        dev_states, hidden_states = self.generate_counterfactual_states(base_state, mode=mode)

        all_divergences: List[str] = []
        total_time_ms = 0.0

        # --- Evaluate Development Split ---
        dev_passed = 0
        for idx, test_state in enumerate(dev_states):
            trial_id = f"{mutation_id}_dev_{idx}"
            s_init = test_state.clone()
            witness = witness_manager.capture_for_contract(test_state, contract, mutation_id=trial_id)
            cand_state = test_state.clone()

            try:
                forward_mutation_fn(cand_state)
            except Exception as e:
                all_divergences.append(f"Dev Trial {idx}: Forward mutation exception: {str(e)}")
                continue

            t0 = time.perf_counter()
            try:
                rec_state = recovery_engine.recover(
                    current_state=cand_state,
                    witness=witness,
                    program=recovery_program,
                    strategy=RecoveryStrategy.EVOUNDO_RECOVERY,
                    mutation_id=trial_id,
                )
            except Exception as e:
                all_divergences.append(f"Dev Trial {idx}: Recovery exception: {str(e)}")
                continue
            finally:
                total_time_ms += (time.perf_counter() - t0) * 1000.0

            is_eq, divs, _ = StateEquivalenceChecker.check_equivalence(s_init, rec_state, contract)
            if is_eq:
                dev_passed += 1
            else:
                all_divergences.extend([f"Dev Trial {idx}: {d}" for d in divs])

        # --- Evaluate Hidden Split (if paper-faithful mode) ---
        hidden_passed = 0
        if hidden_states:
            for idx, test_state in enumerate(hidden_states):
                trial_id = f"{mutation_id}_hid_{idx}"
                s_init = test_state.clone()
                witness = witness_manager.capture_for_contract(test_state, contract, mutation_id=trial_id)
                cand_state = test_state.clone()

                try:
                    forward_mutation_fn(cand_state)
                except Exception as e:
                    all_divergences.append(f"Hidden Trial {idx}: Forward mutation exception: {str(e)}")
                    continue

                t0 = time.perf_counter()
                try:
                    rec_state = recovery_engine.recover(
                        current_state=cand_state,
                        witness=witness,
                        program=recovery_program,
                        strategy=RecoveryStrategy.EVOUNDO_RECOVERY,
                        mutation_id=trial_id,
                    )
                except Exception as e:
                    all_divergences.append(f"Hidden Trial {idx}: Recovery exception: {str(e)}")
                    continue
                finally:
                    total_time_ms += (time.perf_counter() - t0) * 1000.0

                is_eq, divs, _ = StateEquivalenceChecker.check_equivalence(s_init, rec_state, contract)
                if is_eq:
                    hidden_passed += 1
                else:
                    all_divergences.extend([f"Hidden Trial {idx}: {d}" for d in divs])

        total_trials = len(dev_states) + len(hidden_states)
        total_passed = dev_passed + hidden_passed
        empirical_rate = total_passed / total_trials if total_trials > 0 else 0.0

        if mode == "paper_faithful":
            # Compute rigorous Wilson LCB strictly on the hidden evaluation set (Q_hid = 40)
            wilson_lcb = wilson_score_lower_bound(hidden_passed, len(hidden_states), confidence=0.95)
            # Admission requires Q_dev perfect pass and Wilson LCB on Q_hid >= tau_R (0.85)
            is_verified = (dev_passed == len(dev_states)) and (wilson_lcb >= self.tau_R) and (len(all_divergences) == 0)
        else:
            # Smoke mode: 3/3 pass required, compute true mathematical LCB (approx 0.439 for 3/3)
            wilson_lcb = wilson_score_lower_bound(total_passed, total_trials, confidence=0.95)
            is_verified = (total_passed == total_trials) and (len(all_divergences) == 0)

        result = VerificationResult(
            is_verified=is_verified,
            counterfactual_passed=(total_passed == total_trials),
            mode=mode,
            trials_passed=total_passed,
            total_trials=total_trials,
            empirical_rate=empirical_rate,
            wilson_lcb=wilson_lcb,
            tau_R=self.tau_R,
            dev_passed=dev_passed,
            dev_total=len(dev_states),
            dev_rate=dev_passed / len(dev_states) if dev_states else 1.0,
            hidden_passed=hidden_passed,
            hidden_total=len(hidden_states),
            hidden_rate=hidden_passed / len(hidden_states) if hidden_states else 0.0,
            divergence_details=all_divergences,
            similarity_score=empirical_rate,
            recovery_latency_ms=total_time_ms / total_trials if total_trials > 0 else 0.0,
        )

        self.event_logger.emit(
            event_type=EventType.RECOVERY_VERIFIED,
            mutation_id=mutation_id,
            message=(
                f"Recovery Verification ({mode}): {'PASSED' if is_verified else 'FAILED'} "
                f"({total_passed}/{total_trials} passed, Wilson95% LCB={wilson_lcb:.4f})"
            ),
            data=result.to_dict(),
        )

        return result
