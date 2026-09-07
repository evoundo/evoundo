"""EvoUndo: Tenancy Interfaces, Local Context, and Customization Hook."""

from __future__ import annotations
from typing import Optional, Protocol, runtime_checkable


class TenantAccessDeniedError(PermissionError):
    """Raised when access to a resource across tenant boundaries is rejected."""
    pass


@runtime_checkable
class TenantContext(Protocol):
    """Protocol for tenant boundary validation."""

    def get_current_tenant(self) -> str:
        """Return the active tenant identifier for the current execution context."""
        ...

    def validate_access(self, caller_tenant_id: Optional[str], target_tenant_id: Optional[str]) -> None:
        """Validate whether caller_tenant_id is authorized to access target_tenant_id."""
        ...


class DefaultTenantContext:
    """Default single-tenant context for local developer and research use.
    
    Permits local operations within the default tenant partition while enforcing
    basic mismatch safety when explicit tenant identifiers are supplied.
    """

    def __init__(self, default_tenant: str = "default") -> None:
        self.default_tenant = default_tenant

    def get_current_tenant(self) -> str:
        return self.default_tenant

    def validate_access(self, caller_tenant_id: Optional[str], target_tenant_id: Optional[str]) -> None:
        if not caller_tenant_id or not target_tenant_id:
            return
        if caller_tenant_id != target_tenant_id:
            raise TenantAccessDeniedError(
                f"TENANT_ISOLATION_VIOLATION: Caller tenant '{caller_tenant_id}' "
                f"cannot access target tenant '{target_tenant_id}'."
            )


_CURRENT_TENANT_CONTEXT: TenantContext = DefaultTenantContext()


def get_tenant_context() -> TenantContext:
    """Get the active global TenantContext."""
    return _CURRENT_TENANT_CONTEXT


def set_tenant_context(context: TenantContext) -> None:
    """Set the active global TenantContext customization hook."""
    global _CURRENT_TENANT_CONTEXT
    _CURRENT_TENANT_CONTEXT = context

