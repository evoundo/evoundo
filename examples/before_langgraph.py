"""Standard LangGraph Agent (Without EvoUndo Protection)."""

import sqlite3
from typing import TypedDict, Dict, Any
from langgraph.graph import StateGraph, START, END


class AgentState(TypedDict):
    account_id: str
    amount: float
    thread_id: str
    status: str
    balance: float


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
    res = withdraw_tool(state)
    return {"status": "SUCCESS", "balance": res["balance"]}


def create_agent():
    builder = StateGraph(AgentState)
    builder.add_node("bank_processor", bank_node)
    builder.add_edge(START, "bank_processor")
    builder.add_edge("bank_processor", END)
    return builder.compile()
