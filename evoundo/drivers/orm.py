"""SQLAlchemy / SQLModel ORM Surface Driver for EvoUndo 2.1.

Provides automatic unit-of-work lifecycle observation, pre-state witness capture,
fail-closed boundary enforcement, and zero-configuration inverse derivation for
SQLAlchemy and SQLModel operations.
"""

from __future__ import annotations
import inspect as py_inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Type, Union

from evoundo.context import get_current_context

try:
    from sqlalchemy import event, inspect, select
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker, Mapper
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False


logger = logging.getLogger("evoundo.drivers.orm")


class UnsupportedORMOperationError(ValueError):
    """Raised when an ORM operation falls outside the automatic recovery envelope and fails closed."""
    pass


class SQLAlchemyDriver:
    """Zero-configuration SQLAlchemy & SQLModel mutation observer and automatic recovery engine."""

    _is_instrumented: bool = False
    _instrumented_targets: Set[int] = set()

    @classmethod
    def instrument(cls, target: Optional[Any] = None) -> None:
        """Attach lifecycle event listeners to SQLAlchemy Session, sessionmaker, or Engine."""
        if not HAS_SQLALCHEMY:
            raise ImportError("SQLAlchemy is required to use SQLAlchemyDriver. Install with 'pip install sqlalchemy'.")

        # Instrument the global Session class once to capture all session instances
        if not cls._is_instrumented:
            event.listen(Session, "before_flush", cls._on_before_flush)
            event.listen(Session, "after_flush", cls._on_after_flush)
            event.listen(Session, "after_commit", cls._on_after_commit)
            event.listen(Session, "after_rollback", cls._on_after_rollback)
            event.listen(Session, "after_transaction_create", cls._on_transaction_create)
            event.listen(Session, "after_transaction_end", cls._on_transaction_end)
            event.listen(Session, "do_orm_execute", cls._on_do_orm_execute)
            cls._is_instrumented = True
            logger.debug("SQLAlchemyDriver globally instrumented on sqlalchemy.orm.Session.")

        # If a specific Engine or sessionmaker was provided, record it
        if target is not None:
            cls._instrumented_targets.add(id(target))

    @classmethod
    def _get_current_ops(cls, session: Session) -> List[Dict[str, Any]]:
        """Return the active operation list for the current transaction/savepoint depth."""
        stack = session.info.setdefault("_evoundo_ops_stack", [[]])
        if not stack:
            stack.append([])
        return stack[-1]

    @classmethod
    def _on_transaction_create(cls, session: Session, transaction: Any) -> None:
        """Push a new operation list on savepoint nesting."""
        if getattr(transaction, "nested", False):
            stack = session.info.setdefault("_evoundo_ops_stack", [[]])
            stack.append([])

    @classmethod
    def _on_transaction_end(cls, session: Session, transaction: Any) -> None:
        """Pop and promote savepoint operations to parent level upon savepoint completion."""
        if getattr(transaction, "nested", False):
            stack = session.info.setdefault("_evoundo_ops_stack", [[]])
            if len(stack) > 1:
                popped = stack.pop()
                stack[-1].extend(popped)

    @classmethod
    def _on_do_orm_execute(cls, orm_execute_state: Any) -> None:
        """Enforce fail-closed boundaries on bulk untracked ORM queries (UPDATE/DELETE)."""
        ctx = get_current_context()
        if not ctx or not ctx.active_mutation_id:
            return

        if orm_execute_state.is_delete or orm_execute_state.is_update:
            if ctx.active_recovery_level in ("invert", "dependency_aware"):
                raise UnsupportedORMOperationError(
                    "Bulk ORM operations (UPDATE/DELETE without individual entity tracking) "
                    "fall outside the automatic inversion envelope. Restrict the tool to R0 (Observe) "
                    "or R1 (Reconcile), or provide an explicit compensation contract."
                )

    # ---------------------------------------------------------------------- #
    # Event Handlers
    # ---------------------------------------------------------------------- #

    @classmethod
    def _on_before_flush(cls, session: Session, flush_context: Any, instances: Any) -> None:
        """Inspect dirty and deleted entities before changes are flushed to the database."""
        ctx = get_current_context()
        if not ctx or not ctx.active_mutation_id:
            return

        ops = cls._get_current_ops(session)
        bind_engine = session.bind or (session.get_bind() if hasattr(session, "get_bind") else None)

        # 1. Capture DELETED entities
        for obj in list(session.deleted):
            insp = inspect(obj)
            model_cls = obj.__class__
            model_name = model_cls.__name__
            pk = insp.identity[0] if (insp.identity and len(insp.identity) == 1) else (insp.identity or getattr(obj, "id", None))

            # Capture entire entity column dictionary before deletion
            entity_data = {}
            for c in insp.mapper.column_attrs:
                try:
                    entity_data[c.key] = getattr(obj, c.key)
                except Exception:
                    pass

            ops.append({
                "op": "DELETE",
                "model_cls": model_cls,
                "model_name": model_name,
                "pk": pk,
                "witness": entity_data,
                "engine": bind_engine,
            })

        # 2. Capture DIRTY (UPDATED) entities
        for obj in list(session.dirty):
            insp = inspect(obj)
            model_cls = obj.__class__
            model_name = model_cls.__name__
            pk = insp.identity[0] if (insp.identity and len(insp.identity) == 1) else (insp.identity or getattr(obj, "id", None))

            prev_values = {}
            for attr in insp.attrs:
                try:
                    hist = attr.history
                    if hist.has_changes():
                        if hist.deleted:
                            prev_values[attr.key] = hist.deleted[0]
                        else:
                            # Attribute was expired/unloaded; query database for committed value before flush
                            try:
                                col = insp.mapper.columns[attr.key]
                                pk_cols = list(insp.mapper.primary_key)
                                conn = session.connection()
                                if pk_cols and pk is not None:
                                    pk_col = pk_cols[0]
                                    stmt = select(col).where(pk_col == pk)
                                    prev_values[attr.key] = conn.execute(stmt).scalar()
                                else:
                                    prev_values[attr.key] = None
                            except Exception as e:
                                logger.debug("Failed to query expired attribute from DB: %s", e)
                                prev_values[attr.key] = None
                except Exception:
                    pass

            if prev_values:
                ops.append({
                    "op": "UPDATE",
                    "model_cls": model_cls,
                    "model_name": model_name,
                    "pk": pk,
                    "witness": prev_values,
                    "engine": bind_engine,
                })

    @classmethod
    def _on_after_flush(cls, session: Session, flush_context: Any) -> None:
        """Capture newly inserted entities after flush so auto-generated primary keys are populated."""
        ctx = get_current_context()
        if not ctx or not ctx.active_mutation_id:
            return

        ops = cls._get_current_ops(session)
        bind_engine = session.bind or (session.get_bind() if hasattr(session, "get_bind") else None)

        for obj in list(session.new):
            insp = inspect(obj)
            model_cls = obj.__class__
            model_name = model_cls.__name__
            pk = insp.identity[0] if (insp.identity and len(insp.identity) == 1) else (insp.identity or getattr(obj, "id", None))

            ops.append({
                "op": "INSERT",
                "model_cls": model_cls,
                "model_name": model_name,
                "pk": pk,
                "witness": {"pk": pk, "model": model_name},
                "engine": bind_engine,
            })

    @classmethod
    def _on_after_commit(cls, session: Session) -> None:
        """Finalize mutation tracking upon transaction commit and bind automatic inverse handlers."""
        ctx = get_current_context()
        if not ctx or not ctx.active_mutation_id:
            return

        stack = session.info.setdefault("_evoundo_ops_stack", [[]])
        ops = []
        while stack:
            ops.extend(stack.pop(0))
        session.info["_evoundo_ops_stack"] = [[]]
        if "_evoundo_ops" in session.info:
            ops.extend(session.info.pop("_evoundo_ops", []))
        if not ops:
            return

        from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType

        op_type_map = {
            "INSERT": EffectOpType.CREATE,
            "UPDATE": EffectOpType.UPDATE,
            "DELETE": EffectOpType.DELETE,
        }

        # Process captured operations in this commit
        for op in ops:
            model_name = op["model_name"]
            pk = op["pk"]
            target_uri = f"sqlalchemy://{model_name}/{pk}"

            # Set primary target address to the first entity modified
            if not ctx.active_target or not ctx.active_target.startswith("sqlalchemy://"):
                ctx.active_target = target_uri

            # Bind witness for this target address
            ctx.active_witness[target_uri] = op["witness"]

            # Declare effect on active context
            eff_op = op_type_map.get(op["op"], EffectOpType.UPDATE)
            eff = Effect(category=EffectCategory.RESOURCES, target=target_uri, op_type=eff_op)
            if not any(e.target == target_uri and e.category == EffectCategory.RESOURCES for e in ctx.active_effects):
                ctx.active_effects.append(eff)

            # Synthesize automatic inverse function for this operation
            bind_engine = op["engine"]
            model_cls = op["model_cls"]

            if op["op"] == "UPDATE":
                inv_fn = cls._create_update_inverse(model_cls, pk, op["witness"], bind_engine)
                ctx.active_inverses.append(inv_fn)
            elif op["op"] == "INSERT":
                inv_fn = cls._create_insert_inverse(model_cls, pk, bind_engine)
                ctx.active_inverses.append(inv_fn)
            elif op["op"] == "DELETE":
                inv_fn = cls._create_delete_inverse(model_cls, pk, op["witness"], bind_engine)
                ctx.active_inverses.append(inv_fn)

        # Build composite inverse executing all accumulated inverses in reverse chronological order
        inverses_snapshot = list(ctx.active_inverses)

        def _composite_inverse(witness_dict: Any, result: Any) -> None:
            for single_inv in reversed(inverses_snapshot):
                single_inv(witness_dict, result)

        ctx.active_inverse_fn = _composite_inverse

        # Construct declarative recovery operations for durable cross-process persistence
        from evoundo.recovery.operations import DriverRecoveryOp
        from evoundo.recovery.driver_registry import DriverRegistry

        recovery_ops_for_commit = []
        for op in ops:
            model_name = op["model_name"]
            pk = op["pk"]
            target_uri = f"sqlalchemy://{model_name}/{pk}"
            bind_engine = op["engine"]
            model_cls = op["model_cls"]

            db_url = None
            if bind_engine is not None:
                try:
                    db_url = bind_engine.url.render_as_string(hide_password=False)
                except Exception:
                    db_url = str(bind_engine.url)
                DriverRegistry.register_engine(db_url, bind_engine)

            try:
                insp = inspect(model_cls)
                table_name = getattr(insp, "local_table", None)
                if table_name is not None:
                    table_name = table_name.name
                else:
                    table_name = insp.tables[0].name if insp.tables else model_name
                pk_cols = [c.name for c in insp.primary_key] if insp.primary_key else ["id"]
            except Exception:
                table_name = model_name
                pk_cols = ["id"]

            if op["op"] == "UPDATE":
                inv_fn = cls._create_update_inverse(model_cls, pk, op["witness"], bind_engine)
            elif op["op"] == "INSERT":
                inv_fn = cls._create_insert_inverse(model_cls, pk, bind_engine)
            elif op["op"] == "DELETE":
                inv_fn = cls._create_delete_inverse(model_cls, pk, op["witness"], bind_engine)
            else:
                inv_fn = None

            d_op = DriverRecoveryOp(
                driver_type="orm",
                target=target_uri,
                operation=op["op"],
                parameters={
                    "db_url": db_url,
                    "table_name": table_name,
                    "model_name": model_name,
                    "pk": pk,
                    "pk_cols": pk_cols,
                    "witness": op["witness"],
                },
                witness_data=op["witness"],
                inverse_fn=inv_fn,
            )
            recovery_ops_for_commit.append(d_op)

        ctx.active_recovery_ops = ctx.active_recovery_ops + recovery_ops_for_commit

    @classmethod
    def _on_after_rollback(cls, session: Session) -> None:
        """Clear tracked operations if transaction rolled back."""
        stack = session.info.setdefault("_evoundo_ops_stack", [[]])
        if session.in_nested_transaction():
            if stack:
                stack[-1] = []
        else:
            session.info["_evoundo_ops_stack"] = [[]]
            session.info.pop("_evoundo_ops", None)

    # ---------------------------------------------------------------------- #
    # Inverse Inversion Generators
    # ---------------------------------------------------------------------- #

    @classmethod
    def _create_update_inverse(
        cls, model_cls: Type[Any], pk: Any, prev_values: Dict[str, Any], bind_engine: Any
    ) -> Callable[..., Any]:
        """Synthesize automatic inverse for single-entity UPDATE."""
        def _inverse_update(witness: Any, result: Any) -> None:
            target_uri = f"sqlalchemy://{model_cls.__name__}/{pk}"
            values_to_restore = prev_values
            if isinstance(witness, dict):
                if target_uri in witness and isinstance(witness[target_uri], dict):
                    values_to_restore = witness[target_uri]
                elif all(k in witness for k in prev_values.keys()) and "pk" not in witness:
                    values_to_restore = witness
            with Session(bind_engine) as inv_session:
                entity = inv_session.get(model_cls, pk)
                if entity:
                    for k, v in values_to_restore.items():
                        setattr(entity, k, v)
                    inv_session.add(entity)
                    inv_session.commit()
        return _inverse_update

    @classmethod
    def _create_insert_inverse(
        cls, model_cls: Type[Any], pk: Any, bind_engine: Any
    ) -> Callable[..., Any]:
        """Synthesize automatic inverse for INSERT (delete the created entity)."""
        def _inverse_insert(witness: Any, result: Any) -> None:
            target_uri = f"sqlalchemy://{model_cls.__name__}/{pk}"
            resolved_pk = pk
            if isinstance(witness, dict):
                if target_uri in witness and isinstance(witness[target_uri], dict):
                    resolved_pk = witness[target_uri].get("pk", pk)
                elif "pk" in witness:
                    resolved_pk = witness.get("pk", pk)
            with Session(bind_engine) as inv_session:
                entity = inv_session.get(model_cls, resolved_pk)
                if entity:
                    inv_session.delete(entity)
                    inv_session.commit()
        return _inverse_insert

    @classmethod
    def _create_delete_inverse(
        cls, model_cls: Type[Any], pk: Any, entity_data: Dict[str, Any], bind_engine: Any
    ) -> Callable[..., Any]:
        """Synthesize automatic inverse for DELETE (recreate entity from pre-state witness)."""
        def _inverse_delete(witness: Any, result: Any) -> None:
            target_uri = f"sqlalchemy://{model_cls.__name__}/{pk}"
            data = entity_data
            if isinstance(witness, dict):
                if target_uri in witness and isinstance(witness[target_uri], dict):
                    data = witness[target_uri]
                elif all(k in witness for k in entity_data.keys()):
                    data = witness
            with Session(bind_engine) as inv_session:
                recreated = model_cls(**data)
                inv_session.add(recreated)
                inv_session.commit()
        return _inverse_delete
