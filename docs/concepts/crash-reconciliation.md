# Crash and Retry Reconciliation

Autonomous agent execution involves asynchronous tool calls, distributed networks, and ephemeral compute workers. Process crashes and timeouts frequently occur *after* an external mutation has been committed but *before* the agent runtime records completion.

---

## 1. The Post-Commit / Pre-ACK Failure Window

Consider an autonomous agent executing a mutation:

```text
Agent Runtime               EvoUndo                External Datastore
      │                        │                            │
      │── 1. execute_tool() ──>│                            │
      │                        │── 2. Capture witness ─────>│
      │                        │── 3. Apply mutation ──────>│
      │                        │<─ 4. Success / Commit ─────│
      │                        │                            │
   💥 5. WORKER CRASHES BEFORE PERSISTING JOURNAL ACK       │
      │                                                     │
(Worker restarts and replays identical turn)                │
      │                                                     │
      │── 6. execute_tool() ──>│                            │
      │   (__logical_id="T1")  │── 7. Inspect probe ───────>│
      │                        │<─ 8. State already matches─│
      │<─ 9. Cached result ────│                            │
      │  (duplicate suppressed)│                            │
```

Without crash reconciliation, the restarted agent would re-execute the mutation, causing duplicate writes, balance deductions, or corrupted state.

---

## 2. Logical Mutation Identity

EvoUndo uses logical mutation identities (`__logical_mutation_id` or `logical_id`) to correlate retried operations:

```python
@protect_tool(target="file:///tmp/config.json")
def configure_server(settings: dict):
    ...
```

When an agent framework invokes the tool, it supplies a deterministic turn or task ID:

```python
configure_server(settings={"mode": "prod"}, __logical_mutation_id="task_turn_104")
```

If the runtime crashes and replays `task_turn_104`, EvoUndo:
1. Detects that `task_turn_104` has already been recorded.
2. Checks whether the external state matches the expected post-condition via the registered probe.
3. If confirmed, returns `{"cached": True, ...}` and suppresses duplicate physical execution.
4. If the pre-crash mutation did not commit, EvoUndo allows execution to proceed cleanly.

---

## 3. Duplicate Suppression vs. Exactly-Once Semantics

EvoUndo provides **duplicate-execution suppression under supported reconciliation scenarios**.

> [!NOTE]
> EvoUndo does **not** claim universal distributed exactly-once execution across arbitrary uninstrumented third-party APIs. Duplicate suppression is guaranteed for supported mutation surfaces (files, relational databases, Redis, MongoDB) where deterministic state probes verify physical external reality.
