"""Microsoft SQL Server mutation surface driver for EvoUndo.

Provides relational DML recovery, IDENTITY_INSERT handling,
downstream conflict refusal, and physical SQL Server state verification.
Secured against SQL injection via strict identifier validation, dialect bracket quoting,
and complete parameterization.
"""

from __future__ import annotations
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:
    from sqlalchemy import text
except ImportError:
    def text(s: str) -> str:  # type: ignore
        return s
from evoundo.recovery.driver_registry import DriverRegistry

logger = logging.getLogger("evoundo.surfaces.sqlserver")

# Strict regex whitelist for SQL Server identifiers (table, schema, column)
# Disallows whitespace, semicolons, brackets, quotes, dashes, comment delimiters, and control chars.
SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_#$]+$")
DANGEROUS_SQL_CHARS_PATTERN = re.compile(r"[\s;\'\"\[\]\-\-\/\*\\\x00]")


def validate_sql_identifier(ident: str, allow_multipart: bool = False) -> str:
    """Validate that an SQL identifier contains only allowed characters.

    Raises ValueError on SQL injection payloads, stacked queries, quotes, or disallowed characters.
    """
    if not ident or not isinstance(ident, str):
        raise ValueError("SQL identifier cannot be empty and must be a string")

    # Check for raw dangerous characters or comments
    if DANGEROUS_SQL_CHARS_PATTERN.search(ident):
        raise ValueError(f"Invalid SQL identifier: potential SQL injection attempt detected in '{ident}'")

    if allow_multipart:
        parts = ident.split(".")
        if len(parts) > 3 or any(not p for p in parts):
            raise ValueError(f"Invalid multi-part SQL identifier: '{ident}'")
        for p in parts:
            if not SQL_IDENTIFIER_PATTERN.match(p):
                raise ValueError(f"Invalid SQL identifier part '{p}' in '{ident}'")
    else:
        if not SQL_IDENTIFIER_PATTERN.match(ident):
            raise ValueError(f"Invalid SQL identifier: '{ident}'")

    return ident


def quote_identifier_mssql(ident: str, is_table: bool = False) -> str:
    """Validate and bracket-quote an MS SQL Server identifier (e.g. [dbo].[orders] or [col_name])."""
    validate_sql_identifier(ident, allow_multipart=is_table)
    if is_table and "." in ident:
        parts = ident.split(".")
        return ".".join(f"[{p.replace(']', ']]')}]" for p in parts)
    return f"[{ident.replace(']', ']]')}]"


class SqlServerSurfaceDriver:
    """Production Microsoft SQL Server mutation driver with IDENTITY and conflict support."""

    DRIVER_TYPE = "sqlserver"

    @classmethod
    def register(cls) -> None:
        """Register this driver with the central DriverRegistry."""
        DriverRegistry.register_driver(
            driver_type=cls.DRIVER_TYPE,
            executor=cls.execute_recovery,
            verifier=cls.verify_recovery,
        )
        logger.info("Registered '%s' surface driver with DriverRegistry", cls.DRIVER_TYPE)

    @classmethod
    def execute_recovery(cls, op: Any, witness: Any) -> None:
        """Execute physical rollback of an MS SQL Server mutation."""
        params = op.parameters or {}
        db_url = params.get("db_url") or os.environ.get(
            "EVOUNDO_MSSQL_URL", "mssql+pymssql://sa:Password123!@127.0.0.1:1433/evoundo"
        )
        table_name = params.get("table_name")
        pk_column = params.get("pk_column", "id")
        pk_value = params.get("pk_value")
        has_identity = params.get("has_identity", False)
        operation = (op.operation or params.get("operation", "INSERT")).upper()

        w = op.witness_data or params.get("witness", {})
        if hasattr(w, "data") and isinstance(w.data, dict):
            w = w.data
        elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
            w = witness.data

        if not table_name:
            raise ValueError("SQL Server recovery requires 'table_name'")

        # Validate identifiers and generate dialect-safe quoted tokens
        quoted_table = quote_identifier_mssql(table_name, is_table=True)
        quoted_pk = quote_identifier_mssql(pk_column)

        # Validate column names upfront from witness/params before engine creation
        if operation == "UPDATE":
            orig_cols = w.get("row_snapshot") or w.get("old_values", {})
            for col in orig_cols:
                validate_sql_identifier(col)
        elif operation == "DELETE":
            deleted_row = w.get("row_snapshot") or w.get("deleted_values", {})
            for col in deleted_row:
                validate_sql_identifier(col)
        elif operation == "INSERT":
            for col in params.get("inserted_values", {}):
                validate_sql_identifier(col)

        engine = DriverRegistry.get_or_create_engine(db_url)
        lock_hint = "WITH (UPDLOCK, ROWLOCK)" if engine.dialect.name == "mssql" else ""

        try:
            with engine.begin() as conn:
                if operation == "INSERT":
                    # If cascading child records were inserted, delete them in reverse topological order first
                    cascaded_records = params.get("cascaded_records") or w.get("cascaded_records", [])
                    for child in reversed(cascaded_records):
                        child_table = child.get("table_name")
                        child_pk_col = child.get("pk_column", "id")
                        child_pk_val = child.get("pk_value")
                        if child_table and child_pk_val is not None:
                            q_child_table = quote_identifier_mssql(child_table, is_table=True)
                            q_child_pk = quote_identifier_mssql(child_pk_col)
                            conn.execute(
                                text(f"DELETE FROM {q_child_table} WHERE {q_child_pk} = :pk"),
                                {"pk": child_pk_val},
                            )

                    # Check downstream row modification before deleting parent
                    expected_inserted = params.get("inserted_values", {})
                    if pk_value is not None:
                        check_q = text(f"SELECT * FROM {quoted_table} {lock_hint} WHERE {quoted_pk} = :pk")
                        row = conn.execute(check_q, {"pk": pk_value}).mappings().first()
                        if row and expected_inserted:
                            for col, val in expected_inserted.items():
                                validate_sql_identifier(col)
                                if col != pk_column and row.get(col) != val:
                                    raise ValueError(
                                        f"CONFLICT_DETECTED: SQL Server row '{pk_value}' in '{table_name}' "
                                        f"was modified downstream (column '{col}' changed)"
                                    )
                        del_q = text(f"DELETE FROM {quoted_table} WHERE {quoted_pk} = :pk")
                        conn.execute(del_q, {"pk": pk_value})
                        logger.info("Deleted inserted SQL Server row '%s' from '%s'", pk_value, table_name)

                elif operation == "UPDATE":
                    orig_cols = w.get("row_snapshot") or w.get("old_values", {})
                    if pk_value is not None and orig_cols:
                        # Check downstream conflict
                        modified_cols = params.get("modified_columns", [])
                        expected_post = params.get("expected_post_values", {})
                        check_q = text(f"SELECT * FROM {quoted_table} {lock_hint} WHERE {quoted_pk} = :pk")
                        curr_row = conn.execute(check_q, {"pk": pk_value}).mappings().first()
                        if curr_row is None:
                            raise ValueError(f"CONFLICT_DETECTED: Row '{pk_value}' was deleted downstream")
                        for col in modified_cols:
                            validate_sql_identifier(col)
                            if col in expected_post and curr_row.get(col) != expected_post[col]:
                                raise ValueError(
                                    f"CONFLICT_DETECTED: Column '{col}' on row '{pk_value}' was modified downstream"
                                )

                        set_clauses = []
                        bind_params: Dict[str, Any] = {"pk": pk_value}
                        for idx, (col, val) in enumerate(orig_cols.items()):
                            if col != pk_column:
                                validate_sql_identifier(col)
                                q_col = quote_identifier_mssql(col)
                                param_key = f"col_{idx}"
                                set_clauses.append(f"{q_col} = :{param_key}")
                                bind_params[param_key] = val

                        if set_clauses:
                            sql = f"UPDATE {quoted_table} SET {', '.join(set_clauses)} WHERE {quoted_pk} = :pk"
                            conn.execute(text(sql), bind_params)
                            logger.info("Restored updated SQL Server row '%s' in '%s'", pk_value, table_name)

                elif operation == "DELETE":
                    deleted_row = w.get("row_snapshot") or w.get("deleted_values", {})
                    if deleted_row:
                        cols = list(deleted_row.keys())
                        quoted_cols = []
                        val_placeholders = []
                        bind_params = {}
                        for idx, col in enumerate(cols):
                            validate_sql_identifier(col)
                            quoted_cols.append(quote_identifier_mssql(col))
                            param_name = f"val_{idx}"
                            val_placeholders.append(f":{param_name}")
                            bind_params[param_name] = deleted_row[col]

                        sql = f"INSERT INTO {quoted_table} ({', '.join(quoted_cols)}) VALUES ({', '.join(val_placeholders)})"

                        if has_identity and engine.dialect.name == "mssql":
                            conn.execute(text(f"SET IDENTITY_INSERT {quoted_table} ON"))
                        try:
                            conn.execute(text(sql), bind_params)
                        finally:
                            if has_identity and engine.dialect.name == "mssql":
                                conn.execute(text(f"SET IDENTITY_INSERT {quoted_table} OFF"))
                        logger.info("Re-inserted deleted SQL Server row '%s' into '%s'", pk_value, table_name)

                    # Restore foreign key cascaded records in topological order (Parent -> Child -> Grandchild)
                    cascaded_records = params.get("cascaded_records") or w.get("cascaded_records", [])
                    for child in cascaded_records:
                        child_table = child.get("table_name")
                        child_row = child.get("row") or child.get("deleted_values", {})
                        child_identity = child.get("has_identity", False)
                        if child_table and child_row:
                            q_child_table = quote_identifier_mssql(child_table, is_table=True)
                            c_cols = list(child_row.keys())
                            q_c_cols = [quote_identifier_mssql(c) for c in c_cols]
                            c_placeholders = [f":c_val_{i}" for i in range(len(c_cols))]
                            c_bind = {f"c_val_{i}": child_row[c] for i, c in enumerate(c_cols)}
                            c_sql = f"INSERT INTO {q_child_table} ({', '.join(q_c_cols)}) VALUES ({', '.join(c_placeholders)})"
                            if child_identity and engine.dialect.name == "mssql":
                                conn.execute(text(f"SET IDENTITY_INSERT {q_child_table} ON"))
                            try:
                                conn.execute(text(c_sql), c_bind)
                            finally:
                                if child_identity and engine.dialect.name == "mssql":
                                    conn.execute(text(f"SET IDENTITY_INSERT {q_child_table} OFF"))

                else:
                    raise NotImplementedError(f"Unsupported SQL Server recovery operation: {operation}")

        except Exception as e:
            err_msg = str(e)
            is_deadlock = (
                "1205" in err_msg
                or "deadlock" in err_msg.lower()
                or (
                    hasattr(e, "orig")
                    and getattr(e.orig, "args", None)
                    and len(e.orig.args) > 0
                    and e.orig.args[0] == 1205
                )
            )
            if is_deadlock:
                logger.error("SQL Server deadlock (Error 1205) victim on '%s': %s", table_name, e)
                raise ValueError(
                    f"DEADLOCK_DETECTED: SQL Server Error 1205 transaction deadlock victim on '{table_name}': {e}"
                ) from e
            raise

    @classmethod
    def verify_recovery(cls, op: Any, witness: Any) -> bool:
        """Physically verify external SQL Server state matches expected recovery condition."""
        params = op.parameters or {}
        db_url = params.get("db_url") or os.environ.get(
            "EVOUNDO_MSSQL_URL", "mssql+pymssql://sa:Password123!@127.0.0.1:1433/evoundo"
        )
        table_name = params.get("table_name")
        pk_column = params.get("pk_column", "id")
        pk_value = params.get("pk_value")
        operation = (op.operation or params.get("operation", "INSERT")).upper()

        w = op.witness_data or params.get("witness", {})
        if hasattr(w, "data") and isinstance(w.data, dict):
            w = w.data
        elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
            w = witness.data

        if not table_name or pk_value is None:
            return False

        try:
            quoted_table = quote_identifier_mssql(table_name, is_table=True)
            quoted_pk = quote_identifier_mssql(pk_column)
            engine = DriverRegistry.get_or_create_engine(db_url)
            with engine.connect() as conn:
                q = text(f"SELECT * FROM {quoted_table} WHERE {quoted_pk} = :pk")
                row = conn.execute(q, {"pk": pk_value}).mappings().first()

                if operation == "INSERT":
                    if row is not None:
                        return False
                    # Also verify cascaded records if any
                    cascaded_records = params.get("cascaded_records") or w.get("cascaded_records", [])
                    for child in cascaded_records:
                        c_table = child.get("table_name")
                        c_pk_col = child.get("pk_column", "id")
                        c_pk_val = child.get("pk_value")
                        if c_table and c_pk_val is not None:
                            qc_table = quote_identifier_mssql(c_table, is_table=True)
                            qc_pk = quote_identifier_mssql(c_pk_col)
                            c_row = conn.execute(text(f"SELECT * FROM {qc_table} WHERE {qc_pk} = :pk"), {"pk": c_pk_val}).mappings().first()
                            if c_row is not None:
                                return False
                    return True

                elif operation == "UPDATE":
                    if row is None:
                        return False
                    expected = w.get("row_snapshot") or w.get("old_values", {})
                    for col, val in expected.items():
                        validate_sql_identifier(col)
                        if row.get(col) != val and str(row.get(col)) != str(val):
                            return False
                    return True

                elif operation == "DELETE":
                    if row is None:
                        return False
                    # Verify cascaded child records exist
                    cascaded_records = params.get("cascaded_records") or w.get("cascaded_records", [])
                    for child in cascaded_records:
                        c_table = child.get("table_name")
                        c_pk_col = child.get("pk_column", "id")
                        c_pk_val = child.get("pk_value")
                        if c_table and c_pk_val is not None:
                            qc_table = quote_identifier_mssql(c_table, is_table=True)
                            qc_pk = quote_identifier_mssql(c_pk_col)
                            c_row = conn.execute(text(f"SELECT * FROM {qc_table} WHERE {qc_pk} = :pk"), {"pk": c_pk_val}).mappings().first()
                            if c_row is None:
                                return False
                    return True
            return True
        except Exception as e:
            logger.error("Failed SQL Server physical recovery verification: %s", e)
            return False
