"""Explicit, strongly-typed semantic recovery operations for all harness surfaces."""

from __future__ import annotations
import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type
from evoundo.core.state import (
    FileDescriptor,
    HarnessState,
    ListenerDescriptor,
    MiddlewareDescriptor,
    ResourceDescriptor,
    ToolDescriptor,
)
from evoundo.effects.contracts import Effect, EffectCategory, EffectContract, EffectOpType
from evoundo.witness.stores import Witness


class RecoveryVerificationError(RuntimeError):
    """Raised when post-recovery verification fails to confirm external resource restoration."""
    pass


class BaseRecoveryOp:
    """Base class for all discrete semantic recovery operations."""
    
    def apply(self, state: HarnessState, witness: Witness) -> None:
        raise NotImplementedError

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        """Verify that the target resource was successfully restored to pre-mutation state."""
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": self.__class__.__name__}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BaseRecoveryOp:
        return cls()


@dataclass
class RestoreConfigOp(BaseRecoveryOp):
    """Restores a configuration key to its pre-mutation state as recorded in the witness."""
    key: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        existed = witness.get(f"config:{self.key}:existed", False)
        if existed:
            state.config[self.key] = copy.deepcopy(witness.get(f"config:{self.key}:value"))
        else:
            state.delete_config(self.key)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        existed = witness.get(f"config:{self.key}:existed", False)
        if existed:
            return state.config.get(self.key) == witness.get(f"config:{self.key}:value")
        return self.key not in state.config

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RestoreConfigOp", "key": self.key}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RestoreConfigOp:
        return cls(key=data["key"])


@dataclass
class RemoveConfigOp(BaseRecoveryOp):
    """Explicitly deletes a newly added configuration key."""
    key: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        state.delete_config(self.key)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        return self.key not in state.config

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RemoveConfigOp", "key": self.key}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RemoveConfigOp:
        return cls(key=data["key"])


@dataclass
class RestoreToolOp(BaseRecoveryOp):
    """Restores a tool to its pre-mutation descriptor or removes it if newly added."""
    tool_name: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        existed = witness.get(f"tools:{self.tool_name}:existed", False)
        if existed:
            tool_desc: ToolDescriptor = witness.get(f"tools:{self.tool_name}:descriptor")
            if tool_desc:
                state.register_tool(copy.deepcopy(tool_desc))
        else:
            state.remove_tool(self.tool_name)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        existed = witness.get(f"tools:{self.tool_name}:existed", False)
        if existed:
            return self.tool_name in state.tools
        return self.tool_name not in state.tools

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RestoreToolOp", "tool_name": self.tool_name}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RestoreToolOp:
        return cls(tool_name=data["tool_name"])


@dataclass
class RemoveToolOp(BaseRecoveryOp):
    """Explicitly removes a tool from the harness."""
    tool_name: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        state.remove_tool(self.tool_name)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        return self.tool_name not in state.tools

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RemoveToolOp", "tool_name": self.tool_name}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RemoveToolOp:
        return cls(tool_name=data["tool_name"])


@dataclass
class RestoreMiddlewareOp(BaseRecoveryOp):
    """Restores a middleware to its pre-mutation descriptor and position or removes it."""
    middleware_id: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        existed = witness.get(f"middleware:{self.middleware_id}:existed", False)
        if existed:
            m_desc: MiddlewareDescriptor = witness.get(f"middleware:{self.middleware_id}:descriptor")
            if m_desc:
                state.add_middleware(copy.deepcopy(m_desc))
        else:
            state.remove_middleware(self.middleware_id)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        existed = witness.get(f"middleware:{self.middleware_id}:existed", False)
        has_it = any(getattr(m, "id", None) == self.middleware_id or getattr(m, "middleware_id", None) == self.middleware_id for m in state.middleware)
        return has_it if existed else not has_it

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RestoreMiddlewareOp", "middleware_id": self.middleware_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RestoreMiddlewareOp:
        return cls(middleware_id=data["middleware_id"])


@dataclass
class RemoveMiddlewareOp(BaseRecoveryOp):
    """Explicitly removes a middleware by id."""
    middleware_id: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        state.remove_middleware(self.middleware_id)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        return not any(getattr(m, "id", None) == self.middleware_id or getattr(m, "middleware_id", None) == self.middleware_id for m in state.middleware)

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RemoveMiddlewareOp", "middleware_id": self.middleware_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RemoveMiddlewareOp:
        return cls(middleware_id=data["middleware_id"])


@dataclass
class RestoreListenerOp(BaseRecoveryOp):
    """Restores an event listener to its pre-mutation state."""
    event: str
    listener_id: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        existed = witness.get(f"listener:{self.event}:{self.listener_id}:existed", False)
        if existed:
            l_desc: ListenerDescriptor = witness.get(f"listener:{self.event}:{self.listener_id}:descriptor")
            if l_desc:
                state.add_listener(self.event, copy.deepcopy(l_desc))
        else:
            state.remove_listener(self.event, self.listener_id)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        existed = witness.get(f"listener:{self.event}:{self.listener_id}:existed", False)
        evt_listeners = getattr(state, "event_listeners", None)
        if evt_listeners is None:
            evt_listeners = getattr(state, "listeners", {})
        has_it = any(getattr(l, "id", None) == self.listener_id or getattr(l, "listener_id", None) == self.listener_id for l in evt_listeners.get(self.event, []))
        return has_it if existed else not has_it

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RestoreListenerOp", "event": self.event, "listener_id": self.listener_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RestoreListenerOp:
        return cls(event=data["event"], listener_id=data["listener_id"])


@dataclass
class RemoveListenerOp(BaseRecoveryOp):
    """Explicitly removes an event listener."""
    event: str
    listener_id: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        state.remove_listener(self.event, self.listener_id)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        evt_listeners = getattr(state, "event_listeners", None)
        if evt_listeners is None:
            evt_listeners = getattr(state, "listeners", {})
        return not any(getattr(l, "id", None) == self.listener_id or getattr(l, "listener_id", None) == self.listener_id for l in evt_listeners.get(self.event, []))

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RemoveListenerOp", "event": self.event, "listener_id": self.listener_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RemoveListenerOp:
        return cls(event=data["event"], listener_id=data["listener_id"])


@dataclass
class RestoreFileOp(BaseRecoveryOp):
    """Restores file content and existence to its pre-mutation state."""
    path: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        existed = witness.get(f"files:{self.path}:existed", False)
        if existed:
            content = witness.get(f"files:{self.path}:content", "")
            mode = witness.get(f"files:{self.path}:mode", "text")
            state.write_file(self.path, content, mode=mode)
        else:
            state.delete_file(self.path)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        existed = witness.get(f"files:{self.path}:existed", False)
        if existed:
            f_desc = state.files.get(self.path)
            return f_desc is not None and not f_desc.is_deleted and f_desc.content == witness.get(f"files:{self.path}:content", "")
        return state.read_file(self.path) is None

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RestoreFileOp", "path": self.path}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RestoreFileOp:
        return cls(path=data["path"])


@dataclass
class DeleteCreatedFileOp(BaseRecoveryOp):
    """Deletes a file created by a mutation."""
    path: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        state.delete_file(self.path)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        return state.read_file(self.path) is None

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "DeleteCreatedFileOp", "path": self.path}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DeleteCreatedFileOp:
        return cls(path=data["path"])


@dataclass
class RestoreResourceOp(BaseRecoveryOp):
    """Restores managed resource descriptor or closes/removes it if newly created."""
    resource_id: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        existed = witness.get(f"resources:{self.resource_id}:existed", False)
        if existed:
            r_desc: ResourceDescriptor = witness.get(f"resources:{self.resource_id}:descriptor")
            if r_desc:
                state.register_resource(copy.deepcopy(r_desc))
        else:
            state.close_resource(self.resource_id)
            if self.resource_id in state.resources:
                del state.resources[self.resource_id]

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        existed = witness.get(f"resources:{self.resource_id}:existed", False)
        if existed:
            return self.resource_id in state.resources
        return self.resource_id not in state.resources

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RestoreResourceOp", "resource_id": self.resource_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RestoreResourceOp:
        return cls(resource_id=data["resource_id"])


@dataclass
class CloseResourceOp(BaseRecoveryOp):
    """Explicitly closes a managed resource."""
    resource_id: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        state.close_resource(self.resource_id)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        return self.resource_id not in state.resources

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "CloseResourceOp", "resource_id": self.resource_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CloseResourceOp:
        return cls(resource_id=data["resource_id"])


@dataclass
class RestorePromptOp(BaseRecoveryOp):
    """Restores a prompt template to its pre-mutation state."""
    name: str

    def apply(self, state: HarnessState, witness: Witness) -> None:
        existed = witness.get(f"prompts:{self.name}:existed", False)
        if existed:
            val = witness.get(f"prompts:{self.name}:value", "")
            state.set_prompt(self.name, val)
        else:
            if self.name in state.prompts:
                del state.prompts[self.name]

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        existed = witness.get(f"prompts:{self.name}:existed", False)
        if existed:
            return state.prompts.get(self.name) == witness.get(f"prompts:{self.name}:value", "")
        return self.name not in state.prompts

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "RestorePromptOp", "name": self.name}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RestorePromptOp:
        return cls(name=data["name"])


@dataclass
class CustomRecoveryOp(BaseRecoveryOp):
    """Custom user-provided inverse recovery callable."""
    name: str
    inverse_fn: Optional[Any] = None
    verify_fn: Optional[Any] = None

    def apply(self, state: HarnessState, witness: Witness) -> None:
        if callable(self.inverse_fn):
            try:
                self.inverse_fn(state, witness)
            except TypeError:
                self.inverse_fn(witness, None)
        else:
            raise RecoveryVerificationError(
                f"Cannot execute CustomRecoveryOp '{self.name}': recovery function (inverse_fn) is unavailable. "
                f"Recovery cannot proceed."
            )

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        if callable(self.verify_fn):
            return bool(self.verify_fn(state, witness))
        if not callable(self.inverse_fn):
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {"op_type": "CustomRecoveryOp", "name": self.name}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CustomRecoveryOp:
        return cls(name=data.get("name", "CustomRecoveryOp"))


@dataclass(init=False)
class DriverRecoveryOp(CustomRecoveryOp):
    """Durable, declarative recovery operation bound to an external surface driver."""
    driver_type: str
    target: str
    operation: str
    parameters: Dict[str, Any]
    witness_data: Any
    inverse_fn: Optional[Any]
    name: str

    def __init__(
        self,
        driver_type: str = "",
        target: str = "",
        operation: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        witness_data: Any = None,
        inverse_fn: Optional[Any] = None,
        name: str = "DriverRecoveryOp",
        verify_fn: Optional[Any] = None,
    ) -> None:
        super().__init__(name=name, inverse_fn=inverse_fn, verify_fn=verify_fn)
        self.driver_type = driver_type
        self.target = target
        self.operation = operation
        self.parameters = parameters if parameters is not None else {}
        self.witness_data = witness_data
        self.inverse_fn = inverse_fn
        self.name = name

    def apply(self, state: HarnessState, witness: Witness) -> None:
        key = self.parameters.get("key", "")
        # Custom target like Redis hash field (e.g. redis://{key}/field/{field})
        if self.driver_type == "redis" and "/field/" in key:
            if callable(self.inverse_fn):
                try:
                    self.inverse_fn(state, witness)
                except TypeError:
                    self.inverse_fn(witness, None)
                return

            from evoundo.recovery.driver_registry import DriverRegistry, _decode_b64
            url = self.parameters.get("redis_url", "redis://localhost:6379/0")
            client = DriverRegistry.get_or_create_redis(url)
            hash_key, field_name = key.split("/field/", 1)
            old_val = None
            if isinstance(self.witness_data, dict):
                old_val = self.witness_data.get("old_value")
            elif hasattr(witness, "data") and isinstance(witness.data, dict):
                old_val = witness.data.get(self.target, witness.data.get(key))
            old_val = _decode_b64(old_val)
            if old_val is None:
                client.hdel(hash_key, field_name)
            else:
                client.hset(hash_key, field_name, old_val)
            return

        # For standard datastore drivers (redis, mysql, orm, postgres):
        # Route through DriverRegistry.execute_recovery to ensure verifier metadata
        # (expected_reverted_val, expected_reverted_len) is populated and driver monkeypatches work properly.
        if self.driver_type in ("redis", "mysql", "orm", "postgres"):
            if callable(self.inverse_fn):
                fn_name = getattr(self.inverse_fn, "__name__", "")
                if not (fn_name.startswith("_inv") or fn_name.startswith("_inverse_")):
                    try:
                        self.inverse_fn(state, witness)
                    except TypeError:
                        self.inverse_fn(witness, None)
            from evoundo.recovery.driver_registry import DriverRegistry
            DriverRegistry.execute_recovery(self, witness)
            return

        # Non-datastore driver types (json_config, audit_file, sqlite, k8s, etc.)
        if callable(self.inverse_fn):
            try:
                self.inverse_fn(state, witness)
                return
            except TypeError:
                self.inverse_fn(witness, None)
                return

        from evoundo.recovery.driver_registry import DriverRegistry
        DriverRegistry.execute_recovery(self, witness)

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        from evoundo.recovery.driver_registry import DriverRegistry, _decode_b64
        if DriverRegistry.verify_recovery(self, witness):
            return True

        # Field-level physical verification for Redis sub-path targets (e.g. redis://{key}/field/{field})
        key = self.parameters.get("key", "")
        if self.driver_type == "redis" and "/field/" in key:
            try:
                url = self.parameters.get("redis_url", "redis://localhost:6379/0")
                client = DriverRegistry.get_or_create_redis(url)
                hash_key, field_name = key.split("/field/", 1)
                curr = client.hget(hash_key, field_name)
                old_val = None
                if isinstance(self.witness_data, dict):
                    old_val = self.witness_data.get("old_value")
                elif hasattr(witness, "data") and isinstance(witness.data, dict):
                    old_val = witness.data.get(self.target, witness.data.get(key))
                old_val = _decode_b64(old_val)
                if old_val is None:
                    return curr is None
                if isinstance(old_val, bytes):
                    return curr == old_val
                curr_str = curr.decode("utf-8", errors="replace") if isinstance(curr, bytes) else str(curr)
                return curr_str == str(old_val)
            except Exception:
                pass

        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_type": "DriverRecoveryOp",
            "driver_type": self.driver_type,
            "target": self.target,
            "operation": self.operation,
            "parameters": self.parameters,
            "witness_data": self.witness_data,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DriverRecoveryOp:
        return cls(
            driver_type=data.get("driver_type", ""),
            target=data.get("target", ""),
            operation=data.get("operation", ""),
            parameters=data.get("parameters", {}),
            witness_data=data.get("witness_data"),
        )


def _get_op_target_address(op: Any) -> Any:
    """Extract fine-grained target/field address to group operations by distinct external resource."""
    if isinstance(op, DriverRecoveryOp):
        drv = op.driver_type
        params = op.parameters or {}
        if drv == "sqlite":
            db_path = params.get("db_path", "")
            table = params.get("table_name", "")
            row_inverses = params.get("row_inverses")
            if row_inverses:
                rids = tuple(sorted(r["rowid"] for r in row_inverses if isinstance(r, dict) and "rowid" in r))
                cols = tuple(sorted(c for r in row_inverses if isinstance(r, dict) for c in r.get("set_columns", [])))
                return ("sqlite", db_path, table, rids, cols)
            cols = tuple(sorted(params.get("set_columns", [])))
            where_p = tuple(params.get("where_params", []))
            return ("sqlite", db_path, table, where_p, cols)
        if drv == "redis":
            url = params.get("redis_url", "")
            target = op.target or ""
            return ("redis", url, target)
        if drv == "orm":
            db_url = params.get("db_url", "")
            table = params.get("table_name", "")
            pk = str(params.get("pk", ""))
            return ("orm", db_url, table, pk)
        return (drv, op.target or id(op))

    tgt = getattr(op, "target", None) or getattr(op, "name", None) or getattr(op, "key", None)
    if tgt:
        return (op.__class__.__name__, tgt)
    return (op.__class__.__name__, id(op))


@dataclass
class RecoveryProgram:
    """Ordered program of semantic recovery operations targeting only mutated elements."""
    operations: List[BaseRecoveryOp] = field(default_factory=list)

    def add_op(self, op: BaseRecoveryOp) -> None:
        self.operations.append(op)

    def execute(self, state: HarnessState, witness: Witness) -> HarnessState:
        """Apply all recovery operations in reverse order to invert the target mutation."""
        for op in reversed(self.operations):
            op.apply(state, witness)
        return state

    def verify(self, state: HarnessState, witness: Witness) -> bool:
        """Verify that all recovery operations successfully restored the target resources.

        When multiple operations affect the same target/entity/cells, the coherent final prestate
        of that resource (before this entire mutation occurred) is the prestate held by the earliest
        operation in forward chronological order for each distinct cell/field/column.
        """
        if not self.operations:
            if witness and hasattr(witness, "data") and witness.data:
                return False
            return True

        # 1. Group operations by driver/type
        sqlite_ops: List[DriverRecoveryOp] = []
        orm_ops: List[DriverRecoveryOp] = []
        other_ops: List[BaseRecoveryOp] = []

        for op in self.operations:
            if isinstance(op, DriverRecoveryOp):
                if op.driver_type == "sqlite":
                    sqlite_ops.append(op)
                elif op.driver_type == "orm":
                    orm_ops.append(op)
                else:
                    other_ops.append(op)
            else:
                other_ops.append(op)

        # 2. Verify SQLite operations by table with cell-level prestate merging
        if sqlite_ops:
            from evoundo.recovery.driver_registry import DriverRegistry
            sqlite_groups: Dict[Tuple[str, str], List[DriverRecoveryOp]] = {}
            for op in sqlite_ops:
                params = op.parameters or {}
                db_path = params.get("db_path", "")
                table = params.get("table_name", "")
                key = (db_path, table)
                sqlite_groups.setdefault(key, []).append(op)

            for (db_path, table), ops in sqlite_groups.items():
                earliest_cell_prevalues: Dict[int, Dict[str, Any]] = {}
                fallback_ops: List[DriverRecoveryOp] = []
                for op in ops:
                    params = op.parameters or {}
                    row_inverses = params.get("row_inverses")
                    if row_inverses:
                        for r in row_inverses:
                            rid = r["rowid"]
                            cols = r.get("set_columns", params.get("set_columns", []))
                            vals = r.get("pre_values", [])
                            if rid not in earliest_cell_prevalues:
                                earliest_cell_prevalues[rid] = {}
                            for col, val in zip(cols, vals):
                                if col not in earliest_cell_prevalues[rid]:
                                    earliest_cell_prevalues[rid][col] = val
                    else:
                        fallback_ops.append(op)

                if earliest_cell_prevalues:
                    combined_row_inverses = []
                    for rid in sorted(earliest_cell_prevalues.keys()):
                        col_dict = earliest_cell_prevalues[rid]
                        cols = sorted(col_dict.keys())
                        vals = [col_dict[c] for c in cols]
                        combined_row_inverses.append({
                            "rowid": rid,
                            "set_columns": cols,
                            "pre_values": vals,
                        })
                    combined_op = DriverRecoveryOp(
                        driver_type="sqlite",
                        target=f"sqlite://{table}",
                        operation="update",
                        parameters={
                            "db_path": db_path,
                            "table_name": table,
                            "row_inverses": combined_row_inverses,
                        },
                    )
                    if not DriverRegistry.verify_recovery(combined_op, witness):
                        return False

                for fb_op in fallback_ops:
                    if hasattr(fb_op, "verify") and not fb_op.verify(state, witness):
                        return False

        # 3. Verify ORM operations with row/column-level prestate merging
        if orm_ops:
            from evoundo.recovery.driver_registry import DriverRegistry
            orm_groups: Dict[Tuple[str, str, str], List[DriverRecoveryOp]] = {}
            for op in orm_ops:
                params = op.parameters or {}
                db_url = params.get("db_url", "")
                table = params.get("table_name", "")
                pk = str(params.get("pk", ""))
                key = (db_url, table, pk)
                orm_groups.setdefault(key, []).append(op)

            for (db_url, table, pk_str), ops in orm_groups.items():
                first_op = ops[0]
                first_params = first_op.parameters or {}
                op_name = first_op.operation or first_params.get("op", "UPDATE")
                pk = first_params.get("pk")
                pk_cols = first_params.get("pk_cols", ["id"])

                if op_name == "INSERT":
                    if not DriverRegistry.verify_recovery(first_op, witness):
                        return False
                elif op_name == "DELETE":
                    if not DriverRegistry.verify_recovery(first_op, witness):
                        return False
                else:
                    earliest_col_values: Dict[str, Any] = {}
                    for op in ops:
                        params = op.parameters or {}
                        w = op.witness_data or params.get("witness", {})
                        if isinstance(w, dict):
                            for col, val in w.items():
                                if col not in earliest_col_values:
                                    earliest_col_values[col] = val

                    combined_op = DriverRecoveryOp(
                        driver_type="orm",
                        target=f"orm://{table}/{pk_str}",
                        operation="UPDATE",
                        parameters={
                            "db_url": db_url,
                            "table_name": table,
                            "pk": pk,
                            "pk_cols": pk_cols,
                            "witness": earliest_col_values,
                        },
                        witness_data=earliest_col_values,
                    )
                    if not DriverRegistry.verify_recovery(combined_op, witness):
                        return False

        # 4. Verify other operations with earliest-op per target
        if other_ops:
            earliest_other_per_target = {}
            for op in other_ops:
                addr = _get_op_target_address(op)
                if addr not in earliest_other_per_target:
                    earliest_other_per_target[addr] = op

            for op in earliest_other_per_target.values():
                if hasattr(op, "verify"):
                    if not op.verify(state, witness):
                        return False

        return True

    def to_dict(self) -> Dict[str, Any]:
        return {"operations": [op.to_dict() for op in self.operations]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RecoveryProgram:
        program = cls()
        for op_dict in data.get("operations", []):
            op_type = op_dict.get("op_type")
            op_cls = OP_TYPE_MAP.get(op_type)
            if op_cls and hasattr(op_cls, "from_dict"):
                program.add_op(op_cls.from_dict(op_dict))
            elif op_cls:
                program.add_op(op_cls(**{k: v for k, v in op_dict.items() if k != "op_type"}))
        return program

    @classmethod
    def synthesize_from_contract(cls, contract: EffectContract) -> RecoveryProgram:
        """Automatically synthesize a minimal semantic recovery program from an EffectContract."""
        program = cls()

        # Config operations
        for key in sorted(contract.config):
            program.add_op(RestoreConfigOp(key=key))

        # Tools operations
        for tool_name in sorted(contract.tools):
            program.add_op(RestoreToolOp(tool_name=tool_name))

        # Middleware operations
        for mid in sorted(contract.middleware):
            program.add_op(RestoreMiddlewareOp(middleware_id=mid))

        # Listeners operations
        for target in sorted(contract.event_listeners):
            if ":" in target:
                evt, lid = target.split(":", 1)
                program.add_op(RestoreListenerOp(event=evt, listener_id=lid))

        # Files operations
        for path in sorted(contract.files):
            program.add_op(RestoreFileOp(path=path))

        # Resources operations
        for rid in sorted(contract.resources):
            program.add_op(RestoreResourceOp(resource_id=rid))

        # Prompts operations
        for p_name in sorted(contract.prompts):
            program.add_op(RestorePromptOp(name=p_name))

        return program


OP_TYPE_MAP: Dict[str, Type[BaseRecoveryOp]] = {
    "RestoreConfigOp": RestoreConfigOp,
    "RemoveConfigOp": RemoveConfigOp,
    "RestoreToolOp": RestoreToolOp,
    "RemoveToolOp": RemoveToolOp,
    "RestoreMiddlewareOp": RestoreMiddlewareOp,
    "RemoveMiddlewareOp": RemoveMiddlewareOp,
    "RestoreListenerOp": RestoreListenerOp,
    "RemoveListenerOp": RemoveListenerOp,
    "RestoreFileOp": RestoreFileOp,
    "DeleteCreatedFileOp": DeleteCreatedFileOp,
    "RestoreResourceOp": RestoreResourceOp,
    "CloseResourceOp": CloseResourceOp,
    "RestorePromptOp": RestorePromptOp,
    "CustomRecoveryOp": CustomRecoveryOp,
    "DriverRecoveryOp": DriverRecoveryOp,
}
