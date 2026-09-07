"""Model Context Protocol (MCP) + EvoUndo Canonical Reliability Demo."""

import sqlite3
from evoundo import EvoUndoHarness
from evoundo.integrations.mcp_proxy import EvoUndoMCPMiddleware

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

def debit_handler(sku: str, count: int = 1):
    set_stock(get_stock() - count)
    return {"stock": get_stock()}

harness = EvoUndoHarness()
mcp = EvoUndoMCPMiddleware(harness=harness)

print("Initial Stock:", get_stock())
# MCP Request 1
resp1 = mcp.handle_tool_call(
    tool_name="reserve_inventory",
    tool_arguments={"sku": "laptop", "count": 1},
    handler_fn=debit_handler,
    client_id="claude_desktop",
    logical_mutation_id="mut_mcp_001",
    capture_fn=lambda sku, count: get_stock(),
    inverse_fn=lambda wit, res: set_stock(wit),
    post_condition_probe=lambda: get_stock(),
    post_condition_validator=lambda q: q == 9,
)
print("Stock after MCP tool call:", get_stock())

# MCP Request 2 (Retry after client disconnect)
resp2 = mcp.handle_tool_call(
    tool_name="reserve_inventory",
    tool_arguments={"sku": "laptop", "count": 1},
    handler_fn=debit_handler,
    client_id="claude_desktop",
    logical_mutation_id="mut_mcp_001",
    capture_fn=lambda sku, count: get_stock(),
    inverse_fn=lambda wit, res: set_stock(wit),
    post_condition_probe=lambda: get_stock(),
    post_condition_validator=lambda q: q == 9,
)
print("Stock after MCP client retry:", get_stock(), "(Duplicate suppressed! ✅)")

# Revert
harness.revert("mut_mcp_001")
print("Stock after revert:", get_stock(), "(Restored to 10! ✅)")
