"""Redis Surface Driver for EvoUndo.

Provides automated pre-state capture, TTL preservation, and declarative inverse
generation across Strings, Hashes, Sets, Lists, and atomic Counters.
"""

from __future__ import annotations
import base64
import hashlib
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Union

from evoundo.context import get_current_context

try:
    import redis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

logger = logging.getLogger("evoundo.drivers.redis")


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


class RedisDriver:
    """Manages Redis mutation interception, pre-state capture, and inverse generation."""

    def __init__(self, client_or_url: Union[str, Any]):
        if not HAS_REDIS:
            raise ImportError("redis-py is required to use RedisDriver. Install with 'pip install redis'.")

        if isinstance(client_or_url, str):
            self.client = redis.Redis.from_url(client_or_url, decode_responses=False)
            self.redis_url = client_or_url
        else:
            self.client = client_or_url
            self.redis_url = self._extract_url(self.client)

    @staticmethod
    def _extract_url(client: Any) -> str:
        """Extract a canonical connection URL from a redis.Redis client."""
        try:
            kwargs = getattr(client.connection_pool, "connection_kwargs", {})
            host = kwargs.get("host", "localhost")
            port = kwargs.get("port", 6379)
            db = kwargs.get("db", 0)
            password = kwargs.get("password")
            if password:
                return f"redis://:{password}@{host}:{port}/{db}"
            return f"redis://{host}:{port}/{db}"
        except Exception:
            return "redis://localhost:6379/0"

    @classmethod
    def wrap(cls, client_or_url: Union[str, Any]) -> RedisDriver:
        """Wrap an existing Redis client in an EvoUndo mutation observer."""
        return cls(client_or_url)

    def __enter__(self) -> RedisDriver:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def __getattr__(self, name: str) -> Any:
        """Delegate unintercepted commands directly to the underlying Redis client."""
        return getattr(self.client, name)

    # ---------------------------------------------------------------------- #
    # Helper to register operations with active EvoUndo context
    # ---------------------------------------------------------------------- #

    def _record_op(
        self,
        target_key: Union[str, Sequence[str]],
        operation: str,
        parameters: Dict[str, Any],
        witness_data: Any,
        inverse_fn: Any,
    ) -> None:
        ctx = get_current_context()
        if not ctx or not ctx.active_mutation_id:
            return

        target_keys = [target_key] if isinstance(target_key, str) else list(target_key)
        primary_target_uri = f"redis://{target_keys[0]}"
        if not ctx.active_target or not ctx.active_target.startswith("redis://"):
            ctx.active_target = primary_target_uri

        from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
        from evoundo.recovery.operations import DriverRecoveryOp

        for tk in target_keys:
            t_uri = f"redis://{tk}"
            eff = Effect(category=EffectCategory.RESOURCES, target=t_uri, op_type=EffectOpType.UPDATE)
            if not any(e.target == t_uri and e.category == EffectCategory.RESOURCES for e in ctx.active_effects):
                ctx.active_effects.append(eff)
            if t_uri not in ctx.active_witness:
                ctx.active_witness[t_uri] = witness_data

        ctx.active_inverses.append(inverse_fn)

        op = DriverRecoveryOp(
            driver_type="redis",
            target=primary_target_uri,
            operation=operation,
            parameters={**parameters, "redis_url": self.redis_url, "target_uris": [f"redis://{tk}" for tk in target_keys]},
            witness_data=witness_data,
            inverse_fn=inverse_fn,
        )
        ctx.active_recovery_ops.append(op)

    # ---------------------------------------------------------------------- #
    # Intercepted Mutating Operations
    # ---------------------------------------------------------------------- #

    def set(
        self,
        name: str,
        value: Any,
        ex: Optional[int] = None,
        px: Optional[int] = None,
        nx: bool = False,
        xx: bool = False,
        keepttl: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Set key with pre-state witness, absolute deadline TTL, and automatic inverse generation."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            exists = bool(self.client.exists(name))
            deadline_ms = -1
            old_value = None
            old_dump_b64 = None

            if exists:
                try:
                    exp = self.client.pexpiretime(name)
                    if exp > 0:
                        deadline_ms = exp
                    else:
                        pttl = self.client.pttl(name)
                        if pttl > 0:
                            deadline_ms = int(time.time() * 1000) + pttl
                except Exception:
                    pttl = self.client.pttl(name)
                    if pttl > 0:
                        deadline_ms = int(time.time() * 1000) + pttl

                try:
                    raw_val = self.client.get(name)
                    # Support raw bytes without UTF-8 decode assumptions
                    old_value = raw_val
                except Exception:
                    dump_bytes = self.client.dump(name)
                    if dump_bytes:
                        old_dump_b64 = base64.b64encode(dump_bytes).decode("ascii")

            witness = {
                "exists": exists,
                "deadline_ms": deadline_ms,
                "ttl_ms": deadline_ms - int(time.time() * 1000) if deadline_ms > 0 else -1,
                "old_value": old_value,
                "old_dump_b64": old_dump_b64,
            }

            client = self.client

            def _inv(w: Any, r: Any) -> None:
                if not exists:
                    client.delete(name)
                else:
                    now_ms = int(time.time() * 1000)
                    if deadline_ms > 0 and now_ms >= deadline_ms:
                        # Expired: ensure absent, do not resurrect!
                        client.delete(name)
                    elif old_dump_b64:
                        raw_dump = base64.b64decode(old_dump_b64)
                        if deadline_ms > 0:
                            client.restore(name, deadline_ms, raw_dump, replace=True, absttl=True)
                        else:
                            client.restore(name, 0, raw_dump, replace=True)
                    else:
                        client.set(name, old_value)
                        if deadline_ms > 0:
                            client.pexpireat(name, deadline_ms)

            self._record_op(
                target_key=name,
                operation="SET",
                parameters={"key": name, "witness": witness},
                witness_data=witness,
                inverse_fn=_inv,
            )

        return self.client.set(name, value, ex=ex, px=px, nx=nx, xx=xx, keepttl=keepttl, **kwargs)

    def setex(self, name: str, time: int, value: Any) -> Any:
        """Set key with expiration in seconds."""
        return self.set(name, value, ex=time)

    def delete(self, *names: str) -> int:
        """Delete keys with full pre-state preservation via DUMP and absolute deadline."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            for name in names:
                exists = bool(self.client.exists(name))
                if not exists:
                    continue
                deadline_ms = -1
                try:
                    exp = self.client.pexpiretime(name)
                    if exp > 0:
                        deadline_ms = exp
                    else:
                        pttl = self.client.pttl(name)
                        if pttl > 0:
                            deadline_ms = int(time.time() * 1000) + pttl
                except Exception:
                    pttl = self.client.pttl(name)
                    if pttl > 0:
                        deadline_ms = int(time.time() * 1000) + pttl

                dump_bytes = self.client.dump(name)
                old_dump_b64 = base64.b64encode(dump_bytes).decode("ascii") if dump_bytes else None

                witness = {
                    "exists": True,
                    "deadline_ms": deadline_ms,
                    "ttl_ms": deadline_ms - int(time.time() * 1000) if deadline_ms > 0 else -1,
                    "old_dump_b64": old_dump_b64,
                }

                client = self.client

                def _inv(w: Any, r: Any, key_name: str = name, d_b64: str = old_dump_b64, d_ms: int = deadline_ms) -> None:
                    if d_b64:
                        now_ms = int(time.time() * 1000)
                        if d_ms > 0 and now_ms >= d_ms:
                            client.delete(key_name)
                        else:
                            raw_dump = base64.b64decode(d_b64)
                            if d_ms > 0:
                                client.restore(key_name, d_ms, raw_dump, replace=True, absttl=True)
                            else:
                                client.restore(key_name, 0, raw_dump, replace=True)

                self._record_op(
                    target_key=name,
                    operation="DEL",
                    parameters={"key": name, "witness": witness},
                    witness_data=witness,
                    inverse_fn=_inv,
                )

        return self.client.delete(*names)

    def incrby(self, name: str, amount: int = 1) -> int:
        """Increment integer key with algebraic counter-decrement inverse."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            exists = bool(self.client.exists(name))
            raw_prev = self.client.get(name) if exists else None
            prev_val = int(raw_prev) if raw_prev is not None else 0
            ttl_ms = self.client.pttl(name)

            witness = {
                "exists": exists,
                "prev_val": prev_val,
                "delta": amount,
                "ttl_ms": ttl_ms,
                "commutative": True,
                "key": name,
            }

            client = self.client

            def _inv(w: Any, r: Any) -> None:
                if not client.exists(name):
                    raise ValueError(f"CONFLICT_DETECTED: Target counter key '{name}' does not exist in Redis")
                try:
                    curr_raw = client.get(name)
                    curr_val = int(curr_raw)
                except (ValueError, TypeError):
                    raise ValueError(f"CONFLICT_DETECTED: Target key '{name}' does not contain an integer counter")

                client.decrby(name, amount)
                new_val = int(client.get(name))
                if not exists and new_val == 0:
                    client.delete(name)
                if isinstance(w, dict):
                    w["expected_reverted_val"] = new_val
                elif hasattr(w, "data") and isinstance(w.data, dict):
                    w.data["expected_reverted_val"] = new_val

            self._record_op(
                target_key=f"{name}#counter",
                operation="INCRBY",
                parameters={"key": name, "witness": witness},
                witness_data=witness,
                inverse_fn=_inv,
            )

        return self.client.incrby(name, amount)

    def decrby(self, name: str, amount: int = 1) -> int:
        """Decrement integer key with algebraic counter-increment inverse."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            exists = bool(self.client.exists(name))
            raw_prev = self.client.get(name) if exists else None
            prev_val = int(raw_prev) if raw_prev is not None else 0
            ttl_ms = self.client.pttl(name)

            witness = {
                "exists": exists,
                "prev_val": prev_val,
                "delta": -amount,
                "ttl_ms": ttl_ms,
                "commutative": True,
                "key": name,
            }

            client = self.client

            def _inv(w: Any, r: Any) -> None:
                if not client.exists(name):
                    raise ValueError(f"CONFLICT_DETECTED: Target counter key '{name}' does not exist in Redis")
                try:
                    curr_raw = client.get(name)
                    curr_val = int(curr_raw)
                except (ValueError, TypeError):
                    raise ValueError(f"CONFLICT_DETECTED: Target key '{name}' does not contain an integer counter")

                client.incrby(name, amount)
                new_val = int(client.get(name))
                if not exists and new_val == 0:
                    client.delete(name)
                if isinstance(w, dict):
                    w["expected_reverted_val"] = new_val
                elif hasattr(w, "data") and isinstance(w.data, dict):
                    w.data["expected_reverted_val"] = new_val

            self._record_op(
                target_key=f"{name}#counter",
                operation="DECRBY",
                parameters={"key": name, "witness": witness},
                witness_data=witness,
                inverse_fn=_inv,
            )

        return self.client.decrby(name, amount)

    def hset(
        self,
        name: str,
        key: Optional[Any] = None,
        value: Optional[Any] = None,
        mapping: Optional[Dict[Any, Any]] = None,
        items: Optional[List[Any]] = None,
    ) -> int:
        """Set hash fields with field-level inverse generation."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            fields_to_set: Dict[Any, Any] = {}
            if key is not None:
                fields_to_set[key] = value
            if mapping:
                for k, v in mapping.items():
                    fields_to_set[k] = v
            if items:
                for i in range(0, len(items), 2):
                    fields_to_set[items[i]] = items[i + 1]

            field_witnesses: Dict[Any, Optional[Any]] = {}
            for f in fields_to_set.keys():
                old_val = self.client.hget(name, f)
                # Keep raw bytes or None without decode error
                field_witnesses[f] = old_val

            hash_existed = bool(self.client.exists(name))
            witness = {
                "hash_existed": hash_existed,
                "field_witnesses": field_witnesses,
                "field_witnesses_list": [
                    {"key": k, "value": v} for k, v in field_witnesses.items()
                ],
            }

            client = self.client

            def _inv(w: Any, r: Any) -> None:
                w_data = w if isinstance(w, dict) else getattr(w, "data", {})
                fw = {}
                if isinstance(w_data, dict) and "field_witnesses_list" in w_data:
                    from evoundo.recovery.driver_registry import _decode_b64
                    for item in _decode_b64(w_data["field_witnesses_list"]):
                        fw[item["key"]] = item["value"]
                elif isinstance(w_data, dict) and "field_witnesses" in w_data:
                    from evoundo.recovery.driver_registry import _decode_b64
                    fw = _decode_b64(w_data["field_witnesses"])
                else:
                    fw = field_witnesses

                for f, old_f_val in fw.items():
                    if old_f_val is None:
                        client.hdel(name, f)
                    else:
                        client.hset(name, f, old_f_val)

            target_keys = []
            for f in fields_to_set.keys():
                if isinstance(f, bytes):
                    f_repr = f"b64:{base64.b64encode(f).decode('ascii')}"
                else:
                    f_repr = str(f)
                target_keys.append(f"{name}/field/{f_repr}")
            if not target_keys:
                target_keys = [f"{name}/fields"]

            self._record_op(
                target_key=target_keys,
                operation="HSET",
                parameters={"key": name, "witness": witness, "field_keys": list(fields_to_set.keys())},
                witness_data=witness,
                inverse_fn=_inv,
            )

        return self.client.hset(name, key=key, value=value, mapping=mapping, items=items)

    def hdel(self, name: str, *keys: Any) -> int:
        """Delete hash fields with restoration inverse."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            field_witnesses: Dict[Any, Optional[Any]] = {}
            for f in keys:
                old_val = self.client.hget(name, f)
                if old_val is not None:
                    field_witnesses[f] = old_val

            witness = {
                "field_witnesses": field_witnesses,
                "field_witnesses_list": [
                    {"key": k, "value": v} for k, v in field_witnesses.items()
                ],
            }
            client = self.client

            def _inv(w: Any, r: Any) -> None:
                w_data = w if isinstance(w, dict) else getattr(w, "data", {})
                fw = {}
                if isinstance(w_data, dict) and "field_witnesses_list" in w_data:
                    from evoundo.recovery.driver_registry import _decode_b64
                    for item in _decode_b64(w_data["field_witnesses_list"]):
                        fw[item["key"]] = item["value"]
                elif isinstance(w_data, dict) and "field_witnesses" in w_data:
                    from evoundo.recovery.driver_registry import _decode_b64
                    fw = _decode_b64(w_data["field_witnesses"])
                else:
                    fw = field_witnesses

                for f, old_f_val in fw.items():
                    client.hset(name, f, old_f_val)

            target_keys = []
            for f in keys:
                if isinstance(f, bytes):
                    f_repr = f"b64:{base64.b64encode(f).decode('ascii')}"
                else:
                    f_repr = str(f)
                target_keys.append(f"{name}/field/{f_repr}")
            if not target_keys:
                target_keys = [f"{name}/fields"]

            self._record_op(
                target_key=target_keys,
                operation="HDEL",
                parameters={"key": name, "witness": witness, "field_keys": list(keys)},
                witness_data=witness,
                inverse_fn=_inv,
            )

        return self.client.hdel(name, *keys)

    def sadd(self, name: str, *values: Any) -> int:
        """Add set members with SREM inverse."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            members_already_present = []
            new_members = []
            for v in values:
                vb = _to_bytes(v)
                if self.client.sismember(name, vb):
                    members_already_present.append(v)
                else:
                    new_members.append(v)

            witness = {
                "already_present": members_already_present,
                "new_members": new_members,
            }

            client = self.client

            def _inv(w: Any, r: Any) -> None:
                if new_members:
                    client.srem(name, *[_to_bytes(m) for m in new_members])

            target_keys = [f"{name}/member/{hashlib.sha256(_to_bytes(v)).hexdigest()[:16]}" for v in values] or [f"{name}/members"]
            self._record_op(
                target_key=target_keys,
                operation="SADD",
                parameters={"key": name, "witness": witness},
                witness_data=witness,
                inverse_fn=_inv,
            )

        return self.client.sadd(name, *values)

    def srem(self, name: str, *values: Any) -> int:
        """Remove set members with SADD inverse."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            members_that_were_present = []
            for v in values:
                vb = _to_bytes(v)
                if self.client.sismember(name, vb):
                    members_that_were_present.append(v)

            witness = {"were_present": members_that_were_present}
            client = self.client

            def _inv(w: Any, r: Any) -> None:
                if members_that_were_present:
                    client.sadd(name, *[_to_bytes(m) for m in members_that_were_present])

            target_keys = [f"{name}/member/{hashlib.sha256(_to_bytes(v)).hexdigest()[:16]}" for v in values] or [f"{name}/members"]
            self._record_op(
                target_key=target_keys,
                operation="SREM",
                parameters={"key": name, "witness": witness},
                witness_data=witness,
                inverse_fn=_inv,
            )

        return self.client.srem(name, *values)

    def lpush(self, name: str, *values: Any) -> int:
        """Push values to head of list with element-aware inverse."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            exists = bool(self.client.exists(name))
            pre_len = self.client.llen(name) if exists else 0
            pre_items = self.client.lrange(name, 0, -1) if exists else []
            pre_items_bytes = [i if isinstance(i, bytes) else _to_bytes(i) for i in pre_items]

            pushed_values = list(values)
            pre_occurrences = {}
            for v in pushed_values:
                vb = _to_bytes(v)
                token = hashlib.sha256(vb).hexdigest()[:16]
                pre_occurrences[token] = pre_items_bytes.count(vb)

            witness = {
                "exists": exists,
                "pre_len": pre_len,
                "pushed_values": pushed_values,
                "pre_occurrences": pre_occurrences,
                "count": len(values),
                "direction": "left",
            }

            client = self.client

            def _inv(w: Any, r: Any) -> None:
                current_items = client.lrange(name, 0, -1)
                current_bytes = [i if isinstance(i, bytes) else _to_bytes(i) for i in current_items]

                for v in pushed_values:
                    vb = _to_bytes(v)
                    cnt = current_bytes.count(vb)
                    token = hashlib.sha256(vb).hexdigest()[:16]
                    pre_occ = pre_occurrences.get(token, 0)
                    if cnt > 1 or pre_occ > 0:
                        raise ValueError(
                            f"CONFLICT_DETECTED: Duplicate indistinguishable element '{v}' in list '{name}' "
                            f"(occurrences in list: {cnt}, pre-mutation occurrences: {pre_occ})"
                        )
                    if cnt == 1:
                        res = client.eval(REMOVE_UNIQUE_LUA, 1, name, vb)
                        if res == -1:
                            raise ValueError(
                                f"CONFLICT_DETECTED: Duplicate indistinguishable element '{v}' in list '{name}'"
                            )

                post_len = client.llen(name)
                if isinstance(w, dict):
                    w["expected_reverted_len"] = post_len
                elif hasattr(w, "data") and isinstance(w.data, dict):
                    w.data["expected_reverted_len"] = post_len

            target_keys = [f"{name}/item/{hashlib.sha256(_to_bytes(v)).hexdigest()[:16]}" for v in pushed_values] or [f"{name}/items"]
            self._record_op(
                target_key=target_keys,
                operation="LPUSH",
                parameters={"key": name, "witness": witness},
                witness_data=witness,
                inverse_fn=_inv,
            )

        return self.client.lpush(name, *values)

    def rpush(self, name: str, *values: Any) -> int:
        """Push values to tail of list with element-aware inverse."""
        ctx = get_current_context()
        if ctx and ctx.active_mutation_id:
            exists = bool(self.client.exists(name))
            pre_len = self.client.llen(name) if exists else 0
            pre_items = self.client.lrange(name, 0, -1) if exists else []
            pre_items_bytes = [i if isinstance(i, bytes) else _to_bytes(i) for i in pre_items]

            pushed_values = list(values)
            pre_occurrences = {}
            for v in pushed_values:
                vb = _to_bytes(v)
                token = hashlib.sha256(vb).hexdigest()[:16]
                pre_occurrences[token] = pre_items_bytes.count(vb)

            witness = {
                "exists": exists,
                "pre_len": pre_len,
                "pushed_values": pushed_values,
                "pre_occurrences": pre_occurrences,
                "count": len(values),
                "direction": "right",
            }

            client = self.client

            def _inv(w: Any, r: Any) -> None:
                current_items = client.lrange(name, 0, -1)
                current_bytes = [i if isinstance(i, bytes) else _to_bytes(i) for i in current_items]

                for v in pushed_values:
                    vb = _to_bytes(v)
                    cnt = current_bytes.count(vb)
                    token = hashlib.sha256(vb).hexdigest()[:16]
                    pre_occ = pre_occurrences.get(token, 0)
                    if cnt > 1 or pre_occ > 0:
                        raise ValueError(
                            f"CONFLICT_DETECTED: Duplicate indistinguishable element '{v}' in list '{name}' "
                            f"(occurrences in list: {cnt}, pre-mutation occurrences: {pre_occ})"
                        )
                    if cnt == 1:
                        res = client.eval(REMOVE_UNIQUE_LUA, 1, name, vb)
                        if res == -1:
                            raise ValueError(
                                f"CONFLICT_DETECTED: Duplicate indistinguishable element '{v}' in list '{name}'"
                            )

                post_len = client.llen(name)
                if isinstance(w, dict):
                    w["expected_reverted_len"] = post_len
                elif hasattr(w, "data") and isinstance(w.data, dict):
                    w.data["expected_reverted_len"] = post_len

            target_keys = [f"{name}/item/{hashlib.sha256(_to_bytes(v)).hexdigest()[:16]}" for v in pushed_values] or [f"{name}/items"]
            self._record_op(
                target_key=target_keys,
                operation="RPUSH",
                parameters={"key": name, "witness": witness},
                witness_data=witness,
                inverse_fn=_inv,
            )

        return self.client.rpush(name, *values)


def redis_client(client_or_url: Union[str, Any]) -> RedisDriver:
    """Convenience context manager constructor for Redis."""
    return RedisDriver(client_or_url)
