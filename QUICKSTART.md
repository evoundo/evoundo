# EvoUndo 5-Minute Quickstart

This walkthrough shows how to protect a state-changing agent tool, inspect its mutation history, recover a bad change, and handle supported retry scenarios.

---

## 1. Installation

Install from source in editable development mode:

```bash
git clone https://github.com/evoundo/evoundo.git
cd evoundo
pip install -e .
```

Or install via pip (when using PyPI or a pre-built wheel):

```bash
pip install evoundo
```

---

## 2. Option A: Wrap Your Entire Agent (Fastest — 30 Seconds)

If you already have an agent (LangGraph, CrewAI, OpenAI Agents, or custom Python):

```python
from evoundo import wrap

# 1-line protection: auto-detects tools, files, and state mutations
agent = wrap(my_agent)

# Or wrap a list of tool functions directly:
tools = wrap([update_service_config, deploy_cluster])
```

EvoUndo automatically inspects the agent's tools, auto-detects file and resource targets, snapshots pre-state baselines, and intercepts retries without requiring custom lambdas.

---

## 2. Option B: Granular Tool Protection (`@protect_tool`)

For custom tools or fine-grained business logic, decorate individual tools directly:

```python
from pathlib import Path
from evoundo import protect_tool, revert, show_history

CONFIG = Path("/tmp/service.conf")

# Baseline initial configuration
CONFIG.write_text("timeout=30\nworkers=4\n")

@protect_tool(
    target="file:///tmp/service.conf",
    capture_fn=lambda *args, **kwargs: CONFIG.read_text(),
    inverse_fn=lambda witness, result: CONFIG.write_text(witness),
)
def update_service_config(contents: str):
    """Update service configuration file."""
    CONFIG.write_text(contents)
    return {"status": "updated", "path": str(CONFIG)}
```

---

## 3. Autonomous Mutation Execution

The autonomous agent executes a change:

```python
# Agent writes a faulty low-timeout configuration
result = update_service_config(
    "timeout=1\nworkers=1\n",
    __logical_mutation_id="agent_turn_101",
)

print(CONFIG.read_text())
# Output:
# timeout=1
# workers=1
```

EvoUndo automatically captured the pre-mutation baseline (`timeout=30\nworkers=4\n`) and recorded an active mutation in the local registry.

---

## 4. Crash & Retry Reconciliation

If the agent worker crashes immediately after the write and the execution framework replays the turn:

```python
# Framework retries turn with the same logical ID
replayed = update_service_config(
    "timeout=1\nworkers=1\n",
    __logical_mutation_id="agent_turn_101",
)

print(replayed)
# Output: {'status': 'SUCCESS', 'cached': True, 'logical_mutation_id': 'agent_turn_101'}
```

EvoUndo recognizes the logical mutation identity and suppresses duplicate execution for supported retry/reconciliation paths.

---

## 5. Recover the Mutation

When a health check detects service degradation, the agent or operator requests recovery:

```python
# Inspect active mutations
history = show_history()
latest_mutation = history[-1]
mutation_id = latest_mutation["mutation_id"]

# Revert the faulty mutation
revert(mutation_id, reason="Health check failed: service degradation")

# Verify that pre-mutation configuration is restored
print(CONFIG.read_text())
# Output:
# timeout=30
# workers=4
```

The previous configuration is restored. The final `read_text()` call verifies the resulting file contents in this walkthrough.

Supported EvoUndo drivers can also use registered post-condition probes to verify external state before reporting recovery success.

---

## 6. Inspect with Local Studio UI

Launch the local web-based Studio UI to inspect mutation timelines, diffs, recovery status, and verification results:

```bash
evoundo studio --port 8923
```

Open `http://127.0.0.1:8923` in your browser.

---

## Next Steps

- [Core Concepts & Recovery Model](docs/concepts/recovery-model.md)
- [Crash Reconciliation & Failure Windows](docs/concepts/crash-reconciliation.md)
- [Datastore Drivers](docs/drivers/)
- [Framework Integrations](docs/integrations/)
- [Python API Reference](docs/reference/api.md)