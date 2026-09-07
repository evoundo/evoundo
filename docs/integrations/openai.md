# OpenAI Agent & Tool Integration

EvoUndo provides first-class support for OpenAI tool calling protocols, including native JSON Schema definition generation via `.to_tool_def().to_openai_tool()`.

---

## 1. Installation

```bash
pip install "evoundo[openai]"
```

---

## 2. Tool Definition Export

You can protect any standard Python function and directly export it into the OpenAI function schema:

```python
from pathlib import Path
from evoundo import protect_tool

DEPLOY_FILE = Path("/tmp/deployment.yaml")

@protect_tool(
    target="file:///tmp/deployment.yaml",
    capture_fn=lambda manifest: DEPLOY_FILE.read_text() if DEPLOY_FILE.exists() else "",
    inverse_fn=lambda witness, result: DEPLOY_FILE.write_text(witness),
)
def deploy_service(manifest: str) -> dict:
    """Deploy a service specification to the cluster."""
    DEPLOY_FILE.write_text(manifest)
    return {"status": "deployed", "bytes": len(manifest)}

# Export directly to OpenAI JSON tool definition format
tool_schema = deploy_service.to_tool_def().to_openai_tool()
print(tool_schema)
# Output:
# {
#   "type": "function",
#   "function": {
#     "name": "deploy_service",
#     "description": "Deploy a service specification to the cluster.",
#     "parameters": { ... }
#   }
# }
```

---

## 3. Tool Calling Loop with Retry Suppression

Pass the OpenAI `tool_call_id` as the logical mutation ID:

```python
from openai import OpenAI

client = OpenAI()

# In your chat completion tool handling loop:
for tool_call in message.tool_calls:
    if tool_call.function.name == "deploy_service":
        args = json.loads(tool_call.function.arguments)
        
        # Bind the OpenAI tool_call_id for crash & retry reconciliation
        result = deploy_service(
            manifest=args["manifest"],
            __logical_mutation_id=tool_call.id,
        )
```

If the agent connection drops and OpenAI retries the identical tool call, EvoUndo detects the duplicate `tool_call_id` and safely returns the cached result without duplicate execution.
