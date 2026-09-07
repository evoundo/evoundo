# Python API Reference

EvoUndo provides clean programmatic interfaces for tool decoration, recovery orchestration, and governance customization.

---

## Agent Wrapping & Proxy API

### `wrap(agent_or_tools, **kwargs) -> Any`
Primary entry point to wrap an agent instance, a list of tool functions, or an individual tool. EvoUndo intercepts tool execution, captures pre-state witnesses, and tracks mutation identity.

```python
from evoundo import wrap

# 1. Wrap an entire agent instance (LangGraph, CrewAI, OpenAI Agents, AutoGen):
agent = wrap(my_agent)

# 2. Wrap a list of tool functions:
tools = wrap([search_tool, update_config, delete_user])

# 3. Wrap a single callable:
tool = wrap(my_function)
```

---

### `@evoundo`
Class and function decorator for transparent recovery protection.

```python
from evoundo import evoundo

@evoundo
class SREAgent:
    def __init__(self):
        self.tools = [update_config]
```

---

### `wrap_tool(fn, **kwargs) -> Callable`
Wraps an individual tool function with automatic pre-state capture and crash deduplication.

---

## Granular Tool Protection

### `@protect_tool(...)`
Universal decorator protecting state-changing functions with explicit callbacks.

```python
def protect_tool(
    target: str,
    action_class: Union[ActionClass, str] = ActionClass.REVERSIBLE,
    surface: str = "general",
    inverse_fn: Optional[Callable[..., Any]] = None,
    capture_fn: Optional[Callable[..., Any]] = None,
    post_condition_validator: Optional[Callable[[Any], bool]] = None,
    harness: Optional[EvoUndoHarness] = None,
) -> Callable:
    ...
```

**Parameters:**
- `target`: Resource address URI template (e.g. `file:///etc/hosts`, `memory://accounts/{id}`).
- `action_class`: One of `REVERSIBLE`, `COMPENSATABLE`, `RECONCILABLE`, `IRREVERSIBLE`.
- `surface`: Target datastore driver name (`file`, `sqlite`, `postgres`, `redis`, `mongodb`).
- `inverse_fn`: Callback taking `(witness, result)` to undo the mutation.
- `capture_fn`: Callback taking tool arguments to capture pre-state witness.
- `post_condition_validator`: Callback evaluating whether external state reached expected invariant.

---

### `revert(mutation_id, reason="")`
Revert an active mutation by its unique identifier.

```python
from evoundo import revert

revert("mut_1049281a", reason="Validation failure")
```
Raises `ValueError` with `CONFLICT_DETECTED` if downstream active mutations modify the same resource address.

---

### `show_history(status=None) -> List[dict]`
Return the list of recorded mutations from the active harness.

---

## Classes

### `ToolDefinition`
Dataclass representing an exported tool definition with OpenAI schema parity.

```python
tool_def = my_tool.to_tool_def()
openai_schema = tool_def.to_openai_tool()
```

### `EvoUndoHarness`
The underlying orchestration harness managing the mutation registry, journal WAL, recovery engine, and memory adapters.

```python
from evoundo.core.harness import EvoUndoHarness

harness = EvoUndoHarness.get_instance()
```

### `RecoveryTransactionGroup`
Manages multi-mutation causal recovery groups with partial failure isolation and physical verification gating.

```python
from evoundo.recovery.group import RecoveryTransactionGroup

group = RecoveryTransactionGroup(group_id="grp_42")
group.add_mutation(mutation_1)
group.add_mutation(mutation_2)
group.set_group_verifier(lambda: check_system_health())
result = group.revert(harness)
```
