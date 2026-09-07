"""EvoUndo Harness runtime and self-evolution control plane."""

from __future__ import annotations
import copy
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("evoundo.core.harness")

from evoundo.admission.gate import AdmissionGate
from evoundo.admission.policies import (
    AdmissionDecision,
    AdmissionPolicyConfig,
    AdmissionStatus,
    CapabilityResult,
)
from evoundo.compiler.candidate_compiler import CandidateCompiler
from evoundo.compiler.schemas import DeclarativeMutationSpec
from evoundo.core.mutation import MutationBuilder, MutationProposal, ProposalStatus
from evoundo.core.state import HarnessState
from evoundo.core.versioning import VersionLineageGraph
from evoundo.diagnosis.engine import DiagnosticEngine, DiagnosticReport
from evoundo.diagnosis.synthesizer import RecoverySynthesizer
from evoundo.effects.contracts import Effect, EffectAuditReport, EffectCategory, EffectContract
from evoundo.effects.tracker import EffectTracker
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger
from evoundo.recovery.engine import RecoveryEngine
from evoundo.recovery.operations import RecoveryProgram
from evoundo.recovery.snapshots import RecoveryStrategy, SnapshotStore
from evoundo.registry.mutation_registry import MutationRecord, MutationRegistry
from evoundo.verification.counterfactual import CounterfactualVerifier, VerificationResult
from evoundo.witness.manager import WitnessManager
from evoundo.witness.stores import InMemoryWitnessStore, Witness


@dataclass
class EvolutionResult:
    """Developer-facing outcome of a self-evolution attempt."""
    status: str                         # "ADMITTED" | "REJECTED"
    capability_delta: float             # delta score
    recoverability: float               # verification score [0, 1]
    effects: List[Effect]               # observed dynamic effects
    recovery_plan: List[str]            # list of recovery op descriptions
    mutation_id: str
    decision: AdmissionDecision
    record: Optional[MutationRecord] = None
    diagnostic_report: Optional[DiagnosticReport] = None
    retries_attempted: int = 0

    @property
    def admitted(self) -> bool:
        return self.status == "ADMITTED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "admitted": self.admitted,
            "capability_delta": round(self.capability_delta, 4),
            "recoverability": round(self.recoverability, 4),
            "mutation_id": self.mutation_id,
            "effects": [e.to_dict() for e in self.effects],
            "recovery_plan": self.recovery_plan,
            "retries_attempted": self.retries_attempted,
            "decision": self.decision.to_dict(),
            "diagnostic_report": self.diagnostic_report.to_dict() if self.diagnostic_report else None,
        }


def _is_redis_target(target: str) -> bool:
    return isinstance(target, str) and target.startswith("redis://")


def _parse_redis_target(target: str) -> Tuple[str, str, Optional[str]]:
    """Returns (base_key, target_type, sub_id)."""
    if "#counter" in target:
        base = target.split("#counter")[0]
        return (base, "counter", None)
    if "/field/" in target:
        base, sub = target.split("/field/", 1)
        return (base, "field", sub)
    if target.endswith("/fields"):
        base = target[:-len("/fields")]
        return (base, "field_all", None)
    if "/item/" in target:
        base, sub = target.split("/item/", 1)
        return (base, "item", sub)
    if target.endswith("/items"):
        base = target[:-len("/items")]
        return (base, "item_all", None)
    if "/member/" in target:
        base, sub = target.split("/member/", 1)
        return (base, "member", sub)
    if target.endswith("/members"):
        base = target[:-len("/members")]
        return (base, "member_all", None)
    return (target, "whole_key", None)


def _is_commutative_counter_mutation(rec: Any) -> bool:
    """Check if mutation is purely commutative additions/subtractions."""
    if not rec or not getattr(rec, "recovery_program", None):
        return False
    ops = getattr(rec.recovery_program, "operations", [])
    if not ops:
        return False
    for op in ops:
        op_name = getattr(op, "operation", "") or ""
        op_name = str(op_name).upper()
        if op_name not in ("INCRBY", "DECRBY"):
            return False
    return True


def _check_target_conflict(
    cat1: Any,
    target1: str,
    rec1: Any,
    cat2: Any,
    target2: str,
    rec2: Any,
) -> Optional[str]:
    cat1_val = cat1.value if hasattr(cat1, "value") else str(cat1)
    cat2_val = cat2.value if hasattr(cat2, "value") else str(cat2)
    if cat1_val != cat2_val:
        return None

    if not _is_redis_target(target1) or not _is_redis_target(target2):
        from evoundo.effects.address import ResourceAddress
        addr1 = ResourceAddress.parse(target1)
        addr2 = ResourceAddress.parse(target2)
        # If either target has a recognized scheme or path segments
        if addr1.scheme != "general" or addr2.scheme != "general" or "/" in target1 or "/" in target2:
            conflict = addr1.conflicts_with(addr2)
            if conflict:
                return f"Hierarchical conflict [{cat1_val}]: {conflict} by active Mutation '{rec2.mutation_id}' ('{rec2.description}')"
            return None

        if target1 == target2:
            return f"Target [{cat1_val}] '{target1}' was subsequently modified by active Mutation '{rec2.mutation_id}' ('{rec2.description}')"
        return None

    base1, type1, sub1 = _parse_redis_target(target1)
    base2, type2, sub2 = _parse_redis_target(target2)

    if base1 != base2:
        return None

    # Both target the same Redis base key:
    # 1. Whole-key / container-wide collision
    if type1 in ("whole_key", "field_all", "item_all", "member_all") or type2 in ("whole_key", "field_all", "item_all", "member_all"):
        return (
            f"Target [{cat1_val}] '{target1}' has whole-key/container conflict with "
            f"active Mutation '{rec2.mutation_id}' on '{target2}'"
        )

    # 2. Type mismatch on same key
    if type1 != type2:
        return (
            f"Target [{cat1_val}] '{target1}' has type mismatch conflict with "
            f"active Mutation '{rec2.mutation_id}' on '{target2}'"
        )

    # 3. Counters
    if type1 == "counter":
        if _is_commutative_counter_mutation(rec1) and _is_commutative_counter_mutation(rec2):
            return None  # Commutative: allow algebraic compensation
        return (
            f"Counter target [{cat1_val}] '{target1}' was subsequently modified by non-commutative "
            f"active Mutation '{rec2.mutation_id}' ('{rec2.description}')"
        )

    # 4. Hash fields
    if type1 == "field":
        if sub1 == sub2:
            return (
                f"Hash field [{cat1_val}] '{target1}' was subsequently modified by "
                f"active Mutation '{rec2.mutation_id}' on the same field"
            )
        return None  # Orthogonal fields

    # 5. List items
    if type1 == "item":
        if sub1 == sub2:
            return (
                f"List item [{cat1_val}] '{target1}' was subsequently modified by "
                f"active Mutation '{rec2.mutation_id}' on the same item"
            )
        return None  # Distinct items

    # 6. Set members
    if type1 == "member":
        if sub1 == sub2:
            return (
                f"Set member [{cat1_val}] '{target1}' was subsequently modified by "
                f"active Mutation '{rec2.mutation_id}' on the same member"
            )
        return None  # Distinct members

    return f"Target [{cat1_val}] '{target1}' conflicts with '{target2}'"


class EvoUndoHarness:
    """Production reference runtime harness providing self-evolution with verified recoverability."""

    def __init__(
        self,
        initial_state: Optional[HarnessState] = None,
        registry_path: Optional[str] = None,
        policy_config: Optional[AdmissionPolicyConfig] = None,
        event_logger: Optional[StructuredEventLogger] = None,
        reconciler: Optional[Any] = None,
    ):
        self.event_logger = event_logger or default_event_logger
        self.snapshot_store = SnapshotStore()
        self.witness_manager = WitnessManager(store=InMemoryWitnessStore(), event_logger=self.event_logger)
        self.recovery_engine = RecoveryEngine(snapshot_store=self.snapshot_store, event_logger=self.event_logger)
        self.effect_tracker = EffectTracker(event_logger=self.event_logger)
        self.counterfactual_verifier = CounterfactualVerifier(event_logger=self.event_logger)
        self.admission_gate = AdmissionGate(config=policy_config, event_logger=self.event_logger)
        self.mutation_registry = MutationRegistry(storage_path=registry_path)
        from evoundo.reconciliation.reconciler import MutationReconciler
        reg_storage = self.mutation_registry.storage_path
        if reg_storage:
            self.snapshot_path = os.path.splitext(reg_storage)[0] + "_state.json"
            j_path = os.path.splitext(reg_storage)[0] + "_journal.json"
        else:
            self.snapshot_path = None
            j_path = None
        self.reconciler = reconciler if reconciler is not None else MutationReconciler(journal_path=j_path, event_logger=self.event_logger)

        # Re-hydrate state from snapshot if starting with explicit registry_path
        if initial_state is None and reg_storage and self.snapshot_path and os.path.exists(self.snapshot_path):
            try:
                with open(self.snapshot_path, "r") as f:
                    snap_data = json.load(f)
                    self.current_state = HarnessState.from_dict(snap_data)
            except Exception as e:
                self.current_state = HarnessState()
        else:
            self.current_state = initial_state or HarnessState()

        self.lineage = VersionLineageGraph(
            initial_version=1,
            initial_hash=self.current_state.canonical_hash(),
        )

        # Re-populate lineage nodes from persistent registry
        for r in self.mutation_registry.list_mutations():
            if r.status == "ACTIVE" and r.resulting_harness_version > 1:
                self.lineage.append_version(
                    new_version=r.resulting_harness_version,
                    parent_version=r.parent_harness_version,
                    mutation_id=r.mutation_id,
                    state_hash=self.current_state.canonical_hash(),
                    description=r.description,
                )

        self.memory_adapters: List[Any] = []

    def register_memory_adapter(self, adapter: Any) -> None:
        """Register an agent memory adapter for bi-directional world-state invalidation."""
        if hasattr(self, "memory_adapters") and adapter not in self.memory_adapters:
            self.memory_adapters.append(adapter)

    def _persist_state_snapshot(self) -> None:
        """Persist current active state snapshot to disk."""
        if hasattr(self, "snapshot_path") and self.snapshot_path and self.mutation_registry.storage_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.snapshot_path)), exist_ok=True)
                with open(self.snapshot_path, "w") as f:
                    json.dump(self.current_state.to_dict(), f, indent=2)
            except Exception as e:
                self.event_logger.emit(EventType.ERROR, message=f"Failed to persist state snapshot: {e}")

    def sync_from_disk(self) -> None:
        """Synchronize in-memory harness state and registry with persistent storage if updated."""
        if hasattr(self, "mutation_registry"):
            self.mutation_registry.sync()
        if hasattr(self, "snapshot_path") and os.path.exists(self.snapshot_path):
            try:
                with open(self.snapshot_path, "r") as f:
                    snap_data = json.load(f)
                    disk_version = snap_data.get("version", 1)
                    if disk_version != self.current_state.version:
                        self.current_state = HarnessState.from_dict(snap_data)
            except Exception:
                pass

    # --- Public State & Inspection API ---

    def state(self) -> HarnessState:
        """Get a clone of the current active harness state."""
        return self.current_state.clone()

    def status(self) -> Dict[str, Any]:
        """Summary of current harness version and active surfaces."""
        s = self.current_state
        return {
            "version": s.version,
            "state_hash": s.canonical_hash()[:12],
            "config_keys": sorted(list(s.config.keys())),
            "tools": sorted(list(s.tools.keys())),
            "middleware": [m.id for m in s.middleware if m.enabled],
            "event_listeners": {k: [l.id for l in v] for k, v in s.event_listeners.items()},
            "files": sorted(list(s.files.keys())),
            "resources": sorted(list(s.resources.keys())),
            "prompts": sorted(list(s.prompts.keys())),
            "admitted_mutations_count": len(self.mutation_registry.list_mutations()),
        }

    def history(self) -> List[Dict[str, Any]]:
        """Retrieve full audit history of all admitted mutations."""
        return self.mutation_registry.show_history()

    def inspect(self, mutation_id: str) -> Optional[MutationRecord]:
        """Inspect detailed metadata and recovery program for a mutation."""
        return self.mutation_registry.inspect_mutation(mutation_id)

    # --- High-Level Developer Evolution API ---

    def evolve(
        self,
        goal: str,
        mutate_fn: Optional[Callable[[MutationBuilder], None]] = None,
        spec: Optional[Union[DeclarativeMutationSpec, Dict[str, Any]]] = None,
        capability_evaluator: Optional[Callable[[HarnessState], CapabilityResult]] = None,
        budget: int = 3,
        diagnostic_mode: str = "D1",
        recovery_language: str = "L1",
        verification_mode: str = "smoke",
    ) -> EvolutionResult:
        """High-level end-to-end self-evolution with closed-loop diagnostic retry (budget B)."""
        current_proposal: Optional[MutationProposal] = None

        # 1. Build initial proposal from spec or mutate_fn
        if spec is not None:
            compiled, err = CandidateCompiler.compile_spec(spec, recovery_language=recovery_language)
            if err:
                decision = AdmissionDecision(
                    status=AdmissionStatus.REJECT,
                    decision_code="REJECT_COMPILATION_ERROR",
                    reasons=[err],
                )
                return EvolutionResult(
                    status="REJECTED",
                    capability_delta=0.0,
                    recoverability=0.0,
                    effects=[],
                    recovery_plan=[],
                    mutation_id="compilation_failed",
                    decision=decision,
                )
            current_proposal = compiled
        elif mutate_fn is not None:
            builder = MutationBuilder(harness=self, description=goal, proposer="agent")
            mutate_fn(builder)
            current_proposal = builder.build_proposal()
        else:
            # Deterministic reference mutation generator for common self-evolution goals
            builder = MutationBuilder(harness=self, description=goal, proposer="agent")
            if "retry" in goal.lower():
                from evoundo.core.state import MiddlewareDescriptor
                builder.add_middleware(MiddlewareDescriptor(id="retry_mw", name="RetryMiddleware", priority=10))
            elif "cache" in goal.lower() or "caching" in goal.lower():
                from evoundo.core.state import MiddlewareDescriptor
                builder.add_middleware(MiddlewareDescriptor(id="cache_mw", name="CacheMiddleware", priority=20))
                builder.set_config("cache_ttl_sec", 300)
            elif "tool" in goal.lower():
                from evoundo.core.state import ToolDescriptor
                tool_name = "custom_tool"
                builder.register_tool(ToolDescriptor(name=tool_name, description="Auto-evolved tool", fn=lambda x: x))
            else:
                builder.set_config("evolved_flag", True)
            current_proposal = builder.build_proposal()

        retries = 0
        last_decision: Optional[AdmissionDecision] = None
        last_record: Optional[MutationRecord] = None
        last_candidate_state: Optional[HarnessState] = None
        last_diagnosis: Optional[DiagnosticReport] = None
        last_audit_report: Optional[EffectAuditReport] = None

        while retries <= budget:
            decision, record, candidate_state, audit_rep, ver_res = self._evaluate_internal(
                current_proposal,
                capability_evaluator=capability_evaluator,
                diagnostic_mode=diagnostic_mode,
                recovery_language=recovery_language,
                verification_mode=verification_mode,
            )
            last_decision = decision
            last_record = record
            last_candidate_state = candidate_state
            last_audit_report = audit_rep

            if decision.admissible and record and candidate_state:
                # Commit to persistent state
                candidate_state.parent_version = self.current_state.version
                candidate_state.version = self.current_state.version + 1
                self.current_state = candidate_state

                # Append to version lineage
                self.lineage.append_version(
                    new_version=self.current_state.version,
                    parent_version=candidate_state.parent_version,
                    mutation_id=record.mutation_id,
                    state_hash=self.current_state.canonical_hash(),
                    description=record.description,
                )

                # Persist in registry
                self.mutation_registry.record_mutation(record)
                self._persist_state_snapshot()

                recovery_plan = [op.__class__.__name__ for op in record.recovery_program.operations]

                return EvolutionResult(
                    status="ADMITTED",
                    capability_delta=record.capability_delta,
                    recoverability=record.recovery_lcb,
                    effects=record.observed_effects,
                    recovery_plan=recovery_plan,
                    mutation_id=record.mutation_id,
                    decision=decision,
                    record=record,
                    retries_attempted=retries,
                )

            # Failure occurred: Diagnose and attempt synthesis repair if retries remaining
            retries += 1
            last_diagnosis = DiagnosticEngine.diagnose(
                mutation_id=current_proposal.mutation_id,
                execution_success=not (decision.decision_code == "REJECT_EXECUTION_ERROR"),
                execution_error="; ".join(decision.reasons) if decision.decision_code == "REJECT_EXECUTION_ERROR" else None,
                audit_report=audit_rep,
                verification_result=ver_res,
            )

            if retries <= budget and (last_diagnosis.hidden_effects or last_diagnosis.residual_divergences):
                # Synthesize repaired proposal and retry
                current_proposal = RecoverySynthesizer.synthesize_repaired_proposal(current_proposal, last_diagnosis)
            else:
                break

        # If all candidates fail: fail closed and keep previous persistent harness state
        return EvolutionResult(
            status="REJECTED",
            capability_delta=0.0,
            recoverability=0.0,
            effects=last_audit_report.detailed_effects if last_audit_report else [],
            recovery_plan=[],
            mutation_id=current_proposal.mutation_id,
            decision=last_decision or AdmissionDecision(status=AdmissionStatus.REJECT, decision_code="REJECT_FAILED_CLOSED"),
            diagnostic_report=last_diagnosis,
            retries_attempted=retries,
        )

    # --- Low-Level Mutation Lifecycle API ---

    def mutation(self, description: str = "", proposer: str = "agent") -> MutationBuilder:
        """Context manager for staging a self-evolution mutation."""
        return MutationBuilder(harness=self, description=description, proposer=proposer)

    def dry_run(
        self,
        candidate: Union[MutationProposal, MutationBuilder],
        capability_evaluator: Optional[Callable[[HarnessState], CapabilityResult]] = None,
        verification_mode: str = "smoke",
    ) -> AdmissionDecision:
        """Execute full evaluation, counterfactual verification, and audit without persistent admission."""
        decision, _, _, _, _ = self._evaluate_internal(candidate, capability_evaluator=capability_evaluator, verification_mode=verification_mode)
        return decision

    def evaluate(
        self,
        candidate: Union[MutationProposal, MutationBuilder],
        capability_evaluator: Optional[Callable[[HarnessState], CapabilityResult]] = None,
        verification_mode: str = "smoke",
    ) -> AdmissionDecision:
        """Evaluate candidate feasibility without auto-committing."""
        decision, _, _, _, _ = self._evaluate_internal(candidate, capability_evaluator=capability_evaluator, verification_mode=verification_mode)
        return decision

    def admit(
        self,
        candidate: Union[MutationProposal, MutationBuilder],
        capability_evaluator: Optional[Callable[[HarnessState], CapabilityResult]] = None,
        verification_mode: str = "smoke",
    ) -> AdmissionDecision:
        """Evaluate and, if admissible, apply and persist the mutation permanently."""
        decision, record, new_state, _, _ = self._evaluate_internal(
            candidate,
            capability_evaluator=capability_evaluator,
            verification_mode=verification_mode,
        )

        if decision.admissible and record and new_state:
            # Transition active harness state
            new_state.parent_version = self.current_state.version
            new_state.version = self.current_state.version + 1
            self.current_state = new_state

            # Append to lineage
            self.lineage.append_version(
                new_version=self.current_state.version,
                parent_version=new_state.parent_version,
                mutation_id=record.mutation_id,
                state_hash=self.current_state.canonical_hash(),
                description=record.description,
            )

            # Persist record in registry
            self.mutation_registry.record_mutation(record)
            self._persist_state_snapshot()

        return decision

    def protect(
        self,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        side_effect_detector: Optional[Callable[[], List[Effect]]] = None,
        framework: str = "generic",
    ) -> Callable[[Callable[..., Any]], Any]:
        """Universal decorator protecting a tool or function execution with EvoUndo."""
        from evoundo.protection.decorator import protect as protect_dec
        return protect_dec(
            harness=self,
            surface=surface,
            target=target,
            declared_effects=declared_effects,
            capture_fn=capture_fn,
            inverse_fn=inverse_fn,
            post_condition_probe=post_condition_probe,
            post_condition_validator=post_condition_validator,
            side_effect_detector=side_effect_detector,
            framework=framework,
        )

    def record_external_protected_mutation(
        self,
        mutation_id: str,
        description: str,
        witness: Witness,
        recovery_program: RecoveryProgram,
        declared_effects: Optional[List[Effect]] = None,
        identity: Optional[Any] = None,
        attribution: Optional[Any] = None,
        action_class: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
        epoch: int = 1,
    ) -> None:
        """Record an external protected tool/node execution into the harness registry and lineage."""
        from evoundo.registry.mutation_registry import MutationRecord
        from evoundo.effects.contracts import EffectContract

        contract = EffectContract()
        for eff in (declared_effects or []):
            contract.declare(eff.category, eff.target)

        record = MutationRecord(
            mutation_id=mutation_id,
            version=self.current_state.version,
            parent_harness_version=self.current_state.version,
            resulting_harness_version=self.current_state.version,
            description=description,
            effect_contract=contract,
            observed_effects=declared_effects or [],
            witness=witness,
            recovery_program=recovery_program,
            recovery_lcb=1.0,
            admitted=True,
            status="ACTIVE",
            epoch=epoch,
            identity=identity,
            attribution=attribution,
            action_class=action_class,
            metadata=metadata or {},
        )
        self.mutation_registry.record_mutation(record)


    def _evaluate_internal(
        self,
        candidate: Union[MutationProposal, MutationBuilder],
        capability_evaluator: Optional[Callable[[HarnessState], CapabilityResult]] = None,
        diagnostic_mode: str = "D1",
        recovery_language: str = "L1",
        verification_mode: str = "smoke",
    ) -> Tuple[AdmissionDecision, Optional[MutationRecord], Optional[HarnessState], Optional[EffectAuditReport], Optional[VerificationResult]]:
        """Internal EvoUndo control loop: Intercept -> Capture -> Sandbox -> Track -> Verify -> Decide."""
        proposal = candidate.proposal if isinstance(candidate, MutationBuilder) else candidate
        proposal.status = ProposalStatus.EVALUATING

        m_id = proposal.mutation_id
        pre_state = self.current_state.clone()

        self.event_logger.emit(
            event_type=EventType.MUTATION_PROPOSED,
            mutation_id=m_id,
            message=f"Proposal submitted: {proposal.description}",
            data={
                "proposer": proposal.proposer,
                "declared_targets": [f"[{c.value}] {t}" for c, t in proposal.effect_contract.all_declared()],
            },
        )

        # Step 1: Capture Pre-State Witness
        witness = self.witness_manager.capture_for_contract(
            pre_state,
            proposal.effect_contract,
            mutation_id=m_id,
            schema=proposal.witness_spec,
        )

        # Step 2: Capture Snapshot for comparison backends
        self.snapshot_store.capture_full(m_id, pre_state)
        self.snapshot_store.capture_scoped(m_id, pre_state, proposal.effect_contract)

        # Step 3: Execute Mutation in Isolated Candidate State
        candidate_state = pre_state.clone()
        exec_success = True
        exec_error = None

        try:
            if proposal.forward_mutation:
                proposal.forward_mutation(candidate_state)
            self.event_logger.emit(
                event_type=EventType.MUTATION_EXECUTED,
                mutation_id=m_id,
                message="Mutation executed successfully in candidate state",
            )
        except Exception as e:
            exec_success = False
            exec_error = str(e)

        # Step 4: Track Actual Effects (E_observed = diff(pre_state, candidate_state))
        audit_report: Optional[EffectAuditReport] = None
        if exec_success:
            audit_report = self.effect_tracker.audit(
                pre_state=pre_state,
                post_state=candidate_state,
                contract=proposal.effect_contract,
                mutation_id=m_id,
            )

        # Step 5: Evaluate Capability
        capability_result: Optional[CapabilityResult] = None
        if exec_success and capability_evaluator:
            capability_result = capability_evaluator(candidate_state)
            self.event_logger.emit(
                event_type=EventType.CAPABILITY_EVALUATED,
                mutation_id=m_id,
                message=f"Capability evaluated: delta={capability_result.delta} (improved={capability_result.improved})",
                data=capability_result.to_dict(),
            )
        elif exec_success and not capability_evaluator:
            capability_result = CapabilityResult(improved=True, score_before=1.0, score_after=1.0, delta=0.0)

        # Step 6: Counterfactual Recovery Verification (s -> w -> m -> u -> s' ~ s)
        verification_result: Optional[VerificationResult] = None
        if exec_success and proposal.forward_mutation and proposal.recovery_program:
            verification_result = self.counterfactual_verifier.verify(
                base_state=pre_state,
                forward_mutation_fn=proposal.forward_mutation,
                contract=proposal.effect_contract,
                witness_manager=self.witness_manager,
                recovery_engine=self.recovery_engine,
                recovery_program=proposal.recovery_program,
                mutation_id=m_id,
                mode=verification_mode,
            )

        # Step 7: Admission Gate Decision
        decision = self.admission_gate.evaluate(
            mutation_id=m_id,
            execution_success=exec_success,
            execution_error=exec_error,
            audit_report=audit_report,
            capability_result=capability_result,
            verification_result=verification_result,
        )

        record: Optional[MutationRecord] = None
        if decision.admissible and audit_report and proposal.recovery_program:
            proposal.status = ProposalStatus.ADMITTED
            record = MutationRecord(
                mutation_id=m_id,
                version=self.current_state.version + 1,
                parent_harness_version=self.current_state.version,
                resulting_harness_version=self.current_state.version + 1,
                description=proposal.description,
                effect_contract=proposal.effect_contract,
                observed_effects=audit_report.detailed_effects,
                witness=witness,
                recovery_program=proposal.recovery_program,
                capability_delta=capability_result.delta if capability_result else 0.0,
                capability_result=capability_result,
                verification_result=verification_result,
                recovery_lcb=verification_result.wilson_lcb if verification_result else 0.0,
                diagnostic_mode=diagnostic_mode,
                recovery_language=recovery_language,
                admitted=True,
                admission_decision=decision,
            )
        else:
            proposal.status = ProposalStatus.REJECTED

        return decision, record, candidate_state if decision.admissible else None, audit_report, verification_result

    def revert(
        self,
        mutation_id: str,
        reason: str = "",
        strategy: RecoveryStrategy = RecoveryStrategy.EVOUNDO_RECOVERY,
        caller_context: Optional[Any] = None,
        approval_request: Optional[Any] = None,
    ) -> HarnessState:
        """Revert a specific mutation while preserving subsequent independent mutations."""
        record = self.mutation_registry.inspect_mutation(mutation_id)
        if not record:
            raise KeyError(f"Mutation ID '{mutation_id}' not found in registry.")
        mutation_id = record.mutation_id

        if record.status == "REVERTED":
            raise ValueError(f"Mutation '{mutation_id}' is already reverted.")

        # Irreversible Action Enforcement
        act_cls = getattr(record, "action_class", None) or (record.metadata.get("action_class") if record.metadata else None)
        if act_cls == "IRREVERSIBLE" or str(act_cls) == "ActionClass.IRREVERSIBLE" or (hasattr(act_cls, "value") and act_cls.value == "IRREVERSIBLE"):
            from evoundo.actions.classifier import IrreversibleActionBlockedError
            raise IrreversibleActionBlockedError(
                f"IRREVERSIBLE_ACTION: Cannot revert mutation '{mutation_id}' of class IRREVERSIBLE. "
                f"External communication or action cannot be unsent. Compensatory action required."
            )


        # Recovery Authorization Verification via generic interface
        from evoundo.governance import get_recovery_authorizer
        authorizer = getattr(self, "recovery_authorizer", None) or get_recovery_authorizer()
        authorizer.authorize_revert(
            mutation_record=record,
            caller=caller_context,
            approval_request=approval_request,
        )

        # Downstream same-address conflict verification
        active_mutations = self.mutation_registry.list_mutations(status="ACTIVE")
        found_target = False
        conflicts = []
        for m in active_mutations:
            if m.mutation_id == mutation_id:
                found_target = True
                continue
            if found_target:
                # Check if downstream active mutation m touches conflicting target address
                for cat1, target1 in record.effect_contract.all_declared():
                    for cat2, target2 in m.effect_contract.all_declared():
                        conflict_msg = _check_target_conflict(cat1, target1, record, cat2, target2, m)
                        if conflict_msg:
                            conflicts.append(conflict_msg)
                            break

        if conflicts:
            raise ValueError(
                f"CONFLICT_DETECTED: Cannot selectively revert mutation '{mutation_id}'. "
                f"The following downstream active mutations modify the same target: {'; '.join(conflicts)}. "
                f"Revert downstream mutations first or resolve conflict."
            )

        # Execute targeted recovery against current state
        reverted_state = self.recovery_engine.recover(
            current_state=self.current_state,
            witness=record.witness,
            program=record.recovery_program,
            strategy=strategy,
            mutation_id=mutation_id,
        )

        reverted_state.parent_version = self.current_state.version
        reverted_state.version = self.current_state.version + 1
        self.current_state = reverted_state

        self.mutation_registry.mark_reverted(mutation_id, reason=reason)

        # Notify reconciler journal that mutation was reverted
        if hasattr(self, "reconciler") and self.reconciler:
            try:
                self.reconciler.record_reverted(mutation_id)
                ident = getattr(record, "identity", None)
                log_id = None
                if isinstance(ident, dict):
                    log_id = ident.get("logical_mutation_id")
                elif ident:
                    log_id = getattr(ident, "logical_mutation_id", None)
                if not log_id and mutation_id.startswith("mut_"):
                    log_id = mutation_id[4:]
                if log_id:
                    self.reconciler.record_reverted(log_id)
            except Exception as e:
                logger.warning("Error recording revert in reconciler: %s", e)

        # Invalidate dependent agent memory
        if hasattr(self, "memory_adapters"):
            for mem_adapter in self.memory_adapters:
                try:
                    mem_adapter.invalidate(mutation_id)
                except Exception as e:
                    logger.warning("Error invalidating memory adapter for mutation %s: %s", mutation_id, e)

        # Update lineage
        self.lineage.append_version(
            new_version=self.current_state.version,
            parent_version=reverted_state.parent_version,
            mutation_id=f"revert_{mutation_id}",
            state_hash=self.current_state.canonical_hash(),
            description=f"Revert mutation {mutation_id}: {reason or 'no reason'}",
        )

        self.event_logger.emit(
            event_type=EventType.MUTATION_REVERTED,
            mutation_id=mutation_id,
            message=f"Reverted mutation '{mutation_id}': {reason or 'no reason specified'}",
            data={"strategy": strategy.value, "new_state_version": self.current_state.version},
        )

        self._persist_state_snapshot()
        return self.current_state
