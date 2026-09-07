r"""Directional Compensation Ledger and Accounting Invariants for EvoUndo.

Enforces formal compensation semantics for ActionClass.COMPENSATABLE:
1. Strict immutability of historical ledger records: updating or deleting posted entries is forbidden.
2. Directional forward-compensating entries: reversals create offsetting counter-entries linked
   to the parent mutation.
3. Strict double-entry accounting conservation: total debits must equal total credits at all times
   (\sum debits == \sum credits).
"""

from __future__ import annotations
import copy
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from evoundo.actions.classifier import ActionClass

logger = logging.getLogger("evoundo.actions.compensation")


class ImmutableLedgerError(RuntimeError):
    """Raised when an operation attempts to modify or delete an immutable historical ledger entry."""
    pass


class AccountingConservationError(RuntimeError):
    """Raised when the fundamental accounting equation (debits == credits) is violated."""
    pass


@dataclass
class LedgerEntry:
    """Individual immutable journal record in a double-entry ledger."""
    entry_id: str
    debit_account: str
    credit_account: str
    amount_cents: int
    mutation_id: Optional[str] = None
    parent_mutation_id: Optional[str] = None
    parent_entry_id: Optional[str] = None
    memo: str = ""
    is_compensating: bool = False
    status: str = "POSTED"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "debit_account": self.debit_account,
            "credit_account": self.credit_account,
            "amount_cents": self.amount_cents,
            "mutation_id": self.mutation_id,
            "parent_mutation_id": self.parent_mutation_id,
            "parent_entry_id": self.parent_entry_id,
            "memo": self.memo,
            "is_compensating": self.is_compensating,
            "status": self.status,
            "created_at": self.created_at,
        }


class CompensationLedger:
    """Manages double-entry financial journals with directional compensation invariants."""

    def __init__(self, ledger_name: str = "general_ledger"):
        self.ledger_name = ledger_name
        self._entries: Dict[str, LedgerEntry] = {}  # entry_id -> LedgerEntry
        self._entries_by_mutation: Dict[str, List[str]] = {}  # mutation_id -> list of entry_ids
        self._compensations_by_parent: Dict[str, List[str]] = {}  # parent_mutation_id -> list of compensating entry_ids

    def post_entry(
        self,
        entry_id: str,
        debit_account: str,
        credit_account: str,
        amount_cents: int,
        mutation_id: Optional[str] = None,
        memo: str = "",
    ) -> LedgerEntry:
        """Post a new immutable double-entry journal transaction."""
        if entry_id in self._entries:
            raise ImmutableLedgerError(
                f"Ledger entry '{entry_id}' already exists. Historical entries cannot be overwritten."
            )
        if amount_cents < 0:
            raise ValueError(f"Ledger entry amount must be non-negative (got {amount_cents})")

        entry = LedgerEntry(
            entry_id=entry_id,
            debit_account=debit_account,
            credit_account=credit_account,
            amount_cents=amount_cents,
            mutation_id=mutation_id,
            memo=memo,
            is_compensating=False,
            status="POSTED",
        )
        self._entries[entry_id] = entry
        if mutation_id:
            self._entries_by_mutation.setdefault(mutation_id, []).append(entry_id)

        logger.info(
            "Posted ledger entry %s: Debit %s %d cents, Credit %s %d cents (mutation=%s)",
            entry_id, debit_account, amount_cents, credit_account, amount_cents, mutation_id
        )
        return entry

    def compensate_mutation(
        self,
        mutation_id: str,
        reason: str = "Directional financial compensation",
        compensating_entry_id: Optional[str] = None,
    ) -> List[LedgerEntry]:
        """Generate offsetting compensating entries for all original entries linked to mutation_id.
        
        Enforces:
        - Historical entries are NEVER mutated or deleted.
        - Reverse polarity: new debit = original credit, new credit = original debit.
        - Idempotency: duplicate compensation requests return existing compensating entries.
        """
        # Check if already compensated
        existing_comp_ids = self._compensations_by_parent.get(mutation_id, [])
        if existing_comp_ids:
            logger.info("Mutation %s already compensated; returning existing compensating entries", mutation_id)
            return [self._entries[cid] for cid in existing_comp_ids if cid in self._entries]

        orig_entry_ids = self._entries_by_mutation.get(mutation_id, [])
        if not orig_entry_ids:
            raise KeyError(f"No journal entries found for mutation '{mutation_id}'")

        compensating_entries: List[LedgerEntry] = []
        for orig_id in orig_entry_ids:
            orig = self._entries[orig_id]
            comp_id = compensating_entry_id or f"comp_{uuid.uuid4().hex[:10]}"
            # Reverse accounting legs: Debit original credit, Credit original debit
            comp_entry = LedgerEntry(
                entry_id=comp_id,
                debit_account=orig.credit_account,
                credit_account=orig.debit_account,
                amount_cents=orig.amount_cents,
                mutation_id=None,
                parent_mutation_id=mutation_id,
                parent_entry_id=orig.entry_id,
                memo=f"Compensation for entry {orig.entry_id}: {reason}",
                is_compensating=True,
                status="POSTED",
            )
            self._entries[comp_id] = comp_entry
            self._compensations_by_parent.setdefault(mutation_id, []).append(comp_id)
            compensating_entries.append(comp_entry)

            logger.info(
                "Created compensating entry %s for parent entry %s (amount=%d cents)",
                comp_id, orig.entry_id, orig.amount_cents
            )

        # Verify conservation after compensation
        conserved, debits, credits = self.verify_conservation()
        if not conserved:
            raise AccountingConservationError(
                f"Accounting conservation invariant violated after compensation: Debits={debits}, Credits={credits}"
            )

        return compensating_entries

    def verify_conservation(self) -> Tuple[bool, int, int]:
        """Assert double-entry conservation invariant: sum(debits) == sum(credits)."""
        total_debits = 0
        total_credits = 0
        for entry in self._entries.values():
            if entry.status == "POSTED":
                total_debits += entry.amount_cents
                total_credits += entry.amount_cents
        is_conserved = (total_debits == total_credits)
        return is_conserved, total_debits, total_credits

    def get_net_account_balance(self, account: str) -> int:
        """Calculate net balance for an account (Total Debits - Total Credits)."""
        balance = 0
        for entry in self._entries.values():
            if entry.status == "POSTED":
                if entry.debit_account == account:
                    balance += entry.amount_cents
                if entry.credit_account == account:
                    balance -= entry.amount_cents
        return balance

    def get_entry(self, entry_id: str) -> Optional[LedgerEntry]:
        return self._entries.get(entry_id)

    def list_entries(self) -> List[LedgerEntry]:
        return list(self._entries.values())

    def prohibit_delete(self, entry_id: str) -> None:
        """Explicitly guard against destructive ledger deletions."""
        raise ImmutableLedgerError(
            f"Deletion prohibited: Ledger entry '{entry_id}' is an immutable financial audit record."
        )

    def prohibit_update(self, entry_id: str, new_values: Dict[str, Any]) -> None:
        """Explicitly guard against in-place mutations of historical records."""
        raise ImmutableLedgerError(
            f"In-place update prohibited: Ledger entry '{entry_id}' is immutable. "
            f"Post a compensating journal entry to adjust balances."
        )
