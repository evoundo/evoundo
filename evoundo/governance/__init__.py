"""EvoUndo Governance & Customization Interfaces.

Defines generic extension points for authorization, tenancy, approval, and payload security.
"""

from evoundo.governance.authorizer import (
    RecoveryAuthorizer,
    DefaultRecoveryAuthorizer,
    AuthorizationError,
    get_recovery_authorizer,
    set_recovery_authorizer,
)
from evoundo.governance.tenant import (
    TenantContext,
    DefaultTenantContext,
    TenantAccessDeniedError,
    get_tenant_context,
    set_tenant_context,
)
from evoundo.governance.approval import (
    ApprovalProvider,
    DefaultApprovalProvider,
    ApprovalStatus,
    BaseApprovalRequest,
    get_approval_provider,
    set_approval_provider,
)
from evoundo.governance.payload import (
    PayloadProtector,
    DefaultPayloadProtector,
    get_payload_protector,
    set_payload_protector,
)

__all__ = [
    "RecoveryAuthorizer",
    "DefaultRecoveryAuthorizer",
    "AuthorizationError",
    "get_recovery_authorizer",
    "set_recovery_authorizer",
    "TenantContext",
    "DefaultTenantContext",
    "TenantAccessDeniedError",
    "get_tenant_context",
    "set_tenant_context",
    "ApprovalProvider",
    "DefaultApprovalProvider",
    "ApprovalStatus",
    "BaseApprovalRequest",
    "get_approval_provider",
    "set_approval_provider",
    "PayloadProtector",
    "DefaultPayloadProtector",
    "get_payload_protector",
    "set_payload_protector",
]
