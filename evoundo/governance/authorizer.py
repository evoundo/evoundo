"""EvoUndo: Recovery Authorization Interfaces and Customization Hook."""

from __future__ import annotations
from typing import Any, Optional, Protocol, runtime_checkable


class AuthorizationError(PermissionError):
    """Base exception raised when recovery authorization is rejected."""
    pass


@runtime_checkable
class RecoveryAuthorizer(Protocol):
    """Protocol defining the recovery authorization contract for EvoUndo."""

    def authorize_revert(
        self,
        mutation_record: Any,
        caller: Optional[Any] = None,
        approval_request: Optional[Any] = None,
    ) -> None:
        """Authorize or reject an attempted mutation revert."""
        ...


class DefaultRecoveryAuthorizer:
    """Default recovery authorizer for local developer environments.
    
    Permits local reverts without requiring external approval hooks.
    """

    @classmethod
    def authorize_revert(
        cls,
        mutation_record: Any,
        caller: Optional[Any] = None,
        approval_request: Optional[Any] = None,
    ) -> None:
        return


_CURRENT_RECOVERY_AUTHORIZER: RecoveryAuthorizer = DefaultRecoveryAuthorizer()


def get_recovery_authorizer() -> RecoveryAuthorizer:
    """Get the active global RecoveryAuthorizer."""
    return _CURRENT_RECOVERY_AUTHORIZER


def set_recovery_authorizer(authorizer: RecoveryAuthorizer) -> None:
    """Set the active global RecoveryAuthorizer customization hook."""
    global _CURRENT_RECOVERY_AUTHORIZER
    _CURRENT_RECOVERY_AUTHORIZER = authorizer

