# Databricks Omnigent Integration

EvoUndo provides runner-level tool execution middleware, policy governance, and meta-harness execution chaining for the Databricks Omnigent architecture.

---

## Architectural Role

Omnigent acts as a multi-agent meta-harness that coordinates specialist sub-agents and tool execution runners:

$$\text{Omnigent Coordinator} \longrightarrow \text{Downstream Runner (Codex / Claude)} \longrightarrow \text{EvoUndo Layer} \longrightarrow \text{External State}$$

EvoUndo attaches at the runner middleware boundary to ensure that any mutation dispatched through the chain is:
1. **Identified**: Assigned a 10-field identity envelope preserving tenant, session, application, and turn lineage.
2. **Witnessed**: Snapshotted before physical execution.
3. **Reconciled**: Protected against duplicate execution when workers restart.
4. **Reversible**: Recoverable via inverse programs with physical state verification.

---

## Usage

### 1. Transparent Agent Wrapping

Wrap an Omnigent runner or tool collection directly:

```python
from evoundo import wrap

# Wrap your Omnigent runner or tool executor:
runner = wrap(omnigent_runner)
```

### 2. Using `OmnigentAdapter` & `OmnigentMetaHarnessChain`

For programmatic meta-harness chaining:

```python
from evoundo.integrations.omnigent import OmnigentAdapter, OmnigentMetaHarnessChain
from evoundo.core.harness import EvoUndoHarness

harness = EvoUndoHarness.get_instance()
adapter = OmnigentAdapter(harness=harness)

# Create an execution chain targeting OpenAI Codex App Server:
chain = adapter.create_meta_harness_chain()

# Dispatch a mutation with full tenant and session identity:
result = chain.execute_chained_mutation(
    tool_name="update_production_table",
    arguments={"table": "users", "id": 42, "role": "admin"},
    tenant_id="tenant_finance",
    application_id="reconciliation_pipeline",
    session_id="session_8819",
    surface="postgres",
    target_addr="postgres://prod_db/users?id=42",
)
```

---

## Fault Reconciliation & Deduplication

When distributed cloud workers execute Omnigent pipelines, network partitions and worker timeouts can cause framework replays. 

EvoUndo suppresses duplicate mutations using deterministic 10-field identity:

```text
raw_id = f"omnigent:{tenant_id}:{application_id}:{session_id}:{tool_name}:{tool_call_id}"
```

If a retry occurs after a successful commit, EvoUndo probes the datastore, confirms the state already matches, and returns `{"cached": True, "status": "SUCCESS"}` without re-executing.
