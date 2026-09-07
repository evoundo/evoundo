# Datastore Crash Consistency Matrix

EvoUndo provides deterministic state consistency and reconciliation across 11 critical lifecycle execution boundaries.

---

## 11-Boundary Lifecycle Execution Matrix

| # | Lifecycle Execution Point | Failure Mode / Fault Injected | EvoUndo Guarantee & Reconciliation Behavior |
|:---:|:---|:---|:---|
| **1** | **Pre-call Argument Validation** | Invalid parameters, type mismatch | Execution fails before witness capture; 0 journal records created. |
| **2** | **Resource Address Resolution** | Malformed resource URI | Fails closed; mutation rejected prior to external system contact. |
| **3** | **Pre-State Witness Capture** | Datastore read timeout or failure | Mutation aborted immediately; external side-effect is prevented. |
| **4** | **Inverse Program Synthesis** | Unsupported mutation / missing inverse | Admission rejected; external mutation is blocked. |
| **5** | **Pre-Execution Journal Flush** | Disk full or WAL write failure | External tool execution blocked; system fails closed. |
| **6** | **Physical Side-Effect Execution** | Crash during external mutation call | Fresh process detects partial state; marks mutation uncommitted. |
| **7** | **Post-Execution State Probe** | External effect produced unexpected state | Undeclared effect detected; automatic rollback triggered. |
| **8** | **Post-Execution Journal ACK** | Crash after commit, before ACK write | Upon retry, probe checks datastore; verifies state and writes ACK. |
| **9** | **Worker Process Crash** | Abrupt SIGKILL / `os._exit(42)` | Memory lost, but durable write-ahead journal and datastore persist. |
| **10** | **Process Restart & Retry** | Agent framework replays tool call | Logical mutation identity detected; duplicate execution suppressed. |
| **11** | **Selective Rollback Execution** | Concurrent modification / conflict | If resource was altered by downstream active mutation, refuses with `CONFLICT_DETECTED`. |

---

## Failure Window Recovery Details

### Post-Commit / Pre-ACK Process Death
If an agent worker crashes immediately after an external mutation (e.g. database `INSERT` or `UPDATE`) commits, but before the local process acknowledges completion in the journal:
1. When the agent runner restarts and retries the tool with the same `__logical_mutation_id`:
2. EvoUndo executes the registered `post_condition_probe`.
3. If the probe confirms the target state matches the expected post-condition, EvoUndo **suppresses duplicate execution** and returns the reconciled result.
4. If the probe indicates the mutation was not committed, EvoUndo permits safe initial execution.
5. Prior to committing external datastore transactions (such as SQLite), EvoUndo's drivers durably flush captured prestate witnesses and serialized recovery operations to the on-disk journal (`_persist_to_disk`), ensuring recovery programs are intact even if process death occurs immediately following `COMMIT`.
