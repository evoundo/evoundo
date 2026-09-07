# Selective Recovery & Conflict Refusal

Traditional rollback systems restore an entire disk or database snapshot. While safe for single-threaded systems, full-snapshot rollback is catastrophic for multi-agent workflows because it obliterates all independent downstream work performed by other agents.

---

## 1. Selective Recovery

EvoUndo performs **targeted, resource-scoped recovery**:

```text
Time ──>
M1 (Agent 1): updates /etc/nginx/nginx.conf  (bad change)
M2 (Agent 2): updates /var/www/index.html   (valid feature work)
M3 (Agent 3): updates /etc/redis/redis.conf  (valid cache tweak)

Revert M1 only:
  - /etc/nginx/nginx.conf is restored to pre-M1 state.
  - /var/www/index.html remains intact at M2 state.
  - /etc/redis/redis.conf remains intact at M3 state.
```

EvoUndo models mutations as nodes in a directed causal dependency graph. An inverse is applied only to the targeted resource address.

---

## 2. Same-Resource Conflict Refusal

Selective recovery is safe *only* when no subsequent active mutation has touched the same logical resource address.

### The Overwrite Problem

Suppose:
1. Agent executes `M1`: Sets `timeout = 5` (replacing `timeout = 30`).
2. Agent executes `M2`: Sets `timeout = 60` (replacing `timeout = 5`).
3. Later, an automated monitor asks to revert `M1`.

If EvoUndo blindly applied `M1`'s inverse (`timeout = 30`), it would silently overwrite `M2`'s newer configuration (`timeout = 60`), corrupting state!

### Fail-Closed Conflict Detection

EvoUndo inspects the causal graph and active mutation list before executing any revert:

```python
try:
    revert(mutation_m1_id)
except ValueError as e:
    # CONFLICT_DETECTED: Cannot selectively revert mutation 'M1'.
    # The following downstream active mutations modify the same target: 'M2'.
    # Revert downstream mutations first or resolve conflict.
```

When a conflict is detected:
- The rollback is **refused**.
- The mutation remains `ACTIVE`.
- External state is left unmodified.
- The caller or operator is instructed to revert downstream mutations in reverse causal order (`M2` then `M1`) or apply an explicit compensation.
