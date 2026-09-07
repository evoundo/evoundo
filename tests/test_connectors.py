import pytest

from evoundo.actions.connectors import (
    StripeConnector as ActionStripe,
    GitHubConnector as ActionGitHub,
    SalesforceConnector as ActionSalesforce,
    ServiceNowConnector as ActionServiceNow,
)
from evoundo.integrations.connectors import (
    StripeConnector,
    GitHubConnector,
    SalesforceConnector,
    ServiceNowConnector,
)
from evoundo.recovery.operations import DriverRecoveryOp


def test_connectors_import_parity():
    # Both import paths must resolve to the identical class implementations
    assert ActionStripe is StripeConnector
    assert ActionGitHub is GitHubConnector
    assert ActionSalesforce is SalesforceConnector
    assert ActionServiceNow is ServiceNowConnector


def test_stripe_connector_simulation():
    StripeConnector.register()
    # Simulate payment charge
    charge = StripeConnector.charge_customer(
        charge_id="ch_test_101",
        customer_id="cus_test_123",
        amount_cents=5000,
    )
    assert charge["status"] == "succeeded"
    assert "ch_test_101" in StripeConnector._charges

    # Execute refund compensation
    op = DriverRecoveryOp(driver_type="stripe", parameters={"charge_id": "ch_test_101"})
    StripeConnector.execute_compensation(op, None)
    assert StripeConnector.verify_compensation(op, None) is True


def test_github_connector_simulation():
    GitHubConnector.register()
    pr = GitHubConnector.create_pull_request(
        repo="evoundo/sandbox-repo",
        title="Automated Security Patch",
        head="security-fix",
        base="main",
    )
    assert pr["state"] == "open"
    pr_num = pr["number"]

    # Compensating action: close PR
    op = DriverRecoveryOp(driver_type="github", parameters={"repo": "evoundo/sandbox-repo", "pr_number": pr_num})
    GitHubConnector.execute_compensation(op, None)
    assert GitHubConnector.verify_compensation(op, None) is True
