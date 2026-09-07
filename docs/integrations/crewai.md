# CrewAI Integration

CrewAI organizes autonomous agents into multi-agent teams executing collaborative tasks. When an agent in a crew makes an erroneous mutation, EvoUndo provides selective recovery so that only the faulty task's mutation is unwound without interrupting peer agents.

---

## 1. Installation

```bash
pip install "evoundo[crewai]"
```

---

## 2. Wrapping Custom CrewAI Tools

CrewAI tools derive from `BaseTool`. You can wrap the `_run` method with `@protect_tool`:

```python
from pathlib import Path
from crewai.tools import BaseTool
from evoundo import protect_tool

ENV_FILE = Path("/tmp/.env.stage")

class EnvironmentConfigTool(BaseTool):
    name: str = "update_env_config"
    description: str = "Update stage environment configuration key-value pair."

    @protect_tool(
        target="file:///tmp/.env.stage",
        capture_fn=lambda self, key, value: ENV_FILE.read_text() if ENV_FILE.exists() else "",
        inverse_fn=lambda witness, result: ENV_FILE.write_text(witness),
    )
    def _run(self, key: str, value: str) -> str:
        with open(ENV_FILE, "a") as f:
            f.write(f"{key}={value}\n")
        return f"Appended {key}={value}"
```

---

## 3. Causal Isolation in Multi-Agent Crews

In multi-agent crews:
1. Agent 1 (DevOps) updates `/tmp/.env.stage`.
2. Agent 2 (QA) writes test reports to `/tmp/reports/qa.log`.
3. If Agent 1's configuration is invalid, calling `revert()` restores `/tmp/.env.stage` while leaving Agent 2's QA reports completely untouched.
