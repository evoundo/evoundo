# SQLite Recovery Driver

EvoUndo provides native recovery support for local SQLite databases. It captures row-level pre-images and synthesizes inverse SQL DML operations.

---

## 1. Features

- **Row-Level Inverses**: Automatically reverses `INSERT` (via primary key `DELETE`), `UPDATE` (restoring pre-image values), and `DELETE` (re-inserting the previous row).
- **Zero External Dependencies**: Uses Python's built-in `sqlite3` driver.
- **Durable WAL Recording**: Journals mutation records synchronously into SQLite write-ahead logs.

---

## 2. Usage

```python
import sqlite3
from evoundo import protect_tool, revert

DB_PATH = "/tmp/agent_data.db"

# Setup table
conn = sqlite3.connect(DB_PATH)
conn.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT, active INT)")
conn.commit()

@protect_tool(
    target="sqlite:///tmp/agent_data.db/users/{user_id}",
    surface="sqlite",
    table="users",
    pk="user_id",
    db_conn_fn=lambda: sqlite3.connect(DB_PATH),
)
def update_user_email(user_id: str, email: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
    conn.commit()
    conn.close()
    return {"updated": user_id, "new_email": email}
```

---

## 3. Physical State Assertion

When `revert(mutation_id)` is invoked:
1. EvoUndo queries `users` table to check the current value.
2. Generates and executes the parameterized inverse query: `UPDATE users SET email = ? WHERE id = ?`.
3. Re-queries the table to assert that the pre-mutation email has been physically restored.
