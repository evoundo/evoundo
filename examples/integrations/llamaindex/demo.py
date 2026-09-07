"""LlamaIndex + EvoUndo Canonical Reliability Demo."""
import sqlite3
from evoundo import EvoUndoHarness
from evoundo.integrations.llamaindex import LlamaIndexAdapter

conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE inventory (sku TEXT PRIMARY KEY, count INTEGER)")
conn.execute("INSERT INTO inventory VALUES ('laptop', 10)")
conn.commit()

def get_stock():
    cur = conn.cursor()
    cur.execute("SELECT count FROM inventory WHERE sku = 'laptop'")
    return int(cur.fetchone()[0])

def set_stock(val):
    conn.execute("UPDATE inventory SET count = ? WHERE sku = 'laptop'", (val,))
    conn.commit()

harness = EvoUndoHarness()
adapter = LlamaIndexAdapter(harness=harness)

@adapter.protect_tool(
    surface="sqlite",
    target="inventory.laptop",
    capture_fn=lambda *args: get_stock(),
    inverse_fn=lambda wit, res: set_stock(wit),
    post_condition_probe=lambda: get_stock(),
    post_condition_validator=lambda q: q == 9,
)
def llama_reserve():
    set_stock(get_stock() - 1)
    return {"status": "RESERVED", "remaining": get_stock()}

print("Initial Stock:", get_stock())
llama_reserve(__llama_session_id="sess_01", __llama_agent_id="llama_agent", __logical_mutation_id="mut_llama_01")
print("Stock after LlamaIndex tool:", get_stock())
llama_reserve(__llama_session_id="sess_01", __llama_agent_id="llama_agent", __logical_mutation_id="mut_llama_01")
print("Stock after retry:", get_stock(), "(Duplicate suppressed! ✅)")
harness.revert("mut_llama_01")
print("Stock after revert:", get_stock(), "(Restored to 10! ✅)")
