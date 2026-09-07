"""Explicit Action Classification Ontology for EvoUndo.

Classifies mutations into four distinct action classes:
  1. REVERSIBLE: Direct state inversion possible (e.g. DB updates, file writes, S3 objects).
  2. COMPENSATABLE: Forward semantic compensation required (e.g. credit card charge -> refund).
  3. RECONCILABLE: Ambiguous outcome under network uncertainty requiring external probe reconciliation.
  4. IRREVERSIBLE: Permanently irreversible external actions requiring approval/dry-run gates (e.g. sent emails).
"""

from __future__ import annotations
from enum import Enum
import logging
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("evoundo.actions.classifier")


class ActionClass(str, Enum):
    """The four canonical action classes of EvoUndo."""
    REVERSIBLE = "REVERSIBLE"
    COMPENSATABLE = "COMPENSATABLE"
    RECONCILABLE = "RECONCILABLE"
    IRREVERSIBLE = "IRREVERSIBLE"


class ActionClassifier:
    """Classifies external operations into semantic action classes."""

    # Built-in heuristic mappings
    _REVERSIBLE_PREFIXES = (
        "db_", "sql_", "redis_", "s3_", "mongo_", "k8s_", "file_",
        "create_record", "update_record", "delete_record", "set_", "put_",
    )
    _COMPENSATABLE_KEYWORDS = (
        "charge", "payment", "invoice", "refund", "subscription",
        "order", "book_flight", "reserve", "transfer_funds", "create_pr",
    )
    _RECONCILABLE_KEYWORDS = (
        "webhook", "async_job", "dispatch_task", "submit_batch",
        "third_party_sync", "publish_event", "stream_emit",
    )
    _IRREVERSIBLE_KEYWORDS = (
        "send_email", "send_sms", "post_tweet", "physical_shipment",
        "format_disk", "destroy_tenant", "wire_transfer_final",
    )

    @classmethod
    def classify(
        cls,
        tool_name: str,
        target: Optional[str] = None,
        explicit_class: Optional[ActionClass] = None,
    ) -> ActionClass:
        """Determine the action class for an operation."""
        if explicit_class is not None:
            return explicit_class

        lowered_name = tool_name.lower()
        lowered_target = (target or "").lower()

        # Check Irreversible first (highest risk)
        for kw in cls._IRREVERSIBLE_KEYWORDS:
            if kw in lowered_name or kw in lowered_target:
                return ActionClass.IRREVERSIBLE

        # Check Compensatable
        for kw in cls._COMPENSATABLE_KEYWORDS:
            if kw in lowered_name or kw in lowered_target:
                return ActionClass.COMPENSATABLE

        # Check Reconcilable
        for kw in cls._RECONCILABLE_KEYWORDS:
            if kw in lowered_name or kw in lowered_target:
                return ActionClass.RECONCILABLE

        # Default to Reversible
        return ActionClass.REVERSIBLE


class IrreversibleActionBlockedError(RuntimeError):
    """Raised when an autonomous agent attempts an IRREVERSIBLE action without operator approval."""
    pass
