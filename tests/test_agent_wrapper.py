import pytest
from pathlib import Path

from evoundo import wrap, wrap_tool, evoundo, revert, show_history
from evoundo.core.harness import EvoUndoHarness
from evoundo.decorator import set_default_harness


@pytest.fixture
def test_harness(tmp_path):
    reg_file = str(tmp_path / "wrapper_reg.json")
    harness = EvoUndoHarness(registry_path=reg_file)
    set_default_harness(harness)
    return harness


def test_wrap_agent_with_tools_list(test_harness, tmp_path):
    conf = tmp_path / "app.conf"
    conf.write_text("workers=2\n")

    class SimpleAgent:
        def __init__(self):
            def edit_conf(path: str, data: str):
                Path(path).write_text(data)
                return "updated"
            self.tools = [edit_conf]

    raw_agent = SimpleAgent()
    agent = wrap(raw_agent)

    # Tool is wrapped
    assert getattr(agent.tools[0], "_evoundo_wrapped", False) is True

    # Run tool
    res = agent.tools[0](path=str(conf), data="workers=8\n")
    assert res == "updated"
    assert conf.read_text() == "workers=8\n"

    # Revert
    history = show_history()
    assert len(history) == 1
    mutation_id = history[0]["mutation_id"]
    revert(mutation_id)
    assert conf.read_text() == "workers=2\n"


def test_wrap_tool_list_directly(test_harness, tmp_path):
    log_file = tmp_path / "service.log"
    log_file.write_text("INIT\n")

    def append_log(file_path: str, msg: str):
        with open(file_path, "a") as f:
            f.write(msg + "\n")
        return "logged"

    tools = wrap([append_log])
    assert len(tools) == 1
    tools[0](file_path=str(log_file), msg="ERROR: db timeout")
    assert "ERROR" in log_file.read_text()

    mutation_id = show_history()[-1]["mutation_id"]
    revert(mutation_id)
    assert log_file.read_text() == "INIT\n"


def test_wrap_newly_created_file_deletes_on_revert(test_harness, tmp_path):
    new_doc = tmp_path / "new_doc.txt"
    assert not new_doc.exists()

    def create_doc(target_file: str, content: str):
        Path(target_file).write_text(content)
        return "created"

    tool = wrap(create_doc)
    tool(target_file=str(new_doc), content="temporary payload")
    assert new_doc.exists()

    mutation_id = show_history()[-1]["mutation_id"]
    revert(mutation_id)
    assert not new_doc.exists()


def test_wrap_duplicate_suppression_on_crash_retry(test_harness):
    counter = {"calls": 0}

    def charge_card(customer: str, amount: int):
        counter["calls"] += 1
        return {"status": "paid", "amount": amount}

    tool = wrap(charge_card)

    # 1. Forward run
    r1 = tool("alice", 50, __logical_mutation_id="turn_tx_101")
    assert r1["status"] == "paid"
    assert counter["calls"] == 1

    # 2. Crash retry replay
    r2 = tool("alice", 50, __logical_mutation_id="turn_tx_101")
    assert r2["cached"] is True
    assert counter["calls"] == 1  # Not executed twice!


def test_evoundo_class_decorator(test_harness, tmp_path):
    conf = tmp_path / "cluster.yaml"
    conf.write_text("nodes: 3\n")

    @evoundo
    class ClusterOpsAgent:
        def __init__(self):
            def scale(path: str, count: int):
                Path(path).write_text(f"nodes: {count}\n")
                return "scaled"
            self.tools = [scale]

    agent = ClusterOpsAgent()
    agent.tools[0](path=str(conf), count=10)
    assert conf.read_text() == "nodes: 10\n"

    mutation_id = show_history()[-1]["mutation_id"]
    revert(mutation_id)
    assert conf.read_text() == "nodes: 3\n"


def test_evoundo_function_decorator(test_harness, tmp_path):
    flag = tmp_path / "feature.flag"
    flag.write_text("OFF")

    @evoundo
    def toggle_flag(path: str, value: str):
        Path(path).write_text(value)
        return "toggled"

    toggle_flag(path=str(flag), value="ON")
    assert flag.read_text() == "ON"

    mutation_id = show_history()[-1]["mutation_id"]
    revert(mutation_id)
    assert flag.read_text() == "OFF"
