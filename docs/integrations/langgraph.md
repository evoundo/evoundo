# LangGraph Integration

LangGraph provides graph execution and state checkpointing for LLM workflows. However, while LangGraph checkpoints agent state in memory or a database, it cannot automatically undo side-effects committed against external services (e.g. databases, file systems, cloud APIs).

EvoUndo bridges this gap by protecting tools invoked within LangGraph nodes.

---

## 1. Installation

```bash
pip install "evoundo[langgraph]"
```

---

## 2. Usage Pattern

Wrap your custom LangGraph tools with `@protect_tool`:

```python
from pathlib import Path
from langchain_core.tools import tool
from evoundo import protect_tool, revert

CONFIG_PATH = Path("/tmp/app_config.json")

@tool
@protect_tool(
    target="file:///tmp/app_config.json",
    capture_fn=lambda *args, **kwargs: CONFIG_PATH.read_text() if CONFIG_PATH.exists() else "{}",
    inverse_fn=lambda witness, result: CONFIG_PATH.write_text(witness),
)
def update_configuration(config_json: str) -> str:
    """Updates the service configuration file."""
    CONFIG_PATH.write_text(config_json)
    return "Configuration updated successfully."
```

---

## 3. Integrating with LangGraph State Checkpointers

When building a graph:

```python
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

tools = [update_configuration]
tool_node = ToolNode(tools)

# When a node fails or an evaluation detects that the agent made an incorrect edit:
def recovery_node(state):
    last_mutation_id = state.get("last_mutation_id")
    if last_mutation_id:
        revert(last_mutation_id, reason="Graph execution failed validation")
    return {"status": "reverted"}
```

EvoUndo handles the external file or database rollback, while LangGraph manages internal graph branching.
