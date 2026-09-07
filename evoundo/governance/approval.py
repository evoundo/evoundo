"""EvoUndo: Generic Approval Interfaces and Customization Hook."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Dict, Optional, Protocol, runtime_checkable


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass
class BaseApprovalRequest:
    """Generic metadata structure for an approval request."""
    mutation_id: str
    requested_by: str
    target: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: float = field(default_factory=time.time)
    approved_by: Optional[str] = None
    approval_timestamp: Optional[float] = None
    reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ApprovalProvider(Protocol):
    """Protocol defining multi-party approval lifecycle."""

    def is_approved(self, request: Any) -> bool:
        """Check if an approval request satisfies recovery policy."""
        ...


class DefaultApprovalProvider:
    """Default approval provider for local single-developer workflows.
    
    Treats all local recovery operations as pre-approved.
    """

    def is_approved(self, request: Any) -> bool:
        return True


_CURRENT_APPROVAL_PROVIDER: ApprovalProvider = DefaultApprovalProvider()


def get_approval_provider() -> ApprovalProvider:
    """Get the active global ApprovalProvider."""
    return _CURRENT_APPROVAL_PROVIDER


def set_approval_provider(provider: ApprovalProvider) -> None:
    """Set the active global ApprovalProvider customization hook."""
    global _CURRENT_APPROVAL_PROVIDER
    _CURRENT_APPROVAL_PROVIDER = provider

