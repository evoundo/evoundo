"""Recovery Transaction Group Abstraction for EvoUndo.

Binds multiple interdependent mutations across heterogeneous surfaces
(e.g., model promotion + configuration update + traffic routing) into a
coordinated, causally-ordered recovery group with partial-failure detection and resumable recovery.

Uses semantic type RECOVERY_GROUP (not synthetic global ACID).
Does not claim distributed two-phase commit or cross-vendor distributed locks.
"""

from __future__ import annotations
from enum import Enum
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("evoundo.recovery.group")


class GroupStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMMITTED = "COMMITTED"
    RECOVERING = "RECOVERING"
    RECOVERED = "RECOVERED"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    CONFLICTED = "CONFLICTED"


@dataclass
class GroupRevertResult:
    group_id: str
    success: bool
    reverted_mutation_ids: List[str]
    failed_mutation_id: Optional[str] = None
    error_message: Optional[str] = None
    elapsed_ms: float = 0.0
    status: GroupStatus = GroupStatus.RECOVERED


class RecoveryTransactionGroup:
    """Manages multi-mutation causal recovery groups with partial failure isolation."""

    SEMANTIC_TYPE = "RECOVERY_GROUP"

    def __init__(
        self,
        group_id: Optional[str] = None,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.group_id = group_id or f"grp_{uuid.uuid4().hex[:10]}"
        self.description = description
        self.metadata = metadata or {}
        self.mutation_ids: List[str] = []
        self.causal_dependencies: Dict[str, Set[str]] = {}  # child_id -> set(parent_ids)
        self.preconditions: List[Callable[[], bool]] = []
        self.group_verifier: Optional[Callable[[], bool]] = None
        self.status: GroupStatus = GroupStatus.ACTIVE
        self.reverted_mutation_ids: List[str] = []
        self.failed_mutation_id: Optional[str] = None
        self.error_message: Optional[str] = None
        self.created_at: float = time.time()

    def add_mutation(self, mutation_id: str, depends_on: Optional[List[str]] = None) -> None:
        """Append a mutation to this recovery group in causal order."""
        if self.status not in (GroupStatus.ACTIVE, "OPEN"):
            raise RuntimeError(f"Cannot add mutation to recovery group in state {self.status.value if isinstance(self.status, GroupStatus) else self.status}")
        if mutation_id not in self.mutation_ids:
            self.mutation_ids.append(mutation_id)
        if depends_on:
            self.causal_dependencies.setdefault(mutation_id, set()).update(depends_on)
        logger.debug("Added mutation %s to group %s", mutation_id, self.group_id)

    def add_precondition(self, predicate: Callable[[], bool]) -> None:
        """Add a precondition predicate that must evaluate to True before recovery begins."""
        self.preconditions.append(predicate)

    def set_group_verifier(self, verifier: Callable[[], bool]) -> None:
        """Set an overall post-recovery physical verifier for the entire group."""
        self.group_verifier = verifier

    def commit(self) -> None:
        """Seal the group as complete and ready for recovery if needed."""
        self.status = GroupStatus.COMMITTED

    def get_reverse_causal_order(self) -> List[str]:
        """Compute execution order for recovery: child mutations must be reverted before parents.
        
        Uses topological ordering respecting causal dependencies, falling back to reverse
        insertion order for mutations without explicit dependency constraints.
        """
        order: List[str] = []
        visited: Set[str] = set()

        def visit(n: str, ancestors: Set[str]) -> None:
            if n in ancestors:
                # Cycle detected in dependencies; fallback to insertion order
                return
            if n not in visited:
                ancestors.add(n)
                # Ensure all dependents of n that depend on n are visited first
                for child in self.mutation_ids:
                    if n in self.causal_dependencies.get(child, set()):
                        visit(child, ancestors)
                ancestors.remove(n)
                visited.add(n)
                if n not in order:
                    order.append(n)

        # Iterate in reverse insertion order
        for m in reversed(self.mutation_ids):
            if m not in visited:
                visit(m, set())

        # Ensure all mutations are included
        for m in reversed(self.mutation_ids):
            if m not in order:
                order.append(m)

        return order

    def revert(
        self,
        harness: Any,
        reason: str = "Grouped causal rollback",
        caller_context: Optional[Any] = None,
        approval_request: Optional[Any] = None,
    ) -> GroupRevertResult:
        """Revert all mutations in this group in reverse causal order.
        
        Features:
        - Evaluates preconditions before attempting reverts.
        - Supports idempotent resume: skips already-reverted mutations.
        - Partial failure isolation: if member 1 succeeds but member 2 fails,
          status transitions to PARTIALLY_RECOVERED (never reported as RECOVERED).
        - Conflict handling: downstream conflicts transition status to CONFLICTED.
        - Physical group verifier assertion upon completion.
        """
        t0 = time.perf_counter()

        # Check preconditions
        for idx, pre in enumerate(self.preconditions):
            try:
                if not pre():
                    dur = (time.perf_counter() - t0) * 1000.0
                    self.status = GroupStatus.RECOVERY_FAILED
                    self.error_message = f"Precondition #{idx+1} failed before recovery"
                    return GroupRevertResult(
                        group_id=self.group_id,
                        success=False,
                        reverted_mutation_ids=list(self.reverted_mutation_ids),
                        failed_mutation_id=None,
                        error_message=self.error_message,
                        elapsed_ms=dur,
                        status=self.status,
                    )
            except Exception as e:
                dur = (time.perf_counter() - t0) * 1000.0
                self.status = GroupStatus.RECOVERY_FAILED
                self.error_message = f"Precondition #{idx+1} raised error: {e}"
                return GroupRevertResult(
                    group_id=self.group_id,
                    success=False,
                    reverted_mutation_ids=list(self.reverted_mutation_ids),
                    failed_mutation_id=None,
                    error_message=self.error_message,
                    elapsed_ms=dur,
                    status=self.status,
                )

        self.status = GroupStatus.RECOVERING
        causal_order = self.get_reverse_causal_order()

        # Idempotent resume: filter out already reverted mutations
        pending_mutations = [m for m in causal_order if m not in self.reverted_mutation_ids]

        for m_id in pending_mutations:
            try:
                # Support both harness signature with caller_context / approval_request and simple signature
                kwargs: Dict[str, Any] = {"reason": f"{reason} (Group {self.group_id})"}
                if caller_context is not None:
                    kwargs["caller_context"] = caller_context
                if approval_request is not None:
                    kwargs["approval_request"] = approval_request

                # Call harness.revert
                res = harness.revert(m_id, **kwargs)
                if res is None:
                    dur = (time.perf_counter() - t0) * 1000.0
                    self.failed_mutation_id = m_id
                    self.error_message = f"Harness.revert for mutation {m_id} returned None"
                    self.status = (
                        GroupStatus.PARTIALLY_RECOVERED
                        if self.reverted_mutation_ids
                        else GroupStatus.RECOVERY_FAILED
                    )
                    return GroupRevertResult(
                        group_id=self.group_id,
                        success=False,
                        reverted_mutation_ids=list(self.reverted_mutation_ids),
                        failed_mutation_id=m_id,
                        error_message=self.error_message,
                        elapsed_ms=dur,
                        status=self.status,
                    )

                self.reverted_mutation_ids.append(m_id)
                logger.info("Group %s successfully reverted member mutation %s", self.group_id, m_id)

            except Exception as e:
                dur = (time.perf_counter() - t0) * 1000.0
                self.failed_mutation_id = m_id
                self.error_message = str(e)
                err_lower = str(e).lower()

                if "conflict" in err_lower or "conflict_detected" in err_lower:
                    self.status = GroupStatus.CONFLICTED
                elif self.reverted_mutation_ids:
                    self.status = GroupStatus.PARTIALLY_RECOVERED
                else:
                    self.status = GroupStatus.RECOVERY_FAILED

                logger.error("Group revert failed at mutation %s (status=%s): %s", m_id, self.status.value, e)
                return GroupRevertResult(
                    group_id=self.group_id,
                    success=False,
                    reverted_mutation_ids=list(self.reverted_mutation_ids),
                    failed_mutation_id=m_id,
                    error_message=self.error_message,
                    elapsed_ms=dur,
                    status=self.status,
                )

        # Check overall group physical verifier if provided
        if self.group_verifier is not None:
            try:
                verified = self.group_verifier()
                if not verified:
                    dur = (time.perf_counter() - t0) * 1000.0
                    self.status = (
                        GroupStatus.PARTIALLY_RECOVERED
                        if self.reverted_mutation_ids
                        else GroupStatus.RECOVERY_FAILED
                    )
                    self.error_message = "Group physical verifier assertion failed after member reverts"
                    return GroupRevertResult(
                        group_id=self.group_id,
                        success=False,
                        reverted_mutation_ids=list(self.reverted_mutation_ids),
                        failed_mutation_id=None,
                        error_message=self.error_message,
                        elapsed_ms=dur,
                        status=self.status,
                    )
            except Exception as e:
                dur = (time.perf_counter() - t0) * 1000.0
                self.status = (
                    GroupStatus.PARTIALLY_RECOVERED
                    if self.reverted_mutation_ids
                    else GroupStatus.RECOVERY_FAILED
                )
                self.error_message = f"Group physical verifier raised exception: {e}"
                return GroupRevertResult(
                    group_id=self.group_id,
                    success=False,
                    reverted_mutation_ids=list(self.reverted_mutation_ids),
                    failed_mutation_id=None,
                    error_message=self.error_message,
                    elapsed_ms=dur,
                    status=self.status,
                )

        self.status = GroupStatus.RECOVERED
        dur = (time.perf_counter() - t0) * 1000.0
        return GroupRevertResult(
            group_id=self.group_id,
            success=True,
            reverted_mutation_ids=list(self.reverted_mutation_ids),
            elapsed_ms=dur,
            status=self.status,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "description": self.description,
            "semantic_type": self.SEMANTIC_TYPE,
            "mutation_ids": list(self.mutation_ids),
            "reverted_mutation_ids": list(self.reverted_mutation_ids),
            "status": self.status.value if isinstance(self.status, GroupStatus) else self.status,
            "failed_mutation_id": self.failed_mutation_id,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }
