"""Reconciliation engine for crash recovery, framework retries, and duplicate suppression."""

from __future__ import annotations
import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

_reconciler_global_lock = threading.RLock()

from evoundo.identity import MutationIdentity
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger


class FailureStage(str, Enum):
    """Explicit stage where a mutation or retry failed."""
    CAPTURE = "CAPTURE"                 # Pre-execution witness capture
    EXECUTION = "EXECUTION"             # Tool function execution / write
    REGISTRATION = "REGISTRATION"       # Post-execution registry admission
    POST_CONDITION = "POST_CONDITION"   # Post-condition probe evaluation


class MutationLifecycleState(str, Enum):
    """Explicit lifecycle states for a mutation journal entry."""
    STARTED = "STARTED"
    MUTATION_COMPLETED = "MUTATION_COMPLETED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    REVERTED = "REVERTED"


class ReconciliationStatus(str, Enum):
    """Outcome of reconciliation evaluation."""
    EXECUTE_NEW = "EXECUTE_NEW"                                 # Clean first-time execution
    RETRY_REQUIRED = "RETRY_REQUIRED"                           # Previous attempt crashed before mutation; safe to retry
    RECONCILED_DUPLICATE_SUPPRESSED = "RECONCILED_DUPLICATE_SUPPRESSED" # External effect already applied; suppress duplicate
    CONFLICT_DETECTED = "CONFLICT_DETECTED"                     # State diverged unexpectedly


def _encode_bytes_obj(val: Any) -> Any:
    if isinstance(val, bytes):
        import base64
        return {"__b64__": base64.b64encode(val).decode("ascii")}
    if isinstance(val, dict):
        return {k: _encode_bytes_obj(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_encode_bytes_obj(x) for x in val]
    return val


def _decode_bytes_obj(val: Any) -> Any:
    if isinstance(val, dict):
        if "__b64__" in val and len(val) == 1:
            import base64
            try:
                return base64.b64decode(val["__b64__"])
            except Exception:
                return val["__b64__"]
        return {k: _decode_bytes_obj(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_decode_bytes_obj(x) for x in val]
    return val


def _json_default(o: Any) -> Any:
    if isinstance(o, bytes):
        import base64
        return {"__b64__": base64.b64encode(o).decode("ascii")}
    return str(o)


@dataclass
class JournalEntry:
    """Record of an in-flight or completed mutation execution."""
    identity: MutationIdentity
    status: str                                                 # MutationLifecycleState value
    pre_state_hash: Optional[str] = None
    post_state_hash: Optional[str] = None
    result_data: Any = None
    witness_data: Dict[str, Any] = field(default_factory=dict)
    effects_applied: List[Dict[str, Any]] = field(default_factory=list)
    recovery_ops: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    retry_count: int = 0
    epoch: int = 1
    attempt: int = 1
    failure_stage: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "status": self.status,
            "pre_state_hash": self.pre_state_hash,
            "post_state_hash": self.post_state_hash,
            "result_data": _encode_bytes_obj(self.result_data),
            "witness_data": _encode_bytes_obj(self.witness_data),
            "effects_applied": self.effects_applied,
            "recovery_ops": self.recovery_ops,
            "timestamp": self.timestamp,
            "retry_count": self.retry_count,
            "epoch": self.epoch,
            "attempt": self.attempt,
            "failure_stage": self.failure_stage,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> JournalEntry:
        id_data = d.get("identity", {})
        identity = MutationIdentity(
            logical_mutation_id=id_data.get("logical_mutation_id", ""),
            execution_id=id_data.get("execution_id", ""),
            framework=id_data.get("framework", "generic"),
            framework_run_id=id_data.get("framework_run_id"),
            tool_name=id_data.get("tool_name", ""),
            tool_call_id=id_data.get("tool_call_id"),
            retry_attempt=id_data.get("retry_attempt", 0),
            idempotency_key=id_data.get("idempotency_key", ""),
            created_at=id_data.get("created_at", time.time()),
            metadata=id_data.get("metadata", {}),
        )
        return cls(
            identity=identity,
            status=d.get("status", MutationLifecycleState.STARTED.value),
            pre_state_hash=d.get("pre_state_hash"),
            post_state_hash=d.get("post_state_hash"),
            result_data=_decode_bytes_obj(d.get("result_data")),
            witness_data=_decode_bytes_obj(d.get("witness_data", {})),
            effects_applied=d.get("effects_applied", []),
            recovery_ops=d.get("recovery_ops", []),
            timestamp=d.get("timestamp", time.time()),
            retry_count=d.get("retry_count", 0),
            epoch=d.get("epoch", 1),
            attempt=d.get("attempt", d.get("retry_count", 0) + 1),
            failure_stage=d.get("failure_stage"),
            error_message=d.get("error_message"),
        )


@dataclass
class ReconciliationDecision:
    """Decision produced by the reconciler before running a tool/mutation."""
    status: ReconciliationStatus
    identity: MutationIdentity
    should_execute_fn: bool
    cached_result: Any = None
    reason: str = ""
    external_state_matched: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "logical_mutation_id": self.identity.logical_mutation_id,
            "execution_id": self.identity.execution_id,
            "should_execute_fn": self.should_execute_fn,
            "reason": self.reason,
            "external_state_matched": self.external_state_matched,
            "retry_attempt": self.identity.retry_attempt,
        }


class MutationReconciler:
    """Thread-safe and process-safe mutation journal and state reconciler with disk durability."""

    def __init__(
        self,
        journal_path: Optional[str] = None,
        event_logger: Optional[StructuredEventLogger] = None,
    ):
        self.journal_path = journal_path
        self.event_logger = event_logger or default_event_logger
        self._journal: Dict[str, JournalEntry] = {}             # logical_mutation_id -> JournalEntry
        self._dirty_log_ids: Set[str] = set()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Hydrate journal entries from persistent storage if configured."""
        if not self.journal_path:
            return
        import os
        if os.path.exists(self.journal_path):
            try:
                with open(self.journal_path, "r") as f:
                    data = json.load(f)
                    for log_id, entry_dict in data.items():
                        if not (hasattr(self, "_dirty_log_ids") and log_id in self._dirty_log_ids):
                            self._journal[log_id] = JournalEntry.from_dict(entry_dict)
            except Exception as e:
                self.event_logger.emit(
                    event_type=EventType.ERROR,
                    message=f"Failed to load reconciliation journal from {self.journal_path}: {e}",
                )

    def _mark_dirty(self, logical_mutation_id: str) -> None:
        """Track logical mutation IDs that have been modified locally and must be persisted."""
        if not hasattr(self, "_dirty_log_ids"):
            self._dirty_log_ids = set()
        self._dirty_log_ids.add(logical_mutation_id)
        alt_id = logical_mutation_id[4:] if logical_mutation_id.startswith("mut_") else f"mut_{logical_mutation_id}"
        self._dirty_log_ids.add(alt_id)

    def _persist_to_disk(self, target_log_id: Optional[str] = None) -> None:
        """Atomically persist active journal to disk with fsync and cross-worker claim locking."""
        if not self.journal_path:
            return
        import os, tempfile, fcntl
        dir_name = os.path.dirname(os.path.abspath(self.journal_path))
        os.makedirs(dir_name, exist_ok=True)
        lock_path = f"{self.journal_path}.lock"
        tmp_path = None
        
        with _reconciler_global_lock:
            with open(lock_path, "w") as lock_f:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                try:
                    if not hasattr(self, "_claim_lost"):
                        self._claim_lost = {}

                    merged_data = {}
                    if os.path.exists(self.journal_path):
                        try:
                            with open(self.journal_path, "r", encoding="utf-8") as jf:
                                disk_data = json.load(jf)
                                if isinstance(disk_data, dict):
                                    merged_data.update(disk_data)
                        except Exception:
                            pass

                    # Determine candidate modified IDs: only dirty entries may update disk data
                    dirty_ids = set(getattr(self, "_dirty_log_ids", set()))
                    if target_log_id:
                        dirty_ids.add(target_log_id)
                        alt_id = target_log_id[4:] if target_log_id.startswith("mut_") else f"mut_{target_log_id}"
                        dirty_ids.add(alt_id)

                    for did in dirty_ids:
                        self._claim_lost.pop(did, None)

                    for log_id, entry in list(self._journal.items()):
                        # If an entry is NOT dirty, never overwrite disk state with stale in-memory data!
                        if dirty_ids and log_id not in dirty_ids:
                            if log_id in merged_data and isinstance(merged_data[log_id], dict):
                                try:
                                    self._journal[log_id] = JournalEntry.from_dict(merged_data[log_id])
                                except Exception:
                                    pass
                            continue

                        # For dirty entries, apply version fencing and concurrent claim checks against disk
                        if log_id in merged_data:
                            disk_entry_dict = merged_data[log_id]
                            if isinstance(disk_entry_dict, dict):
                                disk_epoch = disk_entry_dict.get("epoch", 1)
                                disk_attempt = disk_entry_dict.get("attempt", 1)
                                disk_status = disk_entry_dict.get("status")

                                # 1. Epoch fencing: disk is in a newer epoch
                                if disk_epoch > entry.epoch:
                                    self._claim_lost[log_id] = True
                                    self._journal[log_id] = JournalEntry.from_dict(disk_entry_dict)
                                    continue

                                # 2. Attempt fencing within same epoch: disk is on a higher attempt
                                if disk_epoch == entry.epoch and disk_attempt > entry.attempt:
                                    self._claim_lost[log_id] = True
                                    self._journal[log_id] = JournalEntry.from_dict(disk_entry_dict)
                                    continue

                                # 3. Status progression fencing: disk already completed mutation, do not demote to STARTED/FAILED
                                if disk_epoch == entry.epoch and disk_attempt == entry.attempt:
                                    if disk_status in (
                                        MutationLifecycleState.MUTATION_COMPLETED.value,
                                        MutationLifecycleState.COMMITTED.value,
                                    ) and entry.status in (
                                        MutationLifecycleState.STARTED.value,
                                        MutationLifecycleState.FAILED.value,
                                    ):
                                        self._claim_lost[log_id] = True
                                        self._journal[log_id] = JournalEntry.from_dict(disk_entry_dict)
                                        continue

                                # 4. Concurrent execution claim fencing:
                                disk_ident = disk_entry_dict.get("identity", {})
                                disk_exec_id = disk_ident.get("execution_id") if isinstance(disk_ident, dict) else getattr(disk_ident, "execution_id", None)
                                my_exec_id = getattr(entry.identity, "execution_id", None) if entry.identity else None

                                if disk_exec_id and my_exec_id and disk_exec_id != my_exec_id:
                                    if entry.status == MutationLifecycleState.STARTED.value:
                                        if disk_status in (
                                            MutationLifecycleState.STARTED.value,
                                            MutationLifecycleState.COMMITTED.value,
                                            MutationLifecycleState.MUTATION_COMPLETED.value,
                                        ):
                                            self._claim_lost[log_id] = True
                                            self._journal[log_id] = JournalEntry.from_dict(disk_entry_dict)
                                            continue

                        merged_data[log_id] = entry.to_dict()

                    # Merge any disk entries that we didn't have in self._journal into self._journal
                    for disk_log_id, disk_entry_dict in merged_data.items():
                        if disk_log_id not in self._journal and isinstance(disk_entry_dict, dict):
                            try:
                                self._journal[disk_log_id] = JournalEntry.from_dict(disk_entry_dict)
                            except Exception:
                                pass

                    tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix="journal_", suffix=".tmp")
                    with os.fdopen(tmp_fd, "w") as f:
                        json.dump(merged_data, f, indent=2, default=_json_default)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp_path, self.journal_path)

                    if hasattr(self, "_dirty_log_ids"):
                        self._dirty_log_ids.clear()
                except Exception as e:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                    self.event_logger.emit(
                        event_type=EventType.ERROR,
                        message=f"Failed to persist reconciliation journal: {e}",
                    )
                    raise e
                finally:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


    def sync_from_disk(self) -> None:
        """Force re-synchronization with disk storage for multi-process environments."""
        self._load_from_disk()

    def sync_with_registry(self, registry: Any) -> None:
        """Synchronize registry status with journal while strictly respecting execution epochs."""
        if not registry:
            return
        for m_rec in registry.list_mutations():
            m_id = getattr(m_rec, "mutation_id", None) if not isinstance(m_rec, dict) else m_rec.get("mutation_id")
            m_ident = getattr(m_rec, "identity", None) if not isinstance(m_rec, dict) else m_rec.get("identity")
            m_status = getattr(m_rec, "status", None) if not isinstance(m_rec, dict) else m_rec.get("status")
            m_epoch = getattr(m_rec, "epoch", 1) if not isinstance(m_rec, dict) else m_rec.get("epoch", 1)
            rec_log_id = m_ident.get("logical_mutation_id") if isinstance(m_ident, dict) else getattr(m_ident, "logical_mutation_id", None)
            target_id = rec_log_id or m_id
            if not target_id:
                continue
            entry = self.get_entry(target_id)
            if entry and m_status == MutationLifecycleState.REVERTED.value:
                # Only sync REVERTED if the registry record matches or exceeds the entry epoch,
                # and entry is not currently in an active or completed state of a newer epoch.
                if m_epoch >= entry.epoch and entry.status not in (
                    MutationLifecycleState.STARTED.value,
                    MutationLifecycleState.MUTATION_COMPLETED.value,
                    MutationLifecycleState.FAILED.value,
                ):
                    self.record_reverted(target_id)
            elif entry is None:
                # Reconstruct / hydrate missing journal entry from persistent registry evidence
                rec_result = None
                if hasattr(m_rec, "metadata") and isinstance(m_rec.metadata, dict):
                    rec_result = m_rec.metadata.get("result")
                elif isinstance(m_rec, dict) and isinstance(m_rec.get("metadata"), dict):
                    rec_result = m_rec["metadata"].get("result")

                if rec_result is None and hasattr(registry, "lookup"):
                    full_rec = registry.lookup(target_id) or (registry.lookup(m_id) if m_id else None)
                    if full_rec:
                        if hasattr(full_rec, "metadata") and isinstance(full_rec.metadata, dict):
                            rec_result = full_rec.metadata.get("result")
                        elif isinstance(full_rec, dict) and isinstance(full_rec.get("metadata"), dict):
                            rec_result = full_rec["metadata"].get("result")

                wit_data = {}
                rec_witness = getattr(m_rec, "witness", None) if not isinstance(m_rec, dict) else m_rec.get("witness")
                if rec_witness:
                    wit_data = getattr(rec_witness, "data", {}) if hasattr(rec_witness, "data") else (rec_witness if isinstance(rec_witness, dict) else {})

                rec_ops = []
                rec_prog = getattr(m_rec, "recovery_program", None) if not isinstance(m_rec, dict) else m_rec.get("recovery_program")
                if rec_prog and hasattr(rec_prog, "operations"):
                    for op in rec_prog.operations:
                        if hasattr(op, "to_dict"):
                            rec_ops.append(op.to_dict())

                if isinstance(m_ident, MutationIdentity):
                    rec_identity = m_ident
                elif isinstance(m_ident, dict):
                    if hasattr(MutationIdentity, "from_dict"):
                        rec_identity = MutationIdentity.from_dict(m_ident)
                    else:
                        rec_identity = MutationIdentity(
                            logical_mutation_id=m_ident.get("logical_mutation_id") or target_id,
                            execution_id=m_ident.get("execution_id", ""),
                            framework=m_ident.get("framework", "generic"),
                            framework_run_id=m_ident.get("framework_run_id"),
                            tool_name=m_ident.get("tool_name"),
                            tool_call_id=m_ident.get("tool_call_id"),
                            retry_attempt=m_ident.get("retry_attempt", 0),
                            idempotency_key=m_ident.get("idempotency_key"),
                            created_at=m_ident.get("created_at", time.time()),
                            metadata=m_ident.get("metadata", {}),
                        )
                else:
                    rec_identity = MutationIdentity(logical_mutation_id=target_id)

                if m_status in ("ACTIVE", MutationLifecycleState.COMMITTED.value):
                    hydrated_entry = JournalEntry(
                        identity=rec_identity,
                        status=MutationLifecycleState.COMMITTED.value,
                        result_data=rec_result,
                        witness_data=wit_data,
                        recovery_ops=rec_ops,
                        epoch=m_epoch,
                        attempt=1,
                    )
                    self._journal[target_id] = hydrated_entry
                    alt_id = target_id[4:] if target_id.startswith("mut_") else f"mut_{target_id}"
                    self._journal[alt_id] = hydrated_entry
                    if m_id:
                        self._journal[m_id] = hydrated_entry
                    self._mark_dirty(target_id)
                    self._persist_to_disk()
                elif m_status == MutationLifecycleState.REVERTED.value:
                    hydrated_entry = JournalEntry(
                        identity=rec_identity,
                        status=MutationLifecycleState.REVERTED.value,
                        epoch=m_epoch,
                        attempt=1,
                    )
                    self._journal[target_id] = hydrated_entry
                    alt_id = target_id[4:] if target_id.startswith("mut_") else f"mut_{target_id}"
                    self._journal[alt_id] = hydrated_entry
                    if m_id:
                        self._journal[m_id] = hydrated_entry
                    self._mark_dirty(target_id)
                    self._persist_to_disk()

    def _handle_rejected_claim_or_completed_disk(
        self,
        log_id: str,
        identity: MutationIdentity,
        action_name: str,
    ) -> Optional[ReconciliationDecision]:
        current_entry = self._journal.get(log_id) or self.get_entry(log_id)
        is_rejected = (
            getattr(self, "_claim_lost", {}).get(log_id, False)
            or (
                current_entry is not None
                and current_entry.status in (
                    MutationLifecycleState.MUTATION_COMPLETED.value,
                    MutationLifecycleState.COMMITTED.value,
                )
            )
            or (
                current_entry is not None
                and current_entry.identity is not None
                and identity.execution_id is not None
                and current_entry.identity.execution_id != identity.execution_id
            )
        )
        if not is_rejected:
            return None

        # If disk state has advanced to completed or committed, return duplicate suppressed with cached result
        if current_entry and current_entry.status in (
            MutationLifecycleState.MUTATION_COMPLETED.value,
            MutationLifecycleState.COMMITTED.value,
        ):
            self.event_logger.emit(
                event_type=EventType.MUTATION_RECONCILED,
                mutation_id=log_id,
                message=f"Mutation already completed by another worker in epoch {current_entry.epoch}; duplicate execution suppressed",
                data={"logical_mutation_id": log_id, "epoch": current_entry.epoch},
            )
            return ReconciliationDecision(
                status=ReconciliationStatus.RECONCILED_DUPLICATE_SUPPRESSED,
                identity=identity,
                should_execute_fn=False,
                cached_result=current_entry.result_data,
                external_state_matched=True,
                reason=f"Mutation was completed by another worker (epoch {current_entry.epoch}). Duplicate execution suppressed.",
            )

        # Otherwise, the concurrent claim was lost to another active or conflicting worker
        return ReconciliationDecision(
            status=ReconciliationStatus.CONFLICT_DETECTED,
            identity=identity,
            should_execute_fn=False,
            cached_result=current_entry.result_data if current_entry else None,
            external_state_matched=False,
            reason=f"Concurrent {action_name} claim lost to another worker for {log_id}. Execution aborted.",
        )

    def evaluate_request(
        self,
        identity: MutationIdentity,
        current_state_probe: Optional[Callable[[], Any]] = None,
        expected_post_condition: Optional[Callable[[Any], bool]] = None,
        mutation_registry: Optional[Any] = None,
    ) -> ReconciliationDecision:
        """Evaluate incoming execution request to determine if it is a new call, duplicate, or retry.

        Core safety invariant: Every retry must strictly answer:
        'What evidence proves executing this again is safe?'
        If evidence is missing or ambiguous, the request must fail closed with CONFLICT_DETECTED.
        """
        self.sync_from_disk()
        if mutation_registry:
            self.sync_with_registry(mutation_registry)

        log_id = identity.logical_mutation_id
        existing_entry = self.get_entry(log_id)

        # Consult mutation registry directly if journal is missing entry
        if existing_entry is None and mutation_registry is not None:
            m_rec = None
            if hasattr(mutation_registry, "inspect_mutation"):
                m_rec = mutation_registry.inspect_mutation(log_id)
                if not m_rec and not log_id.startswith("mut_"):
                    m_rec = mutation_registry.inspect_mutation(f"mut_{log_id}")
            elif hasattr(mutation_registry, "lookup"):
                m_rec = mutation_registry.lookup(log_id)
            elif hasattr(mutation_registry, "get_mutation"):
                m_rec = mutation_registry.get_mutation(log_id)

            if m_rec:
                m_status = getattr(m_rec, "status", None) if not isinstance(m_rec, dict) else m_rec.get("status")
                m_epoch = getattr(m_rec, "epoch", 1) if not isinstance(m_rec, dict) else m_rec.get("epoch", 1)
                rec_result = None
                if hasattr(m_rec, "metadata") and isinstance(m_rec.metadata, dict):
                    rec_result = m_rec.metadata.get("result")
                elif isinstance(m_rec, dict) and isinstance(m_rec.get("metadata"), dict):
                    rec_result = m_rec["metadata"].get("result")

                if rec_result is None and hasattr(mutation_registry, "lookup"):
                    full_rec = mutation_registry.lookup(log_id) or (mutation_registry.lookup(f"mut_{log_id}") if not log_id.startswith("mut_") else None)
                    if full_rec:
                        if hasattr(full_rec, "metadata") and isinstance(full_rec.metadata, dict):
                            rec_result = full_rec.metadata.get("result")
                        elif isinstance(full_rec, dict) and isinstance(full_rec.get("metadata"), dict):
                            rec_result = full_rec["metadata"].get("result")

                wit_data = {}
                rec_witness = getattr(m_rec, "witness", None) if not isinstance(m_rec, dict) else m_rec.get("witness")
                if rec_witness:
                    wit_data = getattr(rec_witness, "data", {}) if hasattr(rec_witness, "data") else (rec_witness if isinstance(rec_witness, dict) else {})

                rec_ops = []
                rec_prog = getattr(m_rec, "recovery_program", None) if not isinstance(m_rec, dict) else m_rec.get("recovery_program")
                if rec_prog and hasattr(rec_prog, "operations"):
                    for op in rec_prog.operations:
                        if hasattr(op, "to_dict"):
                            rec_ops.append(op.to_dict())

                if m_status in ("ACTIVE", MutationLifecycleState.COMMITTED.value):
                    existing_entry = JournalEntry(
                        identity=identity,
                        status=MutationLifecycleState.COMMITTED.value,
                        result_data=rec_result,
                        witness_data=wit_data,
                        recovery_ops=rec_ops,
                        epoch=m_epoch,
                        attempt=1,
                    )
                    self._journal[log_id] = existing_entry
                    alt_id = log_id[4:] if log_id.startswith("mut_") else f"mut_{log_id}"
                    self._journal[alt_id] = existing_entry
                    self._mark_dirty(log_id)
                    self._persist_to_disk()
                    self.event_logger.emit(
                        event_type=EventType.MUTATION_RECONCILED,
                        mutation_id=log_id,
                        message=f"Reconciled from active registry record: duplicate execution suppressed in epoch {m_epoch}",
                        data={"logical_mutation_id": log_id, "epoch": m_epoch},
                    )
                    return ReconciliationDecision(
                        status=ReconciliationStatus.RECONCILED_DUPLICATE_SUPPRESSED,
                        identity=identity,
                        should_execute_fn=False,
                        cached_result=rec_result,
                        external_state_matched=True,
                        reason=f"Mutation already executed and committed as ACTIVE in registry (epoch {m_epoch}). Duplicate execution suppressed.",
                    )
                elif m_status == MutationLifecycleState.REVERTED.value:
                    existing_entry = JournalEntry(
                        identity=identity,
                        status=MutationLifecycleState.REVERTED.value,
                        epoch=m_epoch,
                        attempt=1,
                    )
                    self._journal[log_id] = existing_entry
                    alt_id = log_id[4:] if log_id.startswith("mut_") else f"mut_{log_id}"
                    self._journal[alt_id] = existing_entry
                    self._mark_dirty(log_id)
                    self._persist_to_disk()

        # 1. Brand new logical mutation
        if existing_entry is None:
            entry = JournalEntry(
                identity=identity,
                status=MutationLifecycleState.STARTED.value,
                epoch=1,
                attempt=1,
                retry_count=0,
            )
            self._journal[log_id] = entry
            self._mark_dirty(log_id)
            self._persist_to_disk()
            rejected_decision = self._handle_rejected_claim_or_completed_disk(log_id, identity, "execution")
            if rejected_decision is not None:
                return rejected_decision
            self.event_logger.emit(
                event_type=EventType.MUTATION_REQUESTED,
                mutation_id=log_id,
                message=f"Starting new logical mutation for tool '{identity.tool_name}' in epoch 1",
                data=identity.to_dict(),
            )
            return ReconciliationDecision(
                status=ReconciliationStatus.EXECUTE_NEW,
                identity=identity,
                should_execute_fn=True,
                reason="First execution attempt for this logical mutation.",
            )

        # 2. Previously reverted mutation -> Clean repeat execution in new epoch
        if existing_entry.status == MutationLifecycleState.REVERTED.value:
            existing_entry.epoch += 1
            existing_entry.attempt = 1
            existing_entry.status = MutationLifecycleState.STARTED.value
            existing_entry.retry_count = 0
            existing_entry.result_data = None
            existing_entry.effects_applied = []
            existing_entry.recovery_ops = []
            existing_entry.failure_stage = None
            existing_entry.error_message = None
            existing_entry.timestamp = time.time()
            if not identity.execution_id or (existing_entry.identity and identity.execution_id == existing_entry.identity.execution_id):
                import uuid
                identity.execution_id = str(uuid.uuid4())
            identity.retry_attempt = 0
            existing_entry.identity = identity
            self._mark_dirty(log_id)
            self._persist_to_disk()
            rejected_decision = self._handle_rejected_claim_or_completed_disk(log_id, identity, "re-execution")
            if rejected_decision is not None:
                return rejected_decision
            self.event_logger.emit(
                event_type=EventType.MUTATION_REQUESTED,
                mutation_id=log_id,
                message=f"Re-executing previously reverted mutation for tool '{identity.tool_name}' in epoch {existing_entry.epoch}",
                data=identity.to_dict(),
            )
            return ReconciliationDecision(
                status=ReconciliationStatus.EXECUTE_NEW,
                identity=identity,
                should_execute_fn=True,
                reason=f"Mutation was previously reverted. Repeating execution in epoch {existing_entry.epoch}.",
            )

        # 3. Mutation already completed -> Duplicate suppressed with cached result
        if existing_entry.status in (
            MutationLifecycleState.MUTATION_COMPLETED.value,
            MutationLifecycleState.COMMITTED.value,
        ):
            cached_res = existing_entry.result_data
            if cached_res is None and mutation_registry is not None:
                m_rec = None
                if hasattr(mutation_registry, "inspect_mutation"):
                    m_rec = mutation_registry.inspect_mutation(log_id) or (mutation_registry.inspect_mutation(f"mut_{log_id}") if not log_id.startswith("mut_") else None)
                elif hasattr(mutation_registry, "lookup"):
                    m_rec = mutation_registry.lookup(log_id) or (mutation_registry.lookup(f"mut_{log_id}") if not log_id.startswith("mut_") else None)
                elif hasattr(mutation_registry, "get_mutation"):
                    m_rec = mutation_registry.get_mutation(log_id) or (mutation_registry.get_mutation(f"mut_{log_id}") if not log_id.startswith("mut_") else None)
                if m_rec:
                    m_meta = getattr(m_rec, "metadata", None) if not isinstance(m_rec, dict) else m_rec.get("metadata")
                    if isinstance(m_meta, dict) and "result" in m_meta:
                        cached_res = m_meta["result"]
                        existing_entry.result_data = cached_res
                        self._mark_dirty(log_id)
                        self._persist_to_disk()

            return ReconciliationDecision(
                status=ReconciliationStatus.RECONCILED_DUPLICATE_SUPPRESSED,
                identity=identity,
                should_execute_fn=False,
                cached_result=cached_res,
                external_state_matched=True,
                reason=f"Mutation completed in epoch {existing_entry.epoch}. Duplicate suppressed.",
            )

        # 4. Safe Pre-Execution Failure (e.g. FailureStage.CAPTURE)
        # Evidence: Failure occurred strictly before tool execution; zero external mutations occurred.
        is_pre_execution_failure = (
            existing_entry.status == MutationLifecycleState.FAILED.value
            and existing_entry.failure_stage in (
                FailureStage.CAPTURE.value,
                "CAPTURE",
                "PRE_EXECUTION",
            )
        )

        if is_pre_execution_failure:
            existing_entry.attempt += 1
            existing_entry.retry_count += 1
            if not identity.execution_id or (existing_entry.identity and identity.execution_id == existing_entry.identity.execution_id):
                import uuid
                identity.execution_id = str(uuid.uuid4())
            identity.retry_attempt = existing_entry.retry_count
            existing_entry.identity = identity
            # CRITICAL: Persist STARTED before every execution attempt, including retries!
            existing_entry.status = MutationLifecycleState.STARTED.value
            existing_entry.failure_stage = None
            existing_entry.error_message = None
            existing_entry.timestamp = time.time()
            self._mark_dirty(log_id)
            self._persist_to_disk()
            rejected_decision = self._handle_rejected_claim_or_completed_disk(log_id, identity, "retry")
            if rejected_decision is not None:
                return rejected_decision
            self.event_logger.emit(
                event_type=EventType.MUTATION_REQUESTED,
                mutation_id=log_id,
                message=f"Retrying safe pre-execution capture failure (epoch {existing_entry.epoch}, attempt {existing_entry.attempt})",
                data=identity.to_dict(),
            )
            return ReconciliationDecision(
                status=ReconciliationStatus.RETRY_REQUIRED,
                identity=identity,
                should_execute_fn=True,
                reason=f"Previous attempt failed during pre-execution capture (attempt {existing_entry.attempt}). Safe to retry.",
            )

        # 5. Ambiguous In-Flight Execution (STARTED or FAILED during EXECUTION/TIMEOUT)
        # Check if external state probe proves the write already completed
        if current_state_probe and expected_post_condition:
            try:
                curr = current_state_probe()
                if expected_post_condition(curr):
                    existing_entry.status = MutationLifecycleState.MUTATION_COMPLETED.value
                    self._mark_dirty(log_id)
                    self._persist_to_disk()
                    self.event_logger.emit(
                        event_type=EventType.DUPLICATE_MUTATION_SUPPRESSED,
                        mutation_id=log_id,
                        message=f"Suppressed duplicate execution for {log_id}: external post-condition verified by probe.",
                        data={
                            "logical_mutation_id": log_id,
                            "cached_result": str(existing_entry.result_data),
                            "probed_state": str(curr),
                        },
                    )
                    return ReconciliationDecision(
                        status=ReconciliationStatus.RECONCILED_DUPLICATE_SUPPRESSED,
                        identity=identity,
                        should_execute_fn=False,
                        cached_result=existing_entry.result_data or {"balance": curr, "reconciled": True},
                        external_state_matched=True,
                        reason="External mutation verified on real resource by probe. Duplicate suppressed.",
                    )
            except Exception as e:
                self.event_logger.emit(
                    event_type=EventType.ERROR,
                    mutation_id=log_id,
                    message=f"External state probe failed during reconciliation: {e}",
                )
                return ReconciliationDecision(
                    status=ReconciliationStatus.CONFLICT_DETECTED,
                    identity=identity,
                    should_execute_fn=False,
                    cached_result=existing_entry.result_data,
                    external_state_matched=False,
                    reason=f"External state probe failed with infrastructure error ({e}). Ambiguous execution state: failing closed to prevent duplicate execution.",
                )

        # No evidence proving it is safe to execute again -> STRICT FAIL CLOSED
        existing_entry.retry_count += 1
        identity.retry_attempt = existing_entry.retry_count
        self.event_logger.emit(
            event_type=EventType.ERROR,
            mutation_id=log_id,
            message=f"Ambiguous in-flight execution detected for {log_id}: status is {existing_entry.status}. Refusing retry to prevent duplicate writes.",
            data={"status": existing_entry.status, "epoch": existing_entry.epoch, "attempt": existing_entry.attempt},
        )
        return ReconciliationDecision(
            status=ReconciliationStatus.CONFLICT_DETECTED,
            identity=identity,
            should_execute_fn=False,
            cached_result=existing_entry.result_data,
            external_state_matched=False,
            reason=f"Previous execution attempt failed or timed out in-flight (status {existing_entry.status}, stage {existing_entry.failure_stage}) without proof of safety. Refusing ambiguous retry to prevent duplicate execution.",
        )

    def _find_or_create_entry(self, logical_mutation_id: str) -> JournalEntry:
        self.sync_from_disk()
        entry = self.get_entry(logical_mutation_id)
        if entry is None:
            identity = MutationIdentity(
                logical_mutation_id=logical_mutation_id,
                tool_name="unregistered",
            )
            entry = JournalEntry(identity=identity, status=MutationLifecycleState.STARTED.value)
            self._journal[logical_mutation_id] = entry
        self._mark_dirty(logical_mutation_id)
        return entry

    def record_witness(self, logical_mutation_id: str, witness_data: Dict[str, Any]) -> None:
        """Record captured witness data for a mutation."""
        entry = self._find_or_create_entry(logical_mutation_id)
        entry.witness_data = witness_data
        self._persist_to_disk()

    def record_mutation_executed(
        self,
        logical_mutation_id: str,
        result: Any,
        post_state_hash: Optional[str] = None,
        effects: Optional[List[Dict[str, Any]]] = None,
        recovery_ops: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Record that the external mutation succeeded."""
        entry = self._find_or_create_entry(logical_mutation_id)
        entry.status = MutationLifecycleState.MUTATION_COMPLETED.value
        entry.result_data = result
        entry.post_state_hash = post_state_hash
        entry.failure_stage = None
        entry.error_message = None
        if effects:
            entry.effects_applied = effects
        if recovery_ops:
            entry.recovery_ops = recovery_ops
        self._persist_to_disk()

        self.event_logger.emit(
            event_type=EventType.MUTATION_EXECUTED,
            mutation_id=logical_mutation_id,
            message=f"External mutation successfully applied for {logical_mutation_id}",
            data={"result": str(result), "post_state_hash": post_state_hash, "epoch": entry.epoch, "attempt": entry.attempt},
        )

    def record_committed(self, logical_mutation_id: str) -> None:
        """Record that the runtime committed the step."""
        entry = self._find_or_create_entry(logical_mutation_id)
        entry.status = MutationLifecycleState.COMMITTED.value
        self._persist_to_disk()
        self.event_logger.emit(
            event_type=EventType.EXECUTION_COMMITTED,
            mutation_id=logical_mutation_id,
            message=f"Execution committed for {logical_mutation_id}",
        )

    def record_failed(
        self,
        logical_mutation_id: str,
        error: str,
        failure_stage: FailureStage | str = FailureStage.EXECUTION,
    ) -> None:
        """Record failure during execution with explicit failure stage (never inferred from error text)."""
        entry = self._find_or_create_entry(logical_mutation_id)
        if entry.status in (
            MutationLifecycleState.MUTATION_COMPLETED.value,
            MutationLifecycleState.COMMITTED.value,
        ):
            self.event_logger.emit(
                event_type=EventType.FAILURE_DETECTED,
                mutation_id=logical_mutation_id,
                message=f"Post-completion error for {logical_mutation_id}: {error}",
                data={"error": error},
            )
            return
        stage_val = failure_stage.value if isinstance(failure_stage, FailureStage) else str(failure_stage)
        if stage_val == "PRE_EXECUTION":
            stage_val = FailureStage.CAPTURE.value
        entry.status = MutationLifecycleState.FAILED.value
        entry.error_message = error
        entry.failure_stage = stage_val
        self._persist_to_disk()
        self.event_logger.emit(
            event_type=EventType.FAILURE_DETECTED,
            mutation_id=logical_mutation_id,
            message=f"Execution failed for {logical_mutation_id} (stage: {entry.failure_stage}): {error}",
            data={"error": error, "failure_stage": entry.failure_stage, "epoch": entry.epoch, "attempt": entry.attempt},
        )

    def record_reverted(self, logical_mutation_id: str) -> None:
        """Record that a mutation was reverted."""
        entry = self._find_or_create_entry(logical_mutation_id)
        entry.status = MutationLifecycleState.REVERTED.value
        entry.result_data = None
        entry.effects_applied = []
        entry.recovery_ops = []
        alt_id = logical_mutation_id[4:] if logical_mutation_id.startswith("mut_") else f"mut_{logical_mutation_id}"
        if alt_id in self._journal:
            self._journal[alt_id].status = MutationLifecycleState.REVERTED.value
            self._journal[alt_id].result_data = None
            self._journal[alt_id].effects_applied = []
            self._journal[alt_id].recovery_ops = []
        self._persist_to_disk()
        self.event_logger.emit(
            event_type=EventType.MUTATION_REVERTED,
            mutation_id=logical_mutation_id,
            message=f"Mutation {logical_mutation_id} marked as REVERTED in journal (epoch {entry.epoch})",
        )

    def get_entry(self, logical_mutation_id: str) -> Optional[JournalEntry]:
        self.sync_from_disk()
        if logical_mutation_id in self._journal:
            return self._journal[logical_mutation_id]
        if logical_mutation_id.startswith("mut_"):
            raw_id = logical_mutation_id[4:]
            if raw_id in self._journal:
                return self._journal[raw_id]
        else:
            mut_id = f"mut_{logical_mutation_id}"
            if mut_id in self._journal:
                return self._journal[mut_id]
        return None

    def list_entries(self) -> List[JournalEntry]:
        self.sync_from_disk()
        return list(self._journal.values())

