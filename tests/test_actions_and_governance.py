import pytest

from evoundo.actions.classifier import ActionClass, ActionClassifier
from evoundo.actions.compensation import CompensationLedger, ImmutableLedgerError
from evoundo.actions.broadcast import CompensatableBroadcastChannel
from evoundo.governance import (
    RecoveryAuthorizer,
    DefaultRecoveryAuthorizer,
    AuthorizationError,
    get_recovery_authorizer,
    set_recovery_authorizer,
    TenantContext,
    DefaultTenantContext,
    TenantAccessDeniedError,
    get_tenant_context,
    set_tenant_context,
    ApprovalProvider,
    DefaultApprovalProvider,
    PayloadProtector,
    DefaultPayloadProtector,
)


def test_action_classification():
    classifier = ActionClassifier()
    assert classifier.classify("db_update_user") == ActionClass.REVERSIBLE
    assert classifier.classify("charge_payment") == ActionClass.COMPENSATABLE
    assert classifier.classify("webhook_dispatch") == ActionClass.RECONCILABLE
    assert classifier.classify("send_email_notification") == ActionClass.IRREVERSIBLE


def test_compensation_ledger():
    ledger = CompensationLedger()
    entry = ledger.post_entry(
        entry_id="tx_101",
        debit_account="cash",
        credit_account="revenue",
        amount_cents=5000,
        mutation_id="mut_payment_1",
        memo="Product sale",
    )
    assert entry.amount_cents == 5000
    assert "tx_101" in ledger._entries

    # Attempting to re-post identical entry must raise ImmutableLedgerError
    with pytest.raises(ImmutableLedgerError):
        ledger.post_entry(
            entry_id="tx_101",
            debit_account="cash",
            credit_account="revenue",
            amount_cents=5000,
        )


def test_customization_hook_recovery_authorizer():
    # Verify default allows
    authorizer = get_recovery_authorizer()
    authorizer.authorize_revert(None)

    # Custom authorizer that blocks
    class StrictAuthorizer:
        def authorize_revert(self, mutation_record, caller=None, approval_request=None):
            raise AuthorizationError("Blocked by custom security authorizer policy")

    set_recovery_authorizer(StrictAuthorizer())
    with pytest.raises(AuthorizationError):
        get_recovery_authorizer().authorize_revert(None)

    # Reset
    set_recovery_authorizer(DefaultRecoveryAuthorizer())


def test_customization_hook_tenant_context():
    ctx = DefaultTenantContext(default_tenant="tenant_alpha")
    # Same tenant access allowed
    ctx.validate_access("tenant_alpha", "tenant_alpha")

    # Mismatched tenant access blocked
    with pytest.raises(TenantAccessDeniedError):
        ctx.validate_access("tenant_alpha", "tenant_beta")


def test_customization_hook_payload_protector():
    protector = DefaultPayloadProtector()
    sensitive_data = {
        "user": "alice",
        "api_key": "sk-secret-12345",
        "nested": {
            "password": "super_secret_pw",
            "normal": "value",
        },
    }
    redacted = protector.redact(sensitive_data)
    assert redacted["user"] == "alice"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["normal"] == "value"
