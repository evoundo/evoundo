# PostgreSQL Recovery Driver

The PostgreSQL recovery driver supports PostgreSQL 16+ relational databases, handling primary key identification, parameterized inverses, and schema qualification.

---

## 1. Installation

```bash
pip install "evoundo[orm]"
```

Requires `psycopg2-binary` or `asyncpg`.

---

## 2. Usage

```python
import psycopg2
from evoundo import protect_tool, revert

DSN = "postgresql://postgres:password@localhost:5432/production"

@protect_tool(
    target="postgres://public/accounts/{account_id}",
    surface="postgres",
    table="accounts",
    pk="account_id",
    db_conn_fn=lambda: psycopg2.connect(DSN),
)
def suspend_account(account_id: str, reason: str):
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("UPDATE accounts SET status = 'SUSPENDED' WHERE id = %s", (account_id,))
    conn.commit()
    conn.close()
    return {"account_id": account_id, "status": "SUSPENDED"}
```

---

## 3. SQL Safety Invariants

- **Safe Quoting**: All table and column identifiers are validated against strict alphanumeric schema rules and quoted with dialect-safe quotes.
- **Parameterized Execution**: Inverses never interpolate user strings into queries; values are bound strictly through DBAPI parameters.
- **Conflict Detection**: If a subsequent transaction modified the same row, the recovery engine raises `CONFLICT_DETECTED` rather than overwriting newer data.
