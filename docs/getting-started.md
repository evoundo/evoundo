# Getting Started with EvoUndo

EvoUndo provides state recoverability and crash reconciliation for autonomous agents. This guide walks you through setting up EvoUndo, protecting your first agent tool, handling failures, and using the local Studio UI.

---

## 1. Installation

Install EvoUndo from the release wheel or from source:

```bash
# Install local release wheel
pip install dist/evoundo-0.1.0-py3-none-any.whl

# Or install editable development version
pip install -e .
```

To install with specific drivers or integrations:

```bash
# Redis datastore support
pip install "evoundo[redis]"

# MySQL datastore support
pip install "evoundo[mysql]"

# SQLAlchemy / SQLModel ORM support
pip install "evoundo[orm]"

# LangGraph agent integration
pip install "evoundo[langgraph]"

# OpenAI Agents SDK support
pip install "evoundo[openai]"
```

Verify your installation:

```bash
evoundo --version
```

---

## 2. Initialize an EvoUndo Workspace

Initialize EvoUndo in your project directory:

```bash
evoundo init
```

This creates a local `.evoundo/` directory containing the mutation registry and journal configuration.

---

## 3. Protecting an Agent

### Method A: Wrap Your Existing Agent (Recommended)

Wrap any agent instance (LangGraph, CrewAI, AutoGen, OpenAI Agents SDK, or custom classes) with `evoundo.wrap()`. EvoUndo automatically captures pre-state witnesses and suppresses duplicate retries without requiring manual lambdas:

```python
from evoundo import wrap

# Wrap your agent instance:
agent = wrap(my_agent)

# Execute the agent normally; mutations are tracked and protected:
agent.run("Optimize server configuration and update customer records")
```

You can also use the `@evoundo` decorator directly on individual functions or agent classes:

```python
from evoundo import evoundo, revert

# Decorate any state-changing function:
@evoundo
def write_config(path: str, content: str):
    with open(path, "w") as f:
        f.write(content)

write_config("app.conf", "workers = 2\n")
write_config("app.conf", "workers = 99999\n")

# Revert back to pre-state byte-for-byte:
revert()
```

Or decorate an agent class:

```python
@evoundo
class SREAgent:
    def __init__(self):
        self.tools = [update_service_config]
```

---

### Method B: Granular Tool Protection (`@protect_tool`)

For custom recovery logic or standalone functions, use the granular `@protect_tool` decorator:

```python
from pathlib import Path
from evoundo import protect_tool, revert, show_history

CONFIG_PATH = Path("/tmp/service.conf")
CONFIG_PATH.write_text("timeout=30\nworkers=4\n")

@protect_tool(
    target="file:///tmp/service.conf",
    capture_fn=lambda *args, **kwargs: CONFIG_PATH.read_text(),
    inverse_fn=lambda witness, result: CONFIG_PATH.write_text(witness),
)
def update_service_config(contents: str):
    """Update service configuration file."""
    CONFIG_PATH.write_text(contents)
    return {"status": "updated", "path": str(CONFIG_PATH)}
```

When your agent invokes this tool:

```python
# Agent mutates configuration
result = update_service_config("timeout=1\nworkers=1\n")
print(CONFIG_PATH.read_text())
# Output:
# timeout=1
# workers=1
```

EvoUndo automatically:
1. Captured the pre-mutation state (`timeout=30\nworkers=4\n`) as a durable witness.
2. Formed a recovery program binding the inverse function.
3. Registered the mutation in the local registry with status `ACTIVE`.

---

## 4. Recovering from an Unsafe Mutation

If health checks fail or the agent detects service degradation, inspect the mutation history:

```python
history = show_history()
latest_mutation = history[-1]
mutation_id = latest_mutation["mutation_id"]

print(f"Reverting mutation: {mutation_id}")
revert(mutation_id, reason="Service degradation detected by health check")

print(CONFIG_PATH.read_text())
# Output:
# timeout=30
# workers=4
```

The configuration has been cleanly restored to its pre-mutation state.

---

## 5. Crash and Retry Reconciliation

If an agent worker process crashes immediately after writing a mutation but before recording acknowledgement:

```python
# Pass a logical mutation ID to represent idempotency across retries
update_service_config(
    "timeout=10\nworkers=8\n",
    __logical_mutation_id="turn_42_config_update",
)

# Simulated worker restart: framework replays the identical turn
replayed_result = update_service_config(
    "timeout=10\nworkers=8\n",
    __logical_mutation_id="turn_42_config_update",
)

print(replayed_result)
# Output: {'cached': True, ...}
```

EvoUndo detects that `turn_42_config_update` was already committed, reconciles external state via probe, and suppresses duplicate execution.

---

## 6. Local Studio UI

EvoUndo includes a local Studio web interface to inspect mutations, diffs, and recovery status:

```bash
evoundo studio --port 8923
```

Open `http://127.0.0.1:8923` in your browser to view:
- Live timeline of agent mutations
- Visual before/after diffs
- Conflict analysis and dependency trees
- Local operator recovery actions

---

## Next Steps

- Explore the [Core Recovery Model](concepts/recovery-model.md)
- Learn about [Selective Recovery & Conflict Refusal](concepts/selective-recovery.md)
- Set up [Datastore Drivers](drivers/sqlite.md)
- Read the [API Reference](reference/api.md)
