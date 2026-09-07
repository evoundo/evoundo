"""Persistent mutation registry and auditable lineage store."""

from __future__ import annotations
import base64
import copy
import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from evoundo.admission.policies import AdmissionDecision, CapabilityResult
from evoundo.effects.contracts import Effect, EffectCategory, EffectContract, EffectOpType
from evoundo.recovery.operations import RecoveryProgram
from evoundo.verification.counterfactual import VerificationResult
from evoundo.witness.stores import Witness
from evoundo.governance import TenantAccessDeniedError


def sanitize_json_serializable(obj: Any) -> Any:
    """Recursively convert callables, descriptors, and arbitrary objects to JSON-serializable primitives."""
    if callable(obj):
        return getattr(obj, "__name__", "<callable>")
    if hasattr(obj, "canonical_dict") and callable(obj.canonical_dict):
        return sanitize_json_serializable(obj.canonical_dict())
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return sanitize_json_serializable(obj.to_dict())
    if isinstance(obj, bytes):
        return {"__b64__": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            if isinstance(k, bytes):
                k_str = "__b64__:" + base64.b64encode(k).decode("ascii")
            else:
                k_str = str(k)
            res[k_str] = sanitize_json_serializable(v)
        return res
    if isinstance(obj, (list, tuple, set)):
        return [sanitize_json_serializable(x) for x in obj]
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


@dataclass
class MutationRecord:
    """Immutable audit record for an admitted self-evolution mutation."""
    mutation_id: str
    version: int = 1
    parent_harness_version: int = 0
    resulting_harness_version: int = 1
    description: str = ""
    effect_contract: Optional[EffectContract] = None
    observed_effects: List[Effect] = field(default_factory=list)
    witness: Optional[Witness] = None
    recovery_program: Optional[RecoveryProgram] = None
    forward_mutation_summary: Dict[str, Any] = field(default_factory=dict)
    witness_schema: Dict[str, Any] = field(default_factory=dict)
    capability_delta: float = 0.0
    capability_result: Optional[CapabilityResult] = None
    verification_result: Optional[VerificationResult] = None
    dev_verification: Dict[str, Any] = field(default_factory=dict)
    hidden_verification: Dict[str, Any] = field(default_factory=dict)
    recovery_lcb: float = 1.0
    model: str = "gpt-4o"
    diagnostic_mode: str = "none"
    recovery_language: str = "L1"
    admitted: bool = True
    admission_decision: Optional[AdmissionDecision] = None
    timestamp: float = field(default_factory=time.time)
    status: str = "ACTIVE"
    epoch: int = 1
    audit_log: List[Dict[str, Any]] = field(default_factory=list)
    identity: Optional[Any] = None
    attribution: Optional[Any] = None
    action_class: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    tenant_id: str = "default"
    target: Optional[str] = None
    agent_id: Optional[str] = None
    framework: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        contract_dict = {}
        if self.effect_contract:
            contract_dict = {
                "allowed_categories": [c.value if hasattr(c, "value") else str(c) for c in getattr(self.effect_contract, "allowed_categories", [])],
                "allowed_operations": [o.value if hasattr(o, "value") else str(o) for o in getattr(self.effect_contract, "allowed_operations", [])],
                "tools": list(getattr(self.effect_contract, "tools", [])),
                "middleware": list(getattr(self.effect_contract, "middleware", [])),
                "event_listeners": list(getattr(self.effect_contract, "event_listeners", [])),
                "files": list(getattr(self.effect_contract, "files", [])),
                "resources": list(getattr(self.effect_contract, "resources", [])),
                "prompts": list(getattr(self.effect_contract, "prompts", [])),
            }

        return {
            "mutation_id": self.mutation_id,
            "tenant_id": self.tenant_id,
            "version": self.version,
            "parent_harness_version": self.parent_harness_version,
            "resulting_harness_version": self.resulting_harness_version,
            "description": self.description,
            "effect_contract": contract_dict,
            "observed_effects": [e.to_dict() if hasattr(e, "to_dict") else str(e) for e in (self.observed_effects or [])],
            "witness": sanitize_json_serializable(self.witness.to_dict()) if self.witness and hasattr(self.witness, "to_dict") else (sanitize_json_serializable(self.witness) if self.witness else None),
            "witness_schema": self.witness_schema,
            "recovery_program": sanitize_json_serializable(self.recovery_program.to_dict()) if self.recovery_program and hasattr(self.recovery_program, "to_dict") else None,
            "capability_delta": round(self.capability_delta, 4),
            "capability_result": self.capability_result.to_dict() if self.capability_result and hasattr(self.capability_result, "to_dict") else None,
            "verification_result": self.verification_result.to_dict() if self.verification_result and hasattr(self.verification_result, "to_dict") else None,
            "dev_verification": sanitize_json_serializable(self.dev_verification),
            "hidden_verification": sanitize_json_serializable(self.hidden_verification),
            "recovery_lcb": round(self.recovery_lcb, 4),
            "model": self.model,
            "diagnostic_mode": self.diagnostic_mode,
            "recovery_language": self.recovery_language,
            "admitted": self.admitted,
            "admission_decision": self.admission_decision.to_dict() if self.admission_decision and hasattr(self.admission_decision, "to_dict") else None,
            "timestamp": self.timestamp,
            "status": self.status,
            "epoch": self.epoch,
            "audit_log": sanitize_json_serializable(self.audit_log),
            "identity": self.identity.to_dict() if hasattr(self.identity, "to_dict") else (sanitize_json_serializable(self.identity) if self.identity is not None else None),
            "attribution": self.attribution.to_dict() if hasattr(self.attribution, "to_dict") else (sanitize_json_serializable(self.attribution) if self.attribution is not None else None),
            "action_class": str(self.action_class.value if hasattr(self.action_class, "value") else self.action_class) if self.action_class else None,
            "metadata": sanitize_json_serializable(self.metadata),
            "target": self.target,
            "agent_id": self.agent_id,
            "framework": self.framework,
        }


class MutationRegistry:
    """Persistent ledger of self-evolutions and recovery programs with multi-tenant isolation."""

    def __init__(self, storage_path: Optional[str] = None, backend: Optional[Any] = None):
        self.storage_path = storage_path
        if backend:
            self.backend = backend
        elif storage_path and (storage_path.endswith(".db") or storage_path.endswith(".sqlite")):
            from evoundo.persistence import create_storage_backend
            self.backend = create_storage_backend(storage_path=storage_path)
        else:
            self.backend = None

        self._records: Dict[str, MutationRecord] = {}
        self._order: List[str] = []
        self._load()

    def record_mutation(self, record: MutationRecord) -> None:
        """Append an admitted mutation to the registry with atomic file lock."""
        desired_status = getattr(record, "status", "ACTIVE")
        if not self.storage_path:
            self._records[record.mutation_id] = record
            if record.mutation_id not in self._order:
                self._order.append(record.mutation_id)
            if self.backend:
                self.backend.save_mutation(record)
            return

        lock_path = self.storage_path + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(self.storage_path)), exist_ok=True)
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                self._load()
                record.status = desired_status
                self._records[record.mutation_id] = record
                if record.mutation_id not in self._order:
                    self._order.append(record.mutation_id)
                self._persist()
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

        if self.backend:
            self.backend.save_mutation(record)

    def lookup(self, mutation_id: str, tenant_id: Optional[str] = None) -> Optional[MutationRecord]:
        """Fetch mutation record; raises TenantAccessDeniedError if requesting tenant does not match."""
        self._load()
        rec = self._records.get(mutation_id)
        if rec is None:
            alt_id = mutation_id[4:] if mutation_id.startswith("mut_") else f"mut_{mutation_id}"
            rec = self._records.get(alt_id)
        if rec is None and self.backend:
            rec = self.backend.get_mutation(mutation_id, tenant_id=tenant_id)
            if rec is None:
                alt_id = mutation_id[4:] if mutation_id.startswith("mut_") else f"mut_{mutation_id}"
                rec = self.backend.get_mutation(alt_id, tenant_id=tenant_id)
            if rec:
                self._records[mutation_id] = rec
                if mutation_id not in self._order:
                    self._order.append(mutation_id)

        if rec is None:
            return None

        rec_tenant = getattr(rec, "tenant_id", "default") if not isinstance(rec, dict) else rec.get("tenant_id", "default")
        if tenant_id is not None and tenant_id != rec_tenant:
            raise TenantAccessDeniedError(
                f"Multi-Tenancy Violation: Requesting tenant '{tenant_id}' denied access to mutation "
                f"'{mutation_id}' owned by tenant '{rec_tenant}'"
            )
        return rec

    def inspect_mutation(self, mutation_id: str, tenant_id: Optional[str] = None) -> Optional[MutationRecord]:
        """Fetch details of a single mutation record with tenant fencing."""
        return self.lookup(mutation_id, tenant_id=tenant_id)

    def get(self, mutation_id: str, tenant_id: Optional[str] = None) -> Optional[MutationRecord]:
        """Alias for inspect_mutation."""
        return self.lookup(mutation_id, tenant_id=tenant_id)

    def get_mutation(self, mutation_id: str, tenant_id: Optional[str] = None) -> Optional[MutationRecord]:
        """Alias for inspect_mutation."""
        return self.lookup(mutation_id, tenant_id=tenant_id)

    def list_mutations(
        self,
        status: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[MutationRecord]:
        """List all recorded mutations in chronological order filtered by status and tenant_id."""
        self._load()
        records = [self._records[mid] for mid in self._order if mid in self._records]
        results = []
        for r in records:
            rec_status = getattr(r, "status", None) if not isinstance(r, dict) else r.get("status")
            if status is not None and rec_status != status:
                continue
            rec_tenant = getattr(r, "tenant_id", "default") if not isinstance(r, dict) else r.get("tenant_id", "default")
            if tenant_id is not None and rec_tenant != tenant_id:
                continue
            results.append(r)
        return results

    def mark_reverted(self, mutation_id: str, reason: str = "", tenant_id: Optional[str] = None) -> bool:
        """Update record status to REVERTED with audit message and tenant check."""
        rec = self.lookup(mutation_id, tenant_id=tenant_id)
        if not rec:
            return False

        audit_entry = {
            "action": "REVERT",
            "timestamp": time.time(),
            "reason": reason,
        }

        target_id = getattr(rec, "mutation_id", mutation_id)
        if not self.storage_path:
            if isinstance(rec, dict):
                rec["status"] = "REVERTED"
                rec.setdefault("audit_log", []).append(audit_entry)
            else:
                rec.status = "REVERTED"
                rec.audit_log.append(audit_entry)
            if self.backend:
                self.backend.update_status(target_id, "REVERTED", audit_entry=audit_entry, tenant_id=tenant_id)
            return True

        lock_path = self.storage_path + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(self.storage_path)), exist_ok=True)
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                self._load()
                rec = self._records.get(target_id)
                if not rec:
                    alt_id = target_id[4:] if target_id.startswith("mut_") else f"mut_{target_id}"
                    rec = self._records.get(alt_id)
                    if rec:
                        target_id = alt_id
                if not rec:
                    return False
                if isinstance(rec, dict):
                    rec["status"] = "REVERTED"
                    rec.setdefault("audit_log", []).append(audit_entry)
                else:
                    rec.status = "REVERTED"
                    rec.audit_log.append(audit_entry)
                self._persist()
                if self.backend:
                    self.backend.update_status(target_id, "REVERTED", audit_entry=audit_entry, tenant_id=tenant_id)
                return True
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def disable_mutation(self, mutation_id: str, reason: str = "", tenant_id: Optional[str] = None) -> bool:
        """Mark mutation disabled without executing recovery."""
        rec = self.lookup(mutation_id, tenant_id=tenant_id)
        if not rec:
            return False

        audit_entry = {
            "action": "DISABLE",
            "timestamp": time.time(),
            "reason": reason,
        }

        if not self.storage_path:
            if isinstance(rec, dict):
                rec["status"] = "DISABLED"
                rec.setdefault("audit_log", []).append(audit_entry)
            else:
                rec.status = "DISABLED"
                rec.audit_log.append(audit_entry)
            if self.backend:
                self.backend.update_status(mutation_id, "DISABLED", audit_entry=audit_entry, tenant_id=tenant_id)
            return True

        lock_path = self.storage_path + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(self.storage_path)), exist_ok=True)
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                self._load()
                rec = self._records.get(mutation_id)
                if not rec:
                    return False
                if isinstance(rec, dict):
                    rec["status"] = "DISABLED"
                    rec.setdefault("audit_log", []).append(audit_entry)
                else:
                    rec.status = "DISABLED"
                    rec.audit_log.append(audit_entry)
                self._persist()
                if self.backend:
                    self.backend.update_status(mutation_id, "DISABLED", audit_entry=audit_entry, tenant_id=tenant_id)
                return True
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def show_history(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return human-readable history summary for audit trails with targets and operations."""
        history = []
        for r in self.list_mutations(tenant_id=tenant_id):
            mid = getattr(r, "mutation_id", None) if not isinstance(r, dict) else r.get("mutation_id")
            tid = getattr(r, "tenant_id", "default") if not isinstance(r, dict) else r.get("tenant_id", "default")
            st = getattr(r, "status", None) if not isinstance(r, dict) else r.get("status")
            desc = getattr(r, "description", "") if not isinstance(r, dict) else r.get("description", "")
            ts = getattr(r, "timestamp", 0.0) if not isinstance(r, dict) else r.get("timestamp", 0.0)

            targets = []
            operations = []
            obs_effects = getattr(r, "observed_effects", []) if not isinstance(r, dict) else r.get("observed_effects", [])
            for eff in obs_effects:
                tgt = getattr(eff, "target", "") if not isinstance(eff, dict) else eff.get("target", "")
                op = str(getattr(eff, "op_type", "") if not isinstance(eff, dict) else eff.get("op_type", "")).split(".")[-1]
                if tgt and tgt not in targets:
                    targets.append(tgt)
                if op and op not in operations:
                    operations.append(op)

            rec_prog = getattr(r, "recovery_program", None) if not isinstance(r, dict) else r.get("recovery_program")
            if rec_prog and hasattr(rec_prog, "operations"):
                for op_item in rec_prog.operations:
                    tgt = getattr(op_item, "target", None) or getattr(op_item, "parameters", {}).get("target")
                    if tgt and str(tgt) not in targets:
                        targets.append(str(tgt))
                    driver = getattr(op_item, "driver_type", None) or getattr(op_item, "operation", None) or getattr(op_item, "op_type", None)
                    if driver and str(driver) not in operations:
                        operations.append(str(driver))

            recovery_level = getattr(r, "recovery_language", "R4") if not isinstance(r, dict) else r.get("recovery_language", "R4")
            if not recovery_level:
                recovery_level = "R4"
            if not targets:
                targets = ["default"]
            if not operations:
                operations = ["UPDATE"]

            history.append({
                "mutation_id": mid,
                "tenant_id": tid,
                "timestamp": ts,
                "status": st,
                "targets": targets,
                "operations": operations,
                "recovery_level": recovery_level,
                "description": desc,
            })
        return history

    def sync(self) -> None:
        """Explicitly re-load and re-persist to ensure state coherence with disk."""
        if self.storage_path:
            lock_path = self.storage_path + ".lock"
            os.makedirs(os.path.dirname(os.path.abspath(self.storage_path)), exist_ok=True)
            with open(lock_path, "w") as lock_f:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                try:
                    self._load()
                    self._persist()
                finally:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def _persist(self) -> None:
        if not self.storage_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.storage_path)), exist_ok=True)
        data = {
            mid: (self._records[mid].to_dict() if hasattr(self._records[mid], "to_dict") else self._records[mid])
            for mid in self._order if mid in self._records
        }
        tmp_path = self.storage_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.storage_path)

    def _load(self) -> None:
        if not self.storage_path or not os.path.exists(self.storage_path) or os.path.getsize(self.storage_path) == 0:
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            if "mutations" in data and isinstance(data["mutations"], list):
                items = [(r.get("mutation_id"), r) for r in data["mutations"] if isinstance(r, dict)]
            else:
                items = [(k, v) for k, v in data.items() if isinstance(v, dict)]
            for mid, rec_dict in items:
                if not mid:
                    continue
                observed = []
                for eff in rec_dict.get("observed_effects", []):
                    cat_val = eff.get("category", "resources")
                    try:
                        cat = EffectCategory(cat_val.lower() if isinstance(cat_val, str) else cat_val)
                    except Exception:
                        cat = EffectCategory.RESOURCES
                    op_val = eff.get("op_type", "UPDATE")
                    if str(op_val).upper() == "MODIFY":
                        op = EffectOpType.UPDATE
                    else:
                        try:
                            op = EffectOpType(str(op_val).upper())
                        except Exception:
                            op = EffectOpType.UPDATE
                    observed.append(
                        Effect(
                            category=cat,
                            op_type=op,
                            target=eff.get("target", ""),
                            old_value=eff.get("old_value", eff.get("pre_state")),
                            new_value=eff.get("new_value", eff.get("post_state")),
                            metadata=eff.get("metadata", {}),
                        )
                    )

                contract = EffectContract()
                decl = rec_dict.get("effect_contract", {})
                if isinstance(decl, dict):
                    for k, v in decl.items():
                        if hasattr(contract, k):
                            setattr(contract, k, set(v) if isinstance(v, (list, set)) else v)

                witness = None
                if rec_dict.get("witness"):
                    try:
                        from evoundo.witness.stores import Witness
                        witness = Witness.from_dict(rec_dict["witness"])
                    except Exception:
                        witness = rec_dict["witness"]

                recovery_prog = None
                if rec_dict.get("recovery_program"):
                    try:
                        from evoundo.recovery.operations import RecoveryProgram
                        recovery_prog = RecoveryProgram.from_dict(rec_dict["recovery_program"])
                    except Exception:
                        recovery_prog = rec_dict["recovery_program"]
                else:
                    recovery_prog = RecoveryProgram.synthesize_from_contract(contract)

                id_val = None
                if rec_dict.get("identity"):
                    try:
                        from evoundo.identity import MutationIdentity
                        id_val = MutationIdentity.from_dict(rec_dict["identity"])
                    except Exception:
                        id_val = rec_dict["identity"]

                tid = rec_dict.get("tenant_id", "default")

                if mid in self._records:
                    existing = self._records[mid]
                    existing.status = rec_dict.get("status", existing.status)
                    existing.epoch = rec_dict.get("epoch", existing.epoch)
                    existing.version = rec_dict.get("version", existing.version)
                    existing.tenant_id = tid
                    existing.parent_harness_version = rec_dict.get("parent_harness_version", existing.parent_harness_version)
                    existing.resulting_harness_version = rec_dict.get("resulting_harness_version", existing.resulting_harness_version)
                    existing.audit_log = rec_dict.get("audit_log", existing.audit_log)
                    if "metadata" in rec_dict and isinstance(rec_dict["metadata"], dict):
                        existing.metadata = rec_dict["metadata"]
                    if "target" in rec_dict and rec_dict["target"]:
                        existing.target = rec_dict["target"]
                    if "agent_id" in rec_dict and rec_dict["agent_id"]:
                        existing.agent_id = rec_dict["agent_id"]
                    if "framework" in rec_dict and rec_dict["framework"]:
                        existing.framework = rec_dict["framework"]
                    if id_val is not None and not existing.identity:
                        existing.identity = id_val
                    if existing.recovery_program and existing.recovery_program.operations:
                        from evoundo.recovery.operations import CustomRecoveryOp, DriverRecoveryOp
                        for i, op in enumerate(existing.recovery_program.operations):
                            if isinstance(op, CustomRecoveryOp) and not isinstance(op, DriverRecoveryOp) and recovery_prog and hasattr(recovery_prog, "operations") and i < len(recovery_prog.operations):
                                if getattr(op, "inverse_fn", None) is not None:
                                    recovery_prog.operations[i].inverse_fn = op.inverse_fn
                                if getattr(op, "verify_fn", None) is not None:
                                    recovery_prog.operations[i].verify_fn = op.verify_fn
                    existing.recovery_program = recovery_prog
                    record = existing
                else:
                    record = MutationRecord(
                        mutation_id=mid,
                        tenant_id=tid,
                        version=rec_dict.get("version", 1),
                        parent_harness_version=rec_dict.get("parent_harness_version", 1),
                        resulting_harness_version=rec_dict.get("resulting_harness_version", 2),
                        description=rec_dict.get("description", ""),
                        effect_contract=contract,
                        observed_effects=observed,
                        witness=witness,
                        recovery_program=recovery_prog,
                        capability_delta=rec_dict.get("capability_delta", 0.0),
                        recovery_lcb=rec_dict.get("recovery_lcb", 1.0),
                        recovery_language=rec_dict.get("recovery_language", "L1"),
                        status=rec_dict.get("status", "ACTIVE"),
                        epoch=rec_dict.get("epoch", 1),
                        audit_log=rec_dict.get("audit_log", []),
                        identity=id_val,
                        metadata=rec_dict.get("metadata", {}),
                        target=rec_dict.get("target"),
                        agent_id=rec_dict.get("agent_id"),
                        framework=rec_dict.get("framework"),
                    )
                self._records[mid] = record
                if mid not in self._order:
                    self._order.append(mid)
        except Exception:
            pass
