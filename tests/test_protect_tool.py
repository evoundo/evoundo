import pytest

from evoundo import protect_tool, revert, show_history, ToolDefinition
from evoundo.core.harness import EvoUndoHarness
from evoundo.decorator import set_default_harness


@pytest.fixture
def clean_harness(tmp_path):
    reg_file = str(tmp_path / "test_reg.json")
    harness = EvoUndoHarness(registry_path=reg_file)
    set_default_harness(harness)
    return harness


def test_protect_tool_lifecycle(clean_harness):
    state_box = {"balance": 100.0}

    @protect_tool(
        target="memory://accounts/{acc_id}/balance",
        action_class="REVERSIBLE",
        inverse_fn=lambda wit, res: state_box.update({"balance": 100.0}),
        harness=clean_harness,
    )
    def debit_tool(acc_id: str, amount: float):
        """Debit funds from a customer account."""
        state_box["balance"] -= amount
        return f"Debited {amount} from {acc_id}"

    # 1. Test to_tool_def() export without external dependencies
    tool_def = debit_tool.to_tool_def()
    assert isinstance(tool_def, ToolDefinition)
    assert tool_def.name == "debit_tool"
    assert "Debit funds" in tool_def.description
    assert tool_def.is_mutation is True

    openai_tool = tool_def.to_openai_tool()
    assert openai_tool["type"] == "function"
    assert openai_tool["function"]["name"] == "debit_tool"
    assert "acc_id" in openai_tool["function"]["parameters"]["properties"]

    # 2. Initial execution: balance 100.0 -> 90.0
    res1 = debit_tool("user_1", 10.0, __logical_mutation_id="action_req_101")
    assert "Debited 10.0" in res1
    assert state_box["balance"] == 90.0

    history = show_history()
    assert len(history) == 1
    mutation_id = history[0]["mutation_id"]
    assert history[0]["status"] == "ACTIVE"

    # 3. Crash / Retry reconciliation: duplicate execution suppressed
    res2 = debit_tool("user_1", 10.0, __logical_mutation_id="action_req_101")
    assert isinstance(res2, dict)
    assert res2.get("cached") is True
    # Balance must remain 90.0 (not double debited to 80.0)
    assert state_box["balance"] == 90.0

    # 4. Selective revert
    revert(mutation_id, reason="Customer cancelled")
    assert state_box["balance"] == 100.0

    history_after = show_history()
    assert history_after[-1]["status"] == "REVERTED"


def test_protect_tool_with_capture_fn(clean_harness, tmp_path):
    conf_file = tmp_path / "service.conf"
    conf_file.write_text("timeout=30\nworkers=4\n")

    @protect_tool(
        target="file:///tmp/service.conf",
        capture_fn=lambda *args, **kwargs: conf_file.read_text(),
        inverse_fn=lambda witness, result: conf_file.write_text(witness),
        harness=clean_harness,
    )
    def update_config(contents: str):
        conf_file.write_text(contents)
        return {"status": "updated", "path": str(conf_file)}

    # Forward
    res1 = update_config("timeout=1\nworkers=1\n", __logical_mutation_id="agent_turn_101")
    assert res1["status"] == "updated"
    assert conf_file.read_text() == "timeout=1\nworkers=1\n"

    # Replay duplicate suppression
    res2 = update_config("timeout=1\nworkers=1\n", __logical_mutation_id="agent_turn_101")
    assert res2.get("cached") is True
    assert conf_file.read_text() == "timeout=1\nworkers=1\n"

    # Revert
    history = show_history()
    mutation_id = history[-1]["mutation_id"]
    revert(mutation_id, reason="Degradation")
    assert conf_file.read_text() == "timeout=30\nworkers=4\n"

