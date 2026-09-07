"""JSON file storage backend for EvoUndo (default local engine)."""

from __future__ import annotations
from contextlib import contextmanager
import copy
import fcntl
import json
import logging
import os
import time
from typing import Any, Dict, Generator, List, Optional

from evoundo.persistence.base import MutationStorageBackend

logger = logging.getLogger("evoundo.persistence.json_file")


class JsonFileStorage(MutationStorageBackend):
    """File-backed JSON storage backend using POSIX fcntl advisory file locking."""

    def __init__(self, file_path: Optional[str] = None) -> None:
        self.file_path = file_path
        self._records: Dict[str, Any] = {}
        self._order: List[str] = []
        if self.file_path and os.path.exists(self.file_path):
            self._load()

    @contextmanager
    def acquire_lock(self, timeout_sec: float = 10.0) -> Generator[None, None, None]:
        if not self.file_path:
            yield
            return

        lock_path = self.file_path + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(self.file_path)), exist_ok=True)
        with open(lock_path, "w") as lock_f:
            start_time = time.time()
            acquired = False
            while not acquired:
                try:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (IOError, BlockingIOError):
                    if time.time() - start_time > timeout_sec:
                        raise TimeoutError(f"Timed out after {timeout_sec}s acquiring lock on {lock_path}")
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def _load(self) -> None:
        if not self.file_path or not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    # Preserves records if dictionary
                    records_raw = data.get("records", data)
                    order_raw = data.get("order", list(records_raw.keys()))
                    self._records = records_raw
                    self._order = order_raw
        except Exception as e:
            logger.warning("Failed loading JSON storage from %s: %s", self.file_path, e)

    def _persist(self) -> None:
        if not self.file_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.file_path)), exist_ok=True)
        serialized_records = {}
        for mid, rec in self._records.items():
            if hasattr(rec, "to_dict") and callable(rec.to_dict):
                serialized_records[mid] = rec.to_dict()
            elif isinstance(rec, dict):
                serialized_records[mid] = rec
            else:
                serialized_records[mid] = getattr(rec, "__dict__", str(rec))

        payload = {
            "version": 1,
            "updated_at": time.time(),
            "order": self._order,
            "records": serialized_records,
        }
        temp_path = self.file_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(temp_path, self.file_path)

    def save_mutation(self, record: Any) -> None:
        mid = getattr(record, "mutation_id", None) or (record.get("mutation_id") if isinstance(record, dict) else None)
        if not mid:
            raise ValueError("Mutation record missing mutation_id")

        with self.acquire_lock():
            if self.file_path:
                self._load()
            self._records[mid] = record
            if mid not in self._order:
                self._order.append(mid)
            if self.file_path:
                self._persist()

    def get_mutation(self, mutation_id: str, tenant_id: Optional[str] = None) -> Optional[Any]:
        with self.acquire_lock():
            if self.file_path and not self._records:
                self._load()
            rec = self._records.get(mutation_id)
            if rec is None:
                return None
            rec_tenant = getattr(rec, "tenant_id", "default") if not isinstance(rec, dict) else rec.get("tenant_id", "default")
            if tenant_id is not None and tenant_id != rec_tenant:
                return None
            return rec

    def list_mutations(
        self,
        status: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Any]:
        with self.acquire_lock():
            if self.file_path and not self._records:
                self._load()
            results = []
            for mid in self._order:
                rec = self._records.get(mid)
                if rec is None:
                    continue
                rec_status = getattr(rec, "status", None) if not isinstance(rec, dict) else rec.get("status")
                if status is not None and rec_status != status:
                    continue
                rec_tenant = getattr(rec, "tenant_id", "default") if not isinstance(rec, dict) else rec.get("tenant_id", "default")
                if tenant_id is not None and rec_tenant != tenant_id:
                    continue
                results.append(rec)
            return results

    def update_status(
        self,
        mutation_id: str,
        new_status: str,
        audit_entry: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> bool:
        with self.acquire_lock():
            if self.file_path:
                self._load()
            rec = self._records.get(mutation_id)
            if rec is None:
                return False
            rec_tenant = getattr(rec, "tenant_id", "default") if not isinstance(rec, dict) else rec.get("tenant_id", "default")
            if tenant_id is not None and rec_tenant != tenant_id:
                return False

            if isinstance(rec, dict):
                rec["status"] = new_status
                if audit_entry:
                    rec.setdefault("audit_log", []).append(audit_entry)
            else:
                setattr(rec, "status", new_status)
                if audit_entry and hasattr(rec, "audit_log"):
                    rec.audit_log.append(audit_entry)

            if self.file_path:
                self._persist()
            return True
