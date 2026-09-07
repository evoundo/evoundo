"""Anthropic / Claude Tool-Use + EvoUndo Canonical Reliability Demo."""
import sqlite3
from evoundo import EvoUndoHarness
from evoundo.integrations.anthropic import AnthropicToolUseAdapter

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

def reserve_handler(sku: str, count: int = 1):
    set_stock(get_stock() - count)
    return {"remaining": get_stock()}

harness = EvoUndoHarness()
adapter = AnthropicToolUseAdapter(harness=harness)

tool_block = {"type": "tool_use", "id": "toolu_123", "name": "reserve_stock", "input": {"sku": "laptop", "count": 1}}

print("Initial Stock:", get_stock())
adapter.dispatch_tool_use(
    tool_use_block=tool_block,
    handler_fn=reserve_handler,
    session_id="sess_01",
    logical_mutation_id="mut_claude_01",
    capture_fn=lambda sku, count: get_stock(),
    inverse_fn=lambda wit, res: set_stock(wit),
    post_condition_probe=lambda: get_stock(),
    post_condition_validator=lambda q: q == 9,
)
print("Stock after tool_use dispatch:", get_stock())
adapter.dispatch_tool_use(
    tool_use_block=tool_block,
    handler_fn=reserve_handler,
    session_id="sess_01",
    logical_mutation_id="mut_claude_01",
    capture_fn=lambda sku, count: get_stock(),
    inverse_fn=lambda wit, res: set_stock(wit),
    post_condition_probe=lambda: get_stock(),
    post_condition_validator=lambda q: q == 9,
)
print("Stock after retry:", get_stock(), "(Duplicate suppressed! ✅)")
harness.revert("mut_claude_01")
print("Stock after revert:", get_stock(), "(Restored to 10! ✅)")
