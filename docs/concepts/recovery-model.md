# Core Recovery Model

EvoUndo classifies mutations according to their physical and logical recoverability properties. Rather than treating all tool executions as black boxes, EvoUndo models how state changes can be safely undone, compensated, or guarded.

---

## 1. Action Classification Taxonomy

EvoUndo defines four fundamental action classes:

### `REVERSIBLE`
The original state can be restored directly to its exact pre-mutation state using an inverse function or stored pre-image witness.

**Examples:**
- Updating a database row or JSON configuration
- Writing or appending to a file on disk
- Setting an in-memory key or cache entry
- Git working-tree edits

### `COMPENSATABLE`
The original action cannot be literally reversed (e.g., an external side-effect took place), but a subsequent semantic action can neutralize its business impact.

**Examples:**
- Charging a customer credit card $\to$ issuing an explicit refund
- Creating a calendar reservation $\to$ cancelling the reservation
- Publishing a message to a topic $\to$ sending a correction / retraction

### `RECONCILABLE`
The system cannot directly apply an inverse or simple compensation, but an observer can inspect current external state and converge toward the target invariant.

**Examples:**
- Asynchronous batch jobs
- Infrastructure provisioning drift (e.g. Terraform / CloudFormation state)
- Eventually consistent key-value stores

### `IRREVERSIBLE`
The action cannot be reversed, compensated, or reconciled automatically.

**Examples:**
- Sending an unrecallable email or SMS
- Triggering a physical actuator or industrial robot
- Deleting an unbacked-up root encryption key
- Public disclosure of confidential data

EvoUndo **fails closed** if an attempt is made to revert an `IRREVERSIBLE` action, requiring explicit human operator intervention or blocking unsafe automated rollbacks.

---

## 2. Mutation Lifecycle

Every protected tool execution passes through a rigorous lifecycle:

```text
1. Pre-Execution Admission
   ├── Check feasibility and resource locks
   └── Capture pre-state witness (snapshot / pre-image)
         ↓
2. Physical Mutation Execution
   └── Execute the underlying tool function
         ↓
3. Post-Execution Registration
   ├── Capture post-state probe verification
   ├── Record durable journal WAL entry
   └── Append mutation to causal tree
         ↓
4. Recovery Phase (When Requested)
   ├── Validate caller authorization
   ├── Check same-resource conflicts
   ├── Execute inverse / compensation program
   ├── Physically probe external state
   └── Invalidate dependent agent memory
```

---

## 3. Pre-State Witnesses

A witness is an immutable snapshot of the minimum state necessary to reverse a mutation. Witnesses are captured *before* the mutation executes:

- **File / Config**: Full file content, hash, or diff pre-image.
- **SQL Database**: Pre-mutation column values for the affected primary keys.
- **Redis**: Pre-existing string value, hash field map, or TTL expiration timestamp.
- **Custom**: Application-defined witness dictionary returned by `capture_fn`.

Witnesses pass through sensitive data redaction (`PayloadProtector`) before persistence to ensure credentials and tokens are not leaked into journals.
