"""Google ADK + EvoUndo Canonical Reliability Demo."""

import sqlite3
from evoundo import EvoUndoHarness
from evoundo.integrations.google_adk import GoogleADKAdapter

# Setup in-memory SQLite inventory
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
adapter = GoogleADKAdapter(harness=harness)

@adapter.protect_tool(
    surface="sqlite",
    target="inventory.laptop",
    capture_fn=lambda *args: get_stock(),
    inverse_fn=lambda wit, res: set_stock(wit),
    post_condition_probe=lambda: get_stock(),
    post_condition_validator=lambda q: q == 9,
)
def adk_reserve_item():
    set_stock(get_stock() - 1)
    return {"status": "RESERVED", "stock": get_stock()}

print("--- Initial Stock ---")
print("Inventory:", get_stock())  # 10

print("\n--- Google ADK Agent Runs ---")
res1 = adk_reserve_item(
    __adk_session_id="adk_sess_01",
    __adk_agent_id="adk_agent",
    __logical_mutation_id="mut_adk_001",
)
print("Stock after reserve:", get_stock())  # 9

print("\n--- Worker Crashes & ADK Framework Retries ---")
res2 = adk_reserve_item(
    __adk_session_id="adk_sess_01",
    __adk_agent_id="adk_agent",
    __logical_mutation_id="mut_adk_001",
)
print("Stock after retry:", get_stock(), "(Duplicate deduction suppressed! ✅)")  # 9

print("\n--- Selective Rollback ---")
harness.revert("mut_adk_001")
print("Stock after rollback:", get_stock(), "(Restored to 10! ✅)")
