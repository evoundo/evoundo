# Compensation Workflows

In real-world agent environments, many external actions cannot be physically unwound. For example, once an email is dispatched or a Stripe payment is settled, the external world has irreversibly moved forward.

---

## 1. Physical Inverse vs. Semantic Compensation

| Dimension | Physical Inverse (`REVERSIBLE`) | Semantic Compensation (`COMPENSATABLE`) |
|---|---|---|
| **Mechanism** | Restores exact pre-image bytes or rows | Executes a new forward transaction that neutralizes the business effect |
| **History** | State returns to prior baseline | New forward record appended to ledger |
| **Example** | Restoring a backup configuration file | Calling Stripe Refund API after a charge |

---

## 2. Implementing a Compensating Action

When protecting an action classified as `COMPENSATABLE`, provide a compensation function:

```python
from evoundo import protect_tool
from evoundo.actions.compensation import CompensationLedger

ledger = CompensationLedger()

def refund_charge(witness, result):
    charge_id = result["charge_id"]
    amount = witness["amount"]
    # Issue refund via API
    stripe.Refund.create(charge=charge_id, amount=amount)
    ledger.record_compensation(
        action_name="charge_customer",
        compensating_action="stripe.Refund.create",
        context={"charge_id": charge_id, "amount": amount},
    )

@protect_tool(
    target="api://billing/charge",
    action_class="COMPENSATABLE",
    capture_fn=lambda customer_id, amount: {"customer_id": customer_id, "amount": amount},
    inverse_fn=refund_charge,
)
def charge_customer(customer_id: str, amount: int):
    charge = stripe.Charge.create(customer=customer_id, amount=amount, currency="usd")
    return {"charge_id": charge.id, "status": "succeeded"}
```

When `revert()` is called on a compensatable mutation:
1. EvoUndo executes the compensating function (`refund_charge`).
2. The compensation is recorded in the durable `CompensationLedger`.
3. The mutation status updates to `REVERTED` with an explicit compensation audit record.

---

## 3. Irreversible Action Fencing

If an action cannot be compensated and is classified as `IRREVERSIBLE`, EvoUndo guards against accidental rollback attempts:

```python
@protect_tool(
    target="api://notifications/sms",
    action_class="IRREVERSIBLE",
)
def send_customer_alert(phone: str, message: str):
    twilio.messages.create(to=phone, body=message)
```

Attempting to call `revert()` on this mutation raises an `IrreversibleActionBlockedError`.
