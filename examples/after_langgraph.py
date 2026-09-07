"""EvoUndo-Protected LangGraph Agent."""

import sqlite3
from typing import TypedDict, Dict, Any
from langgraph.graph import StateGraph, START, END

# EvoUndo Integration Imports (+3 LOC)
from evoundo import EvoUndoHarness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType

# Initialize Harness (+1 LOC)
harness = EvoUndoHarness(registry_path="evoundo_reg.json")


class AgentState(TypedDict):
    account_id: str
    amount: float
    thread_id: str
    status: str
    balance: float


# Wrap tool with @harness.protect (+10 LOC)
@harness.protect(
    surface="sqlite",
    target="accounts.balance",
    declared_effects=[Effect(category=EffectCategory.RESOURCES, target="accounts.balance", op_type=EffectOpType.UPDATE)],
    capture_fn=lambda state: get_db_balance(state["account_id"]),
    inverse_fn=lambda wit, res: set_db_balance("acc_user_1", wit),
    post_condition_probe=lambda: get_db_balance("acc_user_1"),
    post_condition_validator=lambda b: True,
    framework="langgraph",
)
def withdraw_tool(state: AgentState) -> Dict[str, Any]:
    conn = sqlite3.connect("app.db")
    cur = conn.cursor()
    cur.execute("UPDATE accounts SET balance = balance - ? WHERE account_id = ?", (state["amount"], state["account_id"]))
    conn.commit()
    cur.execute("SELECT balance FROM accounts WHERE account_id = ?", (state["account_id"],))
    bal = float(cur.fetchone()[0])
    conn.close()
    return {"balance": bal, "status": "COMPLETED"}


def bank_node(state: AgentState) -> Dict[str, Any]:
    # Call protected tool execute passing contextual identifiers (+5 LOC)
    res = withdraw_tool.execute(
        state,
        __framework_run_id=state["thread_id"],
        __tool_call_id="call_withdraw_01",
        __logical_mutation_id=f"mut_lg_{state['thread_id']}_withdraw",
    )
    return {"status": "SUCCESS", "balance": res["balance"]}


def get_db_balance(account_id: str) -> float:
    conn = sqlite3.connect("app.db")
    cur = conn.cursor()
    cur.execute("SELECT balance FROM accounts WHERE account_id = ?", (account_id,))
    row = cur.fetchone()
    conn.close()
    return float(row[0]) if row else 0.0


def set_db_balance(account_id: str, balance: float) -> None:
    conn = sqlite3.connect("app.db")
    cur = conn.cursor()
    cur.execute("UPDATE accounts SET balance = ? WHERE account_id = ?", (balance, account_id))
    conn.commit()
    conn.close()


def create_agent():
    builder = StateGraph(AgentState)
    builder.add_node("bank_processor", bank_node)
    builder.add_edge(START, "bank_processor")
    builder.add_edge("bank_processor", END)
    return builder.compile()
