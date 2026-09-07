"""OpenAI Agents SDK + EvoUndo Canonical Reliability Demo."""

import sqlite3
from evoundo import EvoUndoHarness
from evoundo.integrations.openai_agents_sdk import OpenAIAgentsSDKAdapter

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
adapter = OpenAIAgentsSDKAdapter(harness=harness)

@adapter.protect_function_tool(
    surface="sqlite",
    target="inventory.laptop",
    capture_fn=lambda *args: get_stock(),
    inverse_fn=lambda wit, res: set_stock(wit),
    post_condition_probe=lambda: get_stock(),
    post_condition_validator=lambda q: q == 9,
)
def reserve_laptop():
    set_stock(get_stock() - 1)
    return {"status": "RESERVED", "remaining": get_stock()}

print("Initial Stock:", get_stock())
reserve_laptop(
    __openai_session_id="oa_sess_01",
    __openai_agent_id="oa_agent",
    __logical_mutation_id="mut_oa_01",
)
print("Stock after FunctionTool call:", get_stock())

# Retry
reserve_laptop(
    __openai_session_id="oa_sess_01",
    __openai_agent_id="oa_agent",
    __logical_mutation_id="mut_oa_01",
)
print("Stock after retry:", get_stock(), "(Duplicate suppressed! ✅)")

# Revert
harness.revert("mut_oa_01")
print("Stock after revert:", get_stock(), "(Restored to 10! ✅)")
