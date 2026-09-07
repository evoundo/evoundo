"""Recovery engine coordinating semantic, scoped, and full recovery."""

from __future__ import annotations
from typing import Optional
from evoundo.core.state import HarnessState
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger
from evoundo.recovery.operations import RecoveryProgram, RecoveryVerificationError
from evoundo.recovery.snapshots import RecoveryStrategy, SnapshotStore
from evoundo.witness.stores import Witness


class RecoveryEngine:
    """Coordinates execution of semantic recovery programs against harness state."""

    def __init__(
        self,
        snapshot_store: Optional[SnapshotStore] = None,
        event_logger: Optional[StructuredEventLogger] = None,
    ):
        self.snapshot_store = snapshot_store or SnapshotStore()
        self.event_logger = event_logger or default_event_logger

    def recover(
        self,
        current_state: HarnessState,
        witness: Witness,
        program: RecoveryProgram,
        strategy: RecoveryStrategy = RecoveryStrategy.EVOUNDO_RECOVERY,
        mutation_id: Optional[str] = None,
    ) -> HarnessState:
        """Execute recovery procedure to invert a target mutation."""
        m_id = mutation_id or witness.mutation_id
        target_state = current_state.clone()

        try:
            if strategy == RecoveryStrategy.EVOUNDO_RECOVERY:
                if not program.operations and witness and getattr(witness, "data", None):
                    raise RecoveryVerificationError(
                        f"Empty recovery program cannot certify external mutation '{m_id}' as restored."
                    )
                program.execute(target_state, witness)
                # Verify that post-recovery external state matches witness
                is_valid = program.verify(target_state, witness)
                if not is_valid:
                    raise RecoveryVerificationError(
                        f"Post-recovery external resource verification failed for mutation '{m_id}'. "
                        f"The actual resource state does not match the pre-mutation witness."
                    )
            elif strategy == RecoveryStrategy.FULL_SNAPSHOT:
                restored = self.snapshot_store.restore_full(m_id)
                if restored is None:
                    raise RuntimeError(f"No full snapshot available for mutation {m_id}")
                target_state = restored
            elif strategy == RecoveryStrategy.EFFECT_SCOPED_SNAPSHOT:
                restored = self.snapshot_store.restore_scoped(m_id, target_state)
                if restored is None:
                    raise RuntimeError(f"No scoped snapshot available for mutation {m_id}")
                target_state = restored
            else:
                raise ValueError(f"Unknown recovery strategy: {strategy}")

            self.event_logger.emit(
                event_type=EventType.RECOVERY_EXECUTED,
                mutation_id=m_id,
                message=f"Successfully executed recovery using strategy {strategy.value}",
                data={"strategy": strategy.value, "operations_count": len(program.operations)},
            )
            return target_state

        except Exception as e:
            self.event_logger.emit(
                event_type=EventType.RECOVERY_FAILED,
                mutation_id=m_id,
                message=f"Recovery execution failed: {str(e)}",
                data={"strategy": strategy.value, "error": str(e)},
            )
            raise
