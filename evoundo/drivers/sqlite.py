"""SQLite Surface Driver with automated pre-state capture and inverse generation."""

from __future__ import annotations
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from evoundo.context import get_current_context


class UnsupportedSQLError(ValueError):
    """Raised when an SQL operation cannot be automatically inverted and fails closed."""
    pass


class SQLiteDriver:
    """Manages SQLite execution with automated pre-state witness and inverse query derivation."""

    UPDATE_REGEX = re.compile(r"^UPDATE\s+([a-zA-Z0-9_]+)\s+SET\s+(.+?)\s+WHERE\s+(.+)$", re.IGNORECASE | re.DOTALL)

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> SQLiteDriver:
        self.conn = sqlite3.connect(self.db_path)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            try:
                if exc_type is None:
                    ctx = get_current_context()
                    active_harness = getattr(ctx, "active_harness", None)
                    if active_harness and hasattr(active_harness, "reconciler") and active_harness.reconciler:
                        m_id = getattr(ctx, "active_mutation_id", None) or getattr(ctx, "logical_mutation_id", None)
                        if m_id and getattr(ctx, "active_recovery_ops", None):
                            entry = active_harness.reconciler.get_entry(m_id)
                            if entry:
                                entry.recovery_ops = [o.to_dict() if hasattr(o, "to_dict") else o for o in ctx.active_recovery_ops]
                                if ctx.active_witness:
                                    if entry.witness_data is None:
                                        entry.witness_data = {}
                                    entry.witness_data.update(ctx.active_witness)
                                active_harness.reconciler._mark_dirty(entry.identity.logical_mutation_id)
                                active_harness.reconciler._persist_to_disk()
                    self.conn.commit()
                else:
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    self.conn.close()
                except Exception:
                    pass

    def execute(self, sql: str, params: tuple = ()) -> Any:
        """Execute SQL query with automatic pre-state witness capture and inverse generation."""
        if not self.conn:
            raise RuntimeError("SQLiteDriver must be used within a 'with' context block.")

        cursor = self.conn.cursor()
        trimmed_sql = sql.strip()

        # Check for injection in UPDATE statements
        if trimmed_sql.upper().startswith("UPDATE"):
            if ";" in trimmed_sql.rstrip(";") or "--" in trimmed_sql or "/*" in trimmed_sql:
                parts_before_set = trimmed_sql.upper().split("SET")[0]
                if ";" in parts_before_set or "--" in parts_before_set or "/*" in parts_before_set:
                    raise ValueError(f"Invalid SQL table identifier: potential SQL injection attempt in '{trimmed_sql}'")
                raise ValueError(f"Invalid SQL column identifier: potential SQL injection attempt in '{trimmed_sql}'")

        # Parse UPDATE statements
        match = self.UPDATE_REGEX.match(trimmed_sql)
        if match:
            table_name = match.group(1)
            set_clause = match.group(2)
            where_clause = match.group(3)

            # Validate table name
            _ident_pattern = re.compile(r"^[A-Za-z0-9_#$]+$")
            _danger_pattern = re.compile(r"[\s;\'\"\[\]\-\-\/\*\\\x00]")
            if _danger_pattern.search(table_name) or not _ident_pattern.match(table_name):
                raise ValueError(f"Invalid SQL table identifier: potential SQL injection attempt in '{table_name}'")

            # Parse columns being set (e.g., "status = ?")
            set_assignments = [col.strip() for col in set_clause.split(",")]
            set_columns = []
            for assign in set_assignments:
                parts = assign.split("=")
                col_name = parts[0].strip()
                if _danger_pattern.search(col_name) or not _ident_pattern.match(col_name):
                    raise ValueError(f"Invalid SQL column identifier: potential SQL injection attempt in '{col_name}'")
                set_columns.append(col_name)

            # Reject modifications to primary key columns or rowid aliases
            pk_cols = set()
            try:
                quoted_table_for_pragma = '"' + table_name.replace('"', '""') + '"'
                cursor.execute(f"PRAGMA table_info({quoted_table_for_pragma})")
                for col_info in cursor.fetchall():
                    if len(col_info) >= 6 and col_info[5] > 0:
                        pk_cols.add(col_info[1].lower())
            except Exception:
                pass

            for col in set_columns:
                lower_c = col.strip('"`[]').lower()
                if lower_c in ("rowid", "oid", "_rowid_") or lower_c in pk_cols:
                    raise UnsupportedSQLError(
                        f"Updating primary key or rowid column '{col}' is outside the automatic inversion envelope "
                        f"because row identity is mutable. Primary key modifications must be rejected before mutation."
                    )

            num_set = len(set_columns)
            where_params = params[num_set:]

            # Query pre-state witness via SELECT with safely quoted identifiers and stable rowid
            quoted_cols = ['"' + c.replace('"', '""') + '"' for c in set_columns]
            quoted_table = '"' + table_name.replace('"', '""') + '"'
            select_sql = f"SELECT rowid, {', '.join(quoted_cols)} FROM {quoted_table} WHERE {where_clause}"
            cursor.execute(select_sql, where_params)
            pre_rows = cursor.fetchall()

            if pre_rows:
                row_inverses = []
                for r in pre_rows:
                    rid = r[0]
                    vals = list(r[1:])
                    row_inverses.append({"rowid": rid, "pre_values": vals, "set_columns": set_columns})

                witness_val = [r["pre_values"][0] if len(r["pre_values"]) == 1 else r["pre_values"] for r in row_inverses]
                if len(witness_val) == 1:
                    witness_val = witness_val[0]

                db_path = self.db_path

                def _inverse_fn(witness, result):
                    conn = sqlite3.connect(db_path)
                    try:
                        cur = conn.cursor()
                        for r in row_inverses:
                            rid = r["rowid"]
                            cols = r["set_columns"]
                            vals = r["pre_values"]
                            inv_set = ", ".join(['"' + c.replace('"', '""') + '" = ?' for c in cols])
                            cur.execute(f"UPDATE {quoted_table} SET {inv_set} WHERE rowid = ?", tuple(vals) + (rid,))
                        conn.commit()
                    finally:
                        conn.close()

                # Register witness and inverse with active EvoUndo context
                ctx = get_current_context()
                target_key = ctx.active_target or f"database:{table_name}"
                ctx.active_target = target_key
                ctx.active_witness[target_key] = witness_val
                ctx.active_inverse_fn = _inverse_fn

                from evoundo.recovery.operations import DriverRecoveryOp
                op = DriverRecoveryOp(
                    driver_type="sqlite",
                    target=target_key,
                    operation="update",
                    parameters={
                        "db_path": os.path.abspath(db_path),
                        "table_name": table_name,
                        "set_columns": set_columns,
                        "where_clause": where_clause,
                        "where_params": list(where_params),
                        "row_inverses": row_inverses,
                    },
                    witness_data=witness_val,
                    inverse_fn=lambda s, w: _inverse_fn(witness_val, None),
                )
                ctx.active_recovery_ops.append(op)
                active_harness = getattr(ctx, "active_harness", None)
                if active_harness and hasattr(active_harness, "reconciler") and active_harness.reconciler:
                    m_id = getattr(ctx, "active_mutation_id", None) or getattr(ctx, "logical_mutation_id", None)
                    if m_id:
                        entry = active_harness.reconciler.get_entry(m_id)
                        if entry:
                            entry.recovery_ops = [o.to_dict() if hasattr(o, "to_dict") else o for o in ctx.active_recovery_ops]
                            if ctx.active_witness:
                                if entry.witness_data is None:
                                    entry.witness_data = {}
                                entry.witness_data.update(ctx.active_witness)

            # Execute the forward UPDATE
            cursor.execute(sql, params)
            return {"rows_affected": cursor.rowcount, "table": table_name}

        # If query is not a supported single-table UPDATE, fail closed if an active recovery requires inverse
        ctx = get_current_context()
        if ctx.active_recovery_level in ("invert", "dependency_aware"):
            raise UnsupportedSQLError(
                f"SQL query '{sql}' is outside the automatic inversion envelope. "
                "Supply an explicit inverse function or use single-table UPDATE statements."
            )

        # Normal execution for unsupported/read queries
        cursor.execute(sql, params)
        return cursor.fetchall()


def sqlite(db_path: str) -> SQLiteDriver:
    """Convenience context manager constructor for SQLite."""
    return SQLiteDriver(db_path)
