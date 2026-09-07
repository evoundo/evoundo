"""Central Driver Registry for durable, cross-process recovery execution and external state verification."""

from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("evoundo.recovery.driver_registry")


def _decode_b64(val: Any) -> Any:
    if isinstance(val, dict) and "__b64__" in val and len(val) == 1:
        return base64.b64decode(val["__b64__"])
    if isinstance(val, dict):
        res = {}
        for k, v in val.items():
            decoded_k = k
            if isinstance(k, str) and (k.startswith("__b64__:") or k.startswith("b64:")):
                prefix_len = len("__b64__:") if k.startswith("__b64__:") else len("b64:")
                try:
                    decoded_k = base64.b64decode(k[prefix_len:])
                except Exception:
                    pass
            res[decoded_k] = _decode_b64(v)
        return res
    if isinstance(val, list):
        return [_decode_b64(x) for x in val]
    return val


def _to_bytes(v: Any) -> bytes:
    if isinstance(v, bytes):
        return v
    return str(v).encode("utf-8")


REMOVE_UNIQUE_LUA = """
local key = KEYS[1]
local val = ARGV[1]
local items = redis.call('LRANGE', key, 0, -1)
local count = 0
for i, item in ipairs(items) do
    if item == val then
        count = count + 1
    end
end
if count > 1 then
    return -1
end
if count == 1 then
    redis.call('LREM', key, 1, val)
    return 1
end
return 0
"""


class DriverRegistry:
    """Registry mapping driver types to durable execution and verification handlers."""

    _executors: Dict[str, Callable[[Any, Any], None]] = {}
    _verifiers: Dict[str, Callable[[Any, Any], bool]] = {}
    _engine_cache: Dict[str, Any] = {}
    _redis_cache: Dict[str, Any] = {}

    @classmethod
    def register_driver(
        cls,
        driver_type: str,
        executor: Callable[[Any, Any], None],
        verifier: Callable[[Any, Any], bool],
    ) -> None:
        """Register custom recovery executor and verifier for a driver type."""
        cls._executors[driver_type] = executor
        cls._verifiers[driver_type] = verifier

    @classmethod
    def register_engine(cls, url: str, engine: Any) -> None:
        """Cache an active database engine instance to avoid reconnect overhead."""
        cls._engine_cache[url] = engine

    @classmethod
    def get_or_create_engine(cls, url: str) -> Any:
        """Retrieve cached engine or construct a new SQLAlchemy engine."""
        if url in cls._engine_cache:
            return cls._engine_cache[url]
        try:
            from sqlalchemy import create_engine
            engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
            cls._engine_cache[url] = engine
            return engine
        except Exception as e:
            logger.error("Failed to construct SQLAlchemy engine for %s: %s", url, e)
            raise

    @classmethod
    def get_or_create_redis(cls, url: str) -> Any:
        """Retrieve cached Redis client or construct a new connection."""
        if url in cls._redis_cache:
            return cls._redis_cache[url]
        import redis
        client = redis.Redis.from_url(url, decode_responses=False)
        cls._redis_cache[url] = client
        return client

    @classmethod
    def execute_recovery(cls, op: Any, witness: Any) -> None:
        """Execute recovery for a DriverRecoveryOp using the registered or built-in handler."""
        driver_type = getattr(op, "driver_type", "")
        if driver_type in cls._executors:
            cls._executors[driver_type](op, witness)
            return

        # Built-in handlers
        if driver_type == "json_config":
            cls._execute_json_config(op, witness)
        elif driver_type == "audit_file":
            cls._execute_audit_file(op, witness)
        elif driver_type == "sqlite":
            cls._execute_sqlite(op, witness)
        elif driver_type == "orm":
            cls._execute_orm(op, witness)
        elif driver_type == "redis":
            cls._execute_redis(op, witness)
        elif driver_type == "k8s":
            cls._execute_k8s(op, witness)
        else:
            raise NotImplementedError(f"No recovery executor registered for driver type '{driver_type}'")

    @classmethod
    def verify_recovery(cls, op: Any, witness: Any) -> bool:
        """Verify external state matches witness for a DriverRecoveryOp."""
        driver_type = getattr(op, "driver_type", "")
        if driver_type in cls._verifiers:
            return cls._verifiers[driver_type](op, witness)

        # Built-in verifiers
        if driver_type == "json_config":
            return cls._verify_json_config(op, witness)
        elif driver_type == "audit_file":
            return cls._verify_audit_file(op, witness)
        elif driver_type == "sqlite":
            return cls._verify_sqlite(op, witness)
        elif driver_type == "orm":
            return cls._verify_orm(op, witness)
        elif driver_type == "redis":
            return cls._verify_redis(op, witness)
        elif driver_type == "k8s":
            return cls._verify_k8s(op, witness)
        else:
            logger.warning("No verifier registered for driver type '%s'", driver_type)
            return False


    # ---------------------------------------------------------------------- #
    # Built-in Handlers: JSON Config
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _execute_json_config(op: Any, witness: Any) -> None:
        manifest_path = op.parameters.get("manifest_path")
        key = op.parameters.get("key")
        key_existed = op.parameters.get("key_existed", op.witness_data is not None)
        witness_val = op.witness_data

        if not manifest_path:
            return

        if not os.path.exists(manifest_path):
            if key_existed and witness_val is not None:
                os.makedirs(os.path.dirname(os.path.abspath(manifest_path)), exist_ok=True)
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump({"service": {"env": {key: witness_val}}}, f, indent=2)
            return

        with open(manifest_path, "r", encoding="utf-8") as f:
            try:
                manifest = json.load(f)
            except Exception:
                manifest = {}

        env_dict = manifest.setdefault("service", {}).setdefault("env", {})
        if not key_existed or witness_val is None:
            env_dict.pop(key, None)
        else:
            env_dict[key] = witness_val

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

    @staticmethod
    def _verify_json_config(op: Any, witness: Any) -> bool:
        manifest_path = op.parameters.get("manifest_path")
        key = op.parameters.get("key")
        key_existed = op.parameters.get("key_existed", op.witness_data is not None)
        expected_val = op.witness_data

        if not manifest_path:
            return False
        if not os.path.exists(manifest_path):
            return not key_existed

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            env_dict = manifest.get("service", {}).get("env", {})
            if not key_existed or expected_val is None:
                return key not in env_dict
            actual = env_dict.get(key)
            return actual == expected_val or str(actual) == str(expected_val)
        except Exception as e:
            logger.error("Failed to verify json_config: %s", e)
            return False

    # ---------------------------------------------------------------------- #
    # Built-in Handlers: Audit File
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _execute_audit_file(op: Any, witness: Any) -> None:
        file_path = op.parameters.get("file_path")
        offset = int(op.witness_data if op.witness_data is not None else 0)
        if file_path and os.path.exists(file_path):
            with open(file_path, "r+", encoding="utf-8") as f:
                f.truncate(offset)

    @staticmethod
    def _verify_audit_file(op: Any, witness: Any) -> bool:
        file_path = op.parameters.get("file_path")
        offset = int(op.witness_data if op.witness_data is not None else 0)
        if not file_path:
            return False
        if not os.path.exists(file_path):
            return offset == 0
        return os.path.getsize(file_path) == offset

    # ---------------------------------------------------------------------- #
    # Built-in Handlers: SQLite
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _execute_sqlite(op: Any, witness: Any) -> None:
        db_path = op.parameters.get("db_path")
        table = op.parameters.get("table_name")
        quoted_table = '"' + table.replace('"', '""') + '"'
        row_inverses = op.parameters.get("row_inverses")

        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            if row_inverses:
                for r in row_inverses:
                    rid = r["rowid"]
                    cols = r.get("set_columns", op.parameters.get("set_columns", []))
                    vals = r["pre_values"]
                    inv_set = ", ".join(['"' + col.replace('"', '""') + '" = ?' for col in cols])
                    cur.execute(f"UPDATE {quoted_table} SET {inv_set} WHERE rowid = ?", tuple(vals) + (rid,))
            else:
                cols = op.parameters.get("set_columns", [])
                where = op.parameters.get("where_clause", "")
                where_p = tuple(op.parameters.get("where_params", []))
                pre_vals = op.witness_data if isinstance(op.witness_data, (list, tuple)) else [op.witness_data]
                inv_set = ", ".join(['"' + col.replace('"', '""') + '" = ?' for col in cols])
                inv_sql = f"UPDATE {quoted_table} SET {inv_set} WHERE {where}"
                inv_params = tuple(pre_vals) + where_p
                cur.execute(inv_sql, inv_params)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _verify_sqlite(op: Any, witness: Any) -> bool:
        db_path = op.parameters.get("db_path")
        table = op.parameters.get("table_name")
        quoted_table = '"' + table.replace('"', '""') + '"'
        row_inverses = op.parameters.get("row_inverses")

        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            if row_inverses:
                for r in row_inverses:
                    rid = r["rowid"]
                    cols = r.get("set_columns", op.parameters.get("set_columns", []))
                    vals = r["pre_values"]
                    quoted_cols = ['"' + col.replace('"', '""') + '"' for col in cols]
                    cur.execute(f"SELECT {', '.join(quoted_cols)} FROM {quoted_table} WHERE rowid = ?", (rid,))
                    row = cur.fetchone()
                    if row is None or list(row) != list(vals):
                        return False
                return True
            else:
                cols = op.parameters.get("set_columns", [])
                where = op.parameters.get("where_clause", "")
                where_p = tuple(op.parameters.get("where_params", []))
                pre_vals = op.witness_data if isinstance(op.witness_data, (list, tuple)) else [op.witness_data]
                quoted_cols = ['"' + col.replace('"', '""') + '"' for col in cols]
                check_sql = f"SELECT {', '.join(quoted_cols)} FROM {quoted_table} WHERE {where}"
                cur.execute(check_sql, where_p)
                row = cur.fetchone()
                if row is None:
                    return False
                row_vals = list(row)
                return row_vals == list(pre_vals)
        except Exception as e:
            logger.error("Failed to verify sqlite: %s", e)
            return False
        finally:
            conn.close()

    # ---------------------------------------------------------------------- #
    # Built-in Handlers: ORM (SQLAlchemy / PostgreSQL / SQLite)
    # ---------------------------------------------------------------------- #

    @classmethod
    def _execute_orm(cls, op: Any, witness: Any) -> None:
        from sqlalchemy import text
        db_url = op.parameters.get("db_url")
        table_name = op.parameters.get("table_name")
        pk = op.parameters.get("pk")
        pk_cols = op.parameters.get("pk_cols", ["id"])
        operation = op.operation or op.parameters.get("op", "UPDATE")
        witness_dict = op.witness_data or op.parameters.get("witness", {})

        if not db_url or not table_name:
            logger.warning("Missing db_url or table_name for ORM recovery execution.")
            return

        engine = cls.get_or_create_engine(db_url)
        preparer = getattr(engine.dialect, "identifier_preparer", None)
        q = preparer.quote_identifier if preparer else (lambda s: f'"{s}"')
        q_table = q(table_name)

        with engine.begin() as conn:
            if operation == "UPDATE":
                if not isinstance(witness_dict, dict) or not witness_dict:
                    return
                set_parts = [f'{q(col)} = :{col}' for col in witness_dict.keys()]
                set_clause = ", ".join(set_parts)
                where_clause = " AND ".join([f'{q(col)} = :pk_{i}' for i, col in enumerate(pk_cols)])
                stmt = text(f'UPDATE {q_table} SET {set_clause} WHERE {where_clause}')
                params = {**witness_dict}
                if len(pk_cols) == 1:
                    params["pk_0"] = pk
                else:
                    for i, val in enumerate(pk if isinstance(pk, (list, tuple)) else [pk]):
                        params[f"pk_{i}"] = val
                conn.execute(stmt, params)

            elif operation == "INSERT":
                # Invert INSERT -> DELETE
                where_clause = " AND ".join([f'{q(col)} = :pk_{i}' for i, col in enumerate(pk_cols)])
                stmt = text(f'DELETE FROM {q_table} WHERE {where_clause}')
                params = {"pk_0": pk} if len(pk_cols) == 1 else {f"pk_{i}": v for i, v in enumerate(pk)}
                conn.execute(stmt, params)

            elif operation == "DELETE":
                # Invert DELETE -> INSERT
                if not isinstance(witness_dict, dict) or not witness_dict:
                    return
                cols = list(witness_dict.keys())
                col_str = ", ".join([q(c) for c in cols])
                val_str = ", ".join([f':{c}' for c in cols])
                stmt = text(f'INSERT INTO {q_table} ({col_str}) VALUES ({val_str})')
                conn.execute(stmt, witness_dict)

    @classmethod
    def _verify_orm(cls, op: Any, witness: Any) -> bool:
        from sqlalchemy import text
        db_url = op.parameters.get("db_url")
        table_name = op.parameters.get("table_name")
        pk = op.parameters.get("pk")
        pk_cols = op.parameters.get("pk_cols", ["id"])
        operation = op.operation or op.parameters.get("op", "UPDATE")
        witness_dict = op.witness_data or op.parameters.get("witness", {})

        if not db_url or not table_name:
            return False

        try:
            engine = cls.get_or_create_engine(db_url)
            preparer = getattr(engine.dialect, "identifier_preparer", None)
            q = preparer.quote_identifier if preparer else (lambda s: f'"{s}"')
            q_table = q(table_name)

            with engine.connect() as conn:
                where_clause = " AND ".join([f'{q(col)} = :pk_{i}' for i, col in enumerate(pk_cols)])
                params = {"pk_0": pk} if len(pk_cols) == 1 else {f"pk_{i}": v for i, v in enumerate(pk)}

                if operation == "UPDATE":
                    if not isinstance(witness_dict, dict) or not witness_dict:
                        return True
                    col_str = ", ".join([q(col) for col in witness_dict.keys()])
                    stmt = text(f'SELECT {col_str} FROM {q_table} WHERE {where_clause}')
                    row = conn.execute(stmt, params).fetchone()
                    if row is None:
                        return False
                    row_map = dict(row._mapping)
                    for col, expected in witness_dict.items():
                        actual = row_map.get(col)
                        if actual != expected:
                            matched = False
                            try:
                                if abs(float(actual) - float(expected)) <= 1e-6:
                                    matched = True
                            except (ValueError, TypeError):
                                pass
                            if not matched and str(actual) == str(expected):
                                matched = True
                            if not matched:
                                return False
                    return True

                elif operation == "INSERT":
                    # Row must NOT exist
                    stmt = text(f'SELECT 1 FROM {q_table} WHERE {where_clause}')
                    row = conn.execute(stmt, params).fetchone()
                    return row is None

                elif operation == "DELETE":
                    # Row MUST exist and all restored columns must match witness
                    if not isinstance(witness_dict, dict) or not witness_dict:
                        stmt = text(f'SELECT 1 FROM {q_table} WHERE {where_clause}')
                        row = conn.execute(stmt, params).fetchone()
                        return row is not None
                    col_str = ", ".join([q(col) for col in witness_dict.keys()])
                    stmt = text(f'SELECT {col_str} FROM {q_table} WHERE {where_clause}')
                    row = conn.execute(stmt, params).fetchone()
                    if row is None:
                        return False
                    row_map = dict(row._mapping)
                    for col, expected in witness_dict.items():
                        actual = row_map.get(col)
                        if actual != expected:
                            matched = False
                            try:
                                if abs(float(actual) - float(expected)) <= 1e-6:
                                    matched = True
                            except (ValueError, TypeError):
                                pass
                            if not matched and str(actual) == str(expected):
                                matched = True
                            if not matched:
                                return False
                    return True

            return False
        except Exception as e:
            logger.error("Failed to verify ORM state: %s", e)
            return False

    # ---------------------------------------------------------------------- #
    # Built-in Handlers: Kubernetes
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _execute_k8s(op: Any, witness: Any) -> None:
        live_path = op.parameters.get("live_path")
        prior_snapshot = op.witness_data
        if not live_path:
            return
        if prior_snapshot is not None:
            os.makedirs(os.path.dirname(os.path.abspath(live_path)), exist_ok=True)
            with open(live_path, "w", encoding="utf-8") as f:
                f.write(prior_snapshot)
        elif os.path.exists(live_path):
            os.remove(live_path)

    @staticmethod
    def _verify_k8s(op: Any, witness: Any) -> bool:
        live_path = op.parameters.get("live_path")
        prior_snapshot = op.witness_data
        if not live_path:
            return False
        if prior_snapshot is None:
            return not os.path.exists(live_path)
        if not os.path.exists(live_path):
            return False
        with open(live_path, "r", encoding="utf-8") as f:
            return f.read() == prior_snapshot

    # ---------------------------------------------------------------------- #
    # Built-in Handlers: Redis
    # ---------------------------------------------------------------------- #

    @classmethod
    def _execute_redis(cls, op: Any, witness: Any) -> None:
        import base64
        import hashlib
        import time
        url = op.parameters.get("redis_url", "redis://localhost:6379/0")
        client = cls.get_or_create_redis(url)
        key = op.parameters.get("key")
        operation = (op.operation or op.parameters.get("operation", "SET")).upper()
        w = op.witness_data or op.parameters.get("witness", {})
        if hasattr(w, "data") and isinstance(w.data, dict):
            w = w.data
        elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
            w = witness.data

        if not key:
            return

        now_ms = int(time.time() * 1000)

        if operation == "SET":
            exists = w.get("exists", False)
            if not exists:
                client.delete(key)
            else:
                deadline_ms = w.get("deadline_ms", -1)
                if deadline_ms > 0 and now_ms >= deadline_ms:
                    # Expired: ensure absent, do not resurrect!
                    client.delete(key)
                    return

                old_dump = w.get("old_dump_b64")
                if old_dump:
                    raw_dump = base64.b64decode(old_dump)
                    if deadline_ms > 0:
                        client.restore(key, deadline_ms, raw_dump, replace=True, absttl=True)
                    else:
                        client.restore(key, 0, raw_dump, replace=True)
                else:
                    old_val = _decode_b64(w.get("old_value"))
                    if old_val is not None:
                        client.set(key, old_val)
                        if deadline_ms > 0:
                            client.pexpireat(key, deadline_ms)

        elif operation == "DEL":
            deadline_ms = w.get("deadline_ms", -1)
            if deadline_ms > 0 and now_ms >= deadline_ms:
                # Expired: ensure absent, do not resurrect!
                client.delete(key)
                return

            old_dump = w.get("old_dump_b64")
            if old_dump:
                raw_dump = base64.b64decode(old_dump)
                if deadline_ms > 0:
                    client.restore(key, deadline_ms, raw_dump, replace=True, absttl=True)
                else:
                    client.restore(key, 0, raw_dump, replace=True)

        elif operation in ("INCRBY", "DECRBY"):
            # Algebraic counter compensation
            if not client.exists(key):
                raise ValueError(f"CONFLICT_DETECTED: Target counter key '{key}' does not exist in Redis")
            try:
                curr_raw = client.get(key)
                curr_val = int(curr_raw)
            except (ValueError, TypeError):
                raise ValueError(f"CONFLICT_DETECTED: Target key '{key}' does not contain an integer counter")

            delta = w.get("delta", 1 if operation == "INCRBY" else -1)
            if operation == "INCRBY":
                client.decrby(key, abs(delta))
            else:
                client.incrby(key, abs(delta))

            new_val = int(client.get(key))
            exists = w.get("exists", False)
            if not exists and new_val == 0:
                client.delete(key)

            if isinstance(w, dict):
                w["expected_reverted_val"] = new_val

        elif operation == "HSET":
            if "field_witnesses_list" in w:
                fw_items = _decode_b64(w["field_witnesses_list"])
                field_witnesses = {item["key"]: item["value"] for item in fw_items}
            else:
                field_witnesses = _decode_b64(w.get("field_witnesses", {}))
            for f, old_val in field_witnesses.items():
                if old_val is None:
                    client.hdel(key, f)
                else:
                    client.hset(key, f, old_val)

        elif operation == "HDEL":
            if "field_witnesses_list" in w:
                fw_items = _decode_b64(w["field_witnesses_list"])
                field_witnesses = {item["key"]: item["value"] for item in fw_items}
            else:
                field_witnesses = _decode_b64(w.get("field_witnesses", {}))
            for f, old_val in field_witnesses.items():
                if old_val is not None:
                    client.hset(key, f, old_val)

        elif operation == "SADD":
            new_members = _decode_b64(w.get("new_members", []))
            if new_members:
                client.srem(key, *[_to_bytes(m) for m in new_members])

        elif operation == "SREM":
            were_present = _decode_b64(w.get("were_present", []))
            if were_present:
                client.sadd(key, *[_to_bytes(m) for m in were_present])

        elif operation in ("LPUSH", "RPUSH"):
            # Safe element-aware list inverse
            current_items = client.lrange(key, 0, -1)
            current_bytes = [i if isinstance(i, bytes) else _to_bytes(i) for i in current_items]
            pushed_values = _decode_b64(w.get("pushed_values", []))
            pre_occurrences = _decode_b64(w.get("pre_occurrences", {}))

            for v in pushed_values:
                vb = _to_bytes(v)
                cnt = current_bytes.count(vb)
                token = hashlib.sha256(vb).hexdigest()[:16]
                pre_occ = pre_occurrences.get(token, 0)
                if cnt > 1 or pre_occ > 0:
                    raise ValueError(
                        f"CONFLICT_DETECTED: Duplicate indistinguishable element '{v}' in list '{key}' "
                        f"(occurrences in list: {cnt}, pre-mutation occurrences: {pre_occ})"
                    )
                if cnt == 1:
                    res = client.eval(REMOVE_UNIQUE_LUA, 1, key, vb)
                    if res == -1:
                        raise ValueError(
                            f"CONFLICT_DETECTED: Duplicate indistinguishable element '{v}' in list '{key}'"
                        )

            post_len = client.llen(key)
            if isinstance(w, dict):
                w["expected_reverted_len"] = post_len
            if hasattr(op, "parameters") and isinstance(op.parameters, dict):
                if "witness" in op.parameters and isinstance(op.parameters["witness"], dict):
                    op.parameters["witness"]["expected_reverted_len"] = post_len

    @classmethod
    def _verify_redis(cls, op: Any, witness: Any) -> bool:
        import base64
        import hashlib
        import time
        url = op.parameters.get("redis_url", "redis://localhost:6379/0")
        try:
            client = cls.get_or_create_redis(url)
            key = op.parameters.get("key")
            operation = (op.operation or op.parameters.get("operation", "SET")).upper()
            w = op.witness_data or op.parameters.get("witness", {})
            if hasattr(w, "data") and isinstance(w.data, dict):
                w = w.data
            elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
                w = witness.data

            if not key:
                return False

            now_ms = int(time.time() * 1000)

            if operation == "SET":
                exists = w.get("exists", False)
                if not exists:
                    return not client.exists(key)

                deadline_ms = w.get("deadline_ms", -1)
                if deadline_ms > 0 and now_ms >= deadline_ms:
                    # Expired: key must not exist!
                    return not client.exists(key)

                if not client.exists(key):
                    return False

                if deadline_ms > 0:
                    # Not expired: pttl must be > 0
                    if client.pttl(key) <= 0:
                        return False

                old_val = _decode_b64(w.get("old_value"))
                if old_val is not None:
                    curr_raw = client.get(key)
                    if isinstance(old_val, bytes):
                        return curr_raw == old_val
                    curr_str = curr_raw.decode("utf-8", errors="replace") if isinstance(curr_raw, bytes) else str(curr_raw)
                    return curr_str == str(old_val)
                return True

            elif operation == "DEL":
                deadline_ms = w.get("deadline_ms", -1)
                if deadline_ms > 0 and now_ms >= deadline_ms:
                    # Expired: key must not exist
                    return not client.exists(key)
                return bool(client.exists(key))

            elif operation in ("INCRBY", "DECRBY"):
                exists = w.get("exists", False)
                expected_reverted_val = w.get("expected_reverted_val")

                if not client.exists(key):
                    if not exists and (expected_reverted_val == 0 or expected_reverted_val is None):
                        return True
                    return False

                curr_raw = client.get(key)
                try:
                    curr_val = int(curr_raw) if curr_raw is not None else None
                except (ValueError, TypeError):
                    return False

                if expected_reverted_val is not None:
                    return curr_val == expected_reverted_val
                if not exists and curr_val == 0:
                    return True
                return curr_val == w.get("prev_val")

            elif operation in ("HSET", "HDEL"):
                if "field_witnesses_list" in w:
                    fw_items = _decode_b64(w["field_witnesses_list"])
                    field_witnesses = {item["key"]: item["value"] for item in fw_items}
                else:
                    field_witnesses = _decode_b64(w.get("field_witnesses", {}))
                for f, old_val in field_witnesses.items():
                    curr_f_raw = client.hget(key, f)
                    if old_val is None:
                        if curr_f_raw is not None:
                            return False
                    else:
                        if isinstance(old_val, bytes):
                            if curr_f_raw != old_val:
                                return False
                        else:
                            curr_f_str = curr_f_raw.decode("utf-8", errors="replace") if isinstance(curr_f_raw, bytes) else curr_f_raw
                            if str(curr_f_str) != str(old_val):
                                return False
                return True

            elif operation == "SADD":
                new_members = _decode_b64(w.get("new_members", []))
                for m in new_members:
                    if client.sismember(key, _to_bytes(m)):
                        return False
                return True

            elif operation == "SREM":
                were_present = _decode_b64(w.get("were_present", []))
                for m in were_present:
                    if not client.sismember(key, _to_bytes(m)):
                        return False
                return True

            elif operation in ("LPUSH", "RPUSH"):
                pushed_values = _decode_b64(w.get("pushed_values", []))
                pre_occurrences = _decode_b64(w.get("pre_occurrences", {}))
                pre_len = w.get("pre_len", 0)
                expected_len = w.get("expected_reverted_len", pre_len)

                if not client.exists(key):
                    return expected_len == 0

                current_items = client.lrange(key, 0, -1)
                current_bytes = [i if isinstance(i, bytes) else _to_bytes(i) for i in current_items]

                if len(current_bytes) != expected_len:
                    return False

                for v in pushed_values:
                    vb = _to_bytes(v)
                    token = hashlib.sha256(vb).hexdigest()[:16]
                    expected_occ = pre_occurrences.get(token, 0)
                    if current_bytes.count(vb) != expected_occ:
                        return False
                return True

            return True
        except Exception as e:
            logger.error("Failed to verify Redis recovery state: %s", e)
            return False

