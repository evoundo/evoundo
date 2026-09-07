"""SQLite storage backend for EvoUndo (recommended for local teams)."""

from __future__ import annotations
from contextlib import contextmanager
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, Generator, List, Optional

from evoundo.persistence.base import MutationStorageBackend

logger = logging.getLogger("evoundo.persistence.sqlite")


class SqliteStorage(MutationStorageBackend):
    """ACID-compliant SQLite storage backend with WAL mode and indexing."""

    def __init__(self, db_path: str = ".evoundo/evoundo.db") -> None:
        self.db_path = db_path
        self._mem_conn = None
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        else:
            self._mem_conn = sqlite3.connect(":memory:", timeout=10.0, check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute("PRAGMA busy_timeout=5000;")
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evoundo_mutations (
                    mutation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    timestamp REAL NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    record_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_evoundo_tenant_status 
                ON evoundo_mutations (tenant_id, status);
            """)
            conn.commit()

    @contextmanager
    def acquire_lock(self, timeout_sec: float = 10.0) -> Generator[None, None, None]:
        # SQLite handles concurrency via database-level busy handler and WAL transactions
        yield

    def save_mutation(self, record: Any) -> None:
        mid = getattr(record, "mutation_id", None) or (record.get("mutation_id") if isinstance(record, dict) else None)
        if not mid:
            raise ValueError("Mutation record missing mutation_id")

        tenant_id = getattr(record, "tenant_id", "default") if not isinstance(record, dict) else record.get("tenant_id", "default")
        status = getattr(record, "status", "ACTIVE") if not isinstance(record, dict) else record.get("status", "ACTIVE")
        ts = getattr(record, "timestamp", time.time()) if not isinstance(record, dict) else record.get("timestamp", time.time())
        desc = getattr(record, "description", "") if not isinstance(record, dict) else record.get("description", "")

        if hasattr(record, "to_dict") and callable(record.to_dict):
            payload = record.to_dict()
        elif isinstance(record, dict):
            payload = record
        else:
            payload = getattr(record, "__dict__", {"mutation_id": mid})

        raw_json = json.dumps(payload, default=str)
        now = time.time()

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO evoundo_mutations (mutation_id, tenant_id, status, timestamp, description, record_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (mutation_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    status = excluded.status,
                    description = excluded.description,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at;
            """, (mid, tenant_id, status, ts, desc, raw_json, now))
            conn.commit()

    def get_mutation(self, mutation_id: str, tenant_id: Optional[str] = None) -> Optional[Any]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            if tenant_id is not None:
                cur.execute(
                    "SELECT record_json FROM evoundo_mutations WHERE mutation_id = ? AND tenant_id = ?",
                    (mutation_id, tenant_id),
                )
            else:
                cur.execute(
                    "SELECT record_json FROM evoundo_mutations WHERE mutation_id = ?",
                    (mutation_id,),
                )
            row = cur.fetchone()
            if not row:
                return None
            return json.loads(row["record_json"])

    def list_mutations(
        self,
        status: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Any]:
        query = "SELECT record_json FROM evoundo_mutations WHERE 1=1"
        params: List[Any] = []
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if tenant_id is not None:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY timestamp ASC"

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            return [json.loads(r["record_json"]) for r in rows]

    def update_status(
        self,
        mutation_id: str,
        new_status: str,
        audit_entry: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> bool:
        record = self.get_mutation(mutation_id, tenant_id=tenant_id)
        if not record:
            return False

        if isinstance(record, dict):
            record["status"] = new_status
            if audit_entry:
                record.setdefault("audit_log", []).append(audit_entry)
        raw_json = json.dumps(record, default=str)
        now = time.time()

        with self._get_connection() as conn:
            cur = conn.cursor()
            if tenant_id is not None:
                cur.execute("""
                    UPDATE evoundo_mutations
                    SET status = ?, record_json = ?, updated_at = ?
                    WHERE mutation_id = ? AND tenant_id = ?
                """, (new_status, raw_json, now, mutation_id, tenant_id))
            else:
                cur.execute("""
                    UPDATE evoundo_mutations
                    SET status = ?, record_json = ?, updated_at = ?
                    WHERE mutation_id = ?
                """, (new_status, raw_json, now, mutation_id))
            conn.commit()
            return cur.rowcount > 0
