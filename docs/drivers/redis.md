# Redis Recovery Driver

The Redis driver provides granular, type-aware recovery across Redis 7+ data structures.

---

## 1. Installation

```bash
pip install "evoundo[redis]"
```

---

## 2. Granular Data Structure Support

EvoUndo does not rely on blunt whole-key `DUMP`/`RESTORE` where granular alternatives exist:

- **Strings & Values**: Pre-state capture with atomic `SET` rollback.
- **Hashes (`HSET`/`HDEL`)**: Field-level tracking. If Mutation 1 updates field `x` and Mutation 2 updates field `y`, reverting Mutation 1 cleanly restores field `x` without touching field `y`.
- **Counters (`INCRBY`/`DECRBY`)**: Algebraic delta compensation. If an agent adds +10 to a counter, rollback applies -10 *provided* no non-commutative operation (`SET`, `DEL`) intervened.
- **Lists (`LPUSH`/`RPUSH`)**: Element-aware rollback. Reverts the exact element without removing identical duplicate elements.
- **TTL / Expiration**: Captures absolute expiration deadlines (`PEXPIREAT`). If the deadline expired while waiting to recover, the key is not artificially resurrected.

---

## 3. Example

```python
import redis
from evoundo import protect_tool, revert

r = redis.Redis(host="127.0.0.1", port=6379, db=0)

@protect_tool(
    target="redis://tokens/{session_id}",
    surface="redis",
)
def set_session_token(session_id: str, token: str):
    r.set(f"token:{session_id}", token)
    return {"session": session_id}
```
