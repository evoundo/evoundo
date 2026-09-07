import pytest
from evoundo import protect_tool, wrap_tool, revert, show_history
from evoundo.core.harness import EvoUndoHarness
from evoundo.decorator import set_default_harness
from evoundo.recovery.operations import (
    CustomRecoveryOp,
    RecoveryProgram,
    RecoveryVerificationError,
)
from evoundo.witness import Witness, WitnessCaptureError
from evoundo.reconciliation.reconciler import (
    FailureStage,
    MutationLifecycleState,
    MutationReconciler,
)


@pytest.fixture
def clean_harness(tmp_path):
    reg_file = str(tmp_path / "test_reg.json")
    journal_file = str(tmp_path / "test_journal.json")
    reconciler = MutationReconciler(journal_path=journal_file)
    harness = EvoUndoHarness(registry_path=reg_file, reconciler=reconciler)
    set_default_harness(harness)
    return harness


def test_custom_recovery_op_rejects_missing_inverse_on_reload(clean_harness):
    """P1-1: CustomRecoveryOp must reject recovery if inverse_fn is lost on restart,

    and mutation status must remain ACTIVE instead of falsely transitioning to REVERTED.
    """
    # 1. Simulate CustomRecoveryOp serialized and reloaded (losing in-memory lambda)
    original_op = CustomRecoveryOp(
        name="TestOp",
        inverse_fn=lambda s, w: None,
    )
    serialized = original_op.to_dict()
    reloaded_op = CustomRecoveryOp.from_dict(serialized)

    assert reloaded_op.inverse_fn is None
    # Verify verify() returns False when inverse_fn is missing
    dummy_state = clean_harness.current_state
    dummy_witness = Witness(mutation_id="m_test", data={})
    assert reloaded_op.verify(dummy_state, dummy_witness) is False

    # Verify apply() raises RecoveryVerificationError
    with pytest.raises(RecoveryVerificationError) as excinfo:
        reloaded_op.apply(dummy_state, dummy_witness)
    assert "inverse_fn" in str(excinfo.value)

    # 2. End-to-end harness test: status must stay ACTIVE, not REVERTED
    state_box = {"val": 20}
    m_id = "mut_test_reload_safety"
    clean_harness.record_external_protected_mutation(
        mutation_id=m_id,
        description="Mutation with reloaded op",
        witness=dummy_witness,
        recovery_program=RecoveryProgram(operations=[reloaded_op]),
    )

    rec = clean_harness.mutation_registry.inspect_mutation(m_id)
    assert rec.status == "ACTIVE"

    # Attempting to revert must fail
    with pytest.raises(RecoveryVerificationError):
        clean_harness.revert(m_id)

    # Value must remain untouched (20) and status must remain ACTIVE
    assert state_box["val"] == 20
    rec_after = clean_harness.mutation_registry.inspect_mutation(m_id)
    assert rec_after.status == "ACTIVE"


def test_failed_witness_capture_blocks_mutation_declarative(clean_harness):
    """P1-2: Failed witness capture in protect_tool must raise WitnessCaptureError

    and fail closed (block mutation execution).
    """
    state_box = {"val": 10}

    def failing_capture(*args, **kwargs):
        raise ConnectionError("Witness capture datastore unreachable")

    @protect_tool(
        target="memory://state_box/val",
        capture_fn=failing_capture,
        inverse_fn=lambda wit, res: state_box.update({"val": wit}),
        harness=clean_harness,
    )
    def mutate_state(new_val: int):
        state_box["val"] = new_val
        return new_val

    # Mutation must fail closed with WitnessCaptureError
    with pytest.raises(WitnessCaptureError) as excinfo:
        mutate_state(20, __logical_mutation_id="log_fail_capture_1")

    assert "failing_capture" in str(excinfo.value) or "Witness capture" in str(excinfo.value)
    # Critical invariant: forward execution did NOT run, value remains 10
    assert state_box["val"] == 10

    # Ensure no active mutation was registered in the harness
    assert len(clean_harness.mutation_registry.list_mutations()) == 0


def test_failed_witness_capture_blocks_mutation_wrapper(clean_harness):
    """P1-2: Failed witness capture in wrap_tool must raise WitnessCaptureError

    and fail closed.
    """
    state_box = {"val": 100}

    def failing_capture(*args, **kwargs):
        raise ValueError("Cannot read pre-state")

    def raw_mutation(v: int):
        state_box["val"] = v
        return v

    wrapped = wrap_tool(
        raw_mutation,
        target="memory://state_box/val",
        capture_fn=failing_capture,
        inverse_fn=lambda wit, res: state_box.update({"val": wit}),
        harness=clean_harness,
    )

    with pytest.raises(WitnessCaptureError):
        wrapped(200, __logical_mutation_id="log_wrap_capture_fail")

    assert state_box["val"] == 100


def test_declarative_tool_initializes_journal_entry(clean_harness):
    """P1-3: Declarative tools must initialize reconciliation journal entries,

    ensuring retries across crash boundaries are tracked and deduplicated.
    """
    call_count = [0]

    @protect_tool(
        target="memory://counter",
        harness=clean_harness,
    )
    def do_work(task_id: str):
        call_count[0] += 1
        return f"done_{task_id}"

    logical_id = "task_journal_init_test"

    # 1. First execution
    res1 = do_work("42", __logical_mutation_id=logical_id)
    assert res1 == "done_42"
    assert call_count[0] == 1

    # 2. Verify journal entry exists and is committed in the reconciler
    entry = clean_harness.reconciler.get_entry(logical_id)
    assert entry is not None, f"Journal entry for {logical_id} was never initialized!"
    assert entry.status in ("COMMITTED", "MUTATION_COMPLETED")
    assert entry.result_data == "done_42"

    # 3. Simulate crash between external write and subsequent agent step:
    # A retry with the same logical mutation ID must be suppressed by the reconciler
    res2 = do_work("42", __logical_mutation_id=logical_id)
    assert call_count[0] == 1, "Underlying function was re-executed instead of being suppressed!"
    assert res2 == "done_42" or (isinstance(res2, dict) and res2.get("cached") is True)


def test_crash_before_registration_suppresses_duplicate_write(clean_harness, monkeypatch):
    """P1: Crash retries must not duplicate writes when crash occurs after external

    mutation write but before registry registration.
    """
    state_box = {"balance": 100.0}

    @protect_tool(
        target="memory://accounts/user_1/balance",
        inverse_fn=lambda wit, res: state_box.update({"balance": 100.0}),
        harness=clean_harness,
    )
    def debit_account(amount: float):
        state_box["balance"] -= amount
        return f"Debited {amount}"

    tx_id = "tx_crash_before_reg_99"

    # Simulate worker crash right after external write (in record_external_protected_mutation)
    orig_record = clean_harness.record_external_protected_mutation

    def crashing_record(*args, **kwargs):
        raise RuntimeError("Worker process died before registry registration!")

    clean_harness.record_external_protected_mutation = crashing_record

    # 1. First execution: external write happens, but crash occurs before registry registration
    with pytest.raises(RuntimeError, match="Worker process died before registry registration"):
        debit_account(10.0, __logical_mutation_id=tx_id)

    # Physical state was modified (100 -> 90)
    assert state_box["balance"] == 90.0
    # Journal must already be recorded as completed despite the crash before registration
    journal_entry = clean_harness.reconciler.get_entry(tx_id)
    assert journal_entry is not None
    assert journal_entry.status in ("MUTATION_COMPLETED", "COMMITTED")

    # 2. Worker recovers / retry arrives with same logical ID:
    clean_harness.record_external_protected_mutation = orig_record

    res_retry = debit_account(10.0, __logical_mutation_id=tx_id)

    # CRITICAL: balance must remain 90.0 (NOT double-debited 100 -> 90 -> 80!)
    assert state_box["balance"] == 90.0, f"Duplicate write occurred! Balance was {state_box['balance']} instead of 90.0"
    assert isinstance(res_retry, dict)
    assert res_retry.get("cached") is True

    # Mutation is recovered into registry and can be reverted
    clean_harness.revert(f"mut_{tx_id}")
    assert state_box["balance"] == 100.0


def test_undo_clears_stale_journal_and_repeating_call_reexecutes(clean_harness):
    """P1: Reverting a mutation must update the journal so repeating the call

    executes cleanly and does not return stale cached success.
    """
    state_box = {"balance": 100.0}

    @protect_tool(
        target="memory://accounts/user_1/balance",
        inverse_fn=lambda wit, res: state_box.update({"balance": 100.0}),
        harness=clean_harness,
    )
    def debit_account(amount: float):
        state_box["balance"] -= amount
        return f"Debited {amount}"

    tx_id = "tx_undo_repeat_42"

    # 1. Initial call: 100.0 -> 90.0
    res1 = debit_account(10.0, __logical_mutation_id=tx_id)
    assert "Debited 10.0" in res1
    assert state_box["balance"] == 90.0

    # 2. Undo the mutation
    clean_harness.revert(f"mut_{tx_id}", reason="User refund")
    # Actual balance is restored to 100.0
    assert state_box["balance"] == 100.0

    # Journal must show REVERTED, not stale COMMITTED
    entry = clean_harness.reconciler.get_entry(tx_id)
    assert entry is not None
    assert entry.status == "REVERTED"

    # 3. Repeat the call with the same logical mutation ID:
    # It must NOT return cached success with stale 90 when balance is 100!
    res2 = debit_account(10.0, __logical_mutation_id=tx_id)

    # Function must execute, debiting 100.0 -> 90.0
    assert "Debited 10.0" in res2
    assert state_box["balance"] == 90.0


def test_wrap_tool_retry_recovery_avoids_unbound_local_error(clean_harness):
    """P1: wrap_tool must not raise UnboundLocalError when reconstructing

    after a crash before harness registration.
    """
    state_box = {"val": 10}

    def update_val(new_val: int):
        state_box["val"] = new_val
        return {"val": new_val}

    wrapped = wrap_tool(
        update_val,
        target="memory://state/val",
        capture_fn=lambda *a, **k: state_box["val"],
        inverse_fn=lambda wit, res: state_box.update({"val": wit}),
        harness=clean_harness,
    )

    log_id = "tx_wrap_retry_scope_1"

    # Simulate crash before registry registration
    orig_record = clean_harness.record_external_protected_mutation

    def crashing_record(*args, **kwargs):
        raise RuntimeError("Crash before registration")

    clean_harness.record_external_protected_mutation = crashing_record

    with pytest.raises(RuntimeError, match="Crash before registration"):
        wrapped(20, __logical_mutation_id=log_id)

    assert state_box["val"] == 20

    # Worker recovers / retry executed
    clean_harness.record_external_protected_mutation = orig_record

    # This retry MUST NOT raise UnboundLocalError for eff_inverse or res_addr
    res = wrapped(20, __logical_mutation_id=log_id)
    assert res.get("cached") is True

    # Mutation must be registered and revertible
    clean_harness.revert(f"mut_{log_id}")
    assert state_box["val"] == 10


def test_driver_recovery_preserved_during_reconstruction(clean_harness, tmp_path):
    """P1: Driver recovery operations (e.g. JSONConfigDriver) must be preserved in the

    reconciler journal and reconstructed on retry, ensuring revert physically restores state
    rather than falsely reporting REVERTED.
    """
    import json
    from evoundo.drivers.json_config import json_config

    config_file = str(tmp_path / "app_config.json")
    with open(config_file, "w") as f:
        json.dump({"service": {"env": {"PORT": "2"}}}, f)

    @protect_tool(
        target=f"config://{config_file}/service/env/PORT",
        harness=clean_harness,
    )
    def update_config(new_port: int):
        with json_config(config_file) as cfg:
            cfg.set_env("PORT", new_port)
        return f"Port set to {new_port}"

    log_id = "tx_driver_recon_1"

    # 1. First execution: inject crash right before registry registration
    orig_record = clean_harness.record_external_protected_mutation

    def crashing_record(*args, **kwargs):
        raise RuntimeError("Crash before registry registration")

    clean_harness.record_external_protected_mutation = crashing_record

    with pytest.raises(RuntimeError, match="Crash before registry registration"):
        update_config(999, __logical_mutation_id=log_id)

    # Physical file is now 999
    with open(config_file) as f:
        data = json.load(f)
    assert data["service"]["env"]["PORT"] == "999"

    # 2. Worker restarts / retry arrives
    clean_harness.record_external_protected_mutation = orig_record
    res = update_config(999, __logical_mutation_id=log_id)
    assert res.get("cached") is True

    # 3. Revert must execute reconstructed DriverRecoveryOp and restore PORT to 2
    clean_harness.revert(f"mut_{log_id}")

    with open(config_file) as f:
        reverted_data = json.load(f)

    # CRITICAL: Setting must be physically restored to "2", NOT remain 999!
    assert reverted_data["service"]["env"]["PORT"] == "2", (
        f"Driver recovery was dropped! Expected '2', got '{reverted_data['service']['env']['PORT']}'"
    )

    rec = clean_harness.mutation_registry.inspect_mutation(f"mut_{log_id}")
    assert rec.status == "REVERTED"


def test_ambiguous_crash_retry_refuses_duplicate_write(clean_harness):
    """P1: A crash in-flight after external write but before completion leaves status STARTED.

    Retrying the ambiguous call must be refused with CONFLICT_DETECTED rather than
    duplicate execution (100 -> 90 -> 80).
    """
    state_box = {"balance": 100.0}

    @protect_tool(
        target="memory://accounts/user_1/balance",
        harness=clean_harness,
    )
    def debit_account_crashing(amount: float):
        state_box["balance"] -= amount
        # Crash in-flight after write, before function returns or journal completion is recorded
        raise SystemExit("Process killed in-flight")

    log_id = "tx_ambiguous_crash_1"

    with pytest.raises(SystemExit, match="Process killed in-flight"):
        debit_account_crashing(10.0, __logical_mutation_id=log_id)

    # Physical state was modified to 90.0
    assert state_box["balance"] == 90.0

    # Journal entry was created and remains STARTED
    entry = clean_harness.reconciler.get_entry(log_id)
    assert entry is not None
    assert entry.status == "STARTED"

    # Retry arrives on recovered worker without probe
    @protect_tool(
        target="memory://accounts/user_1/balance",
        harness=clean_harness,
    )
    def debit_account_retry(amount: float):
        state_box["balance"] -= amount
        return f"Debited {amount}"

    # Ambiguous retry MUST be refused with CONFLICT_DETECTED
    with pytest.raises(RuntimeError) as excinfo:
        debit_account_retry(10.0, __logical_mutation_id=log_id)

    assert "CONFLICT_DETECTED" in str(excinfo.value)

    # CRITICAL: Balance must remain 90.0, NOT double-debited to 80.0!
    assert state_box["balance"] == 90.0, (
        f"Duplicate execution occurred! Balance was {state_box['balance']} instead of 90.0"
    )


def test_ambiguous_crash_retry_with_probe_suppresses_duplicate(clean_harness):
    """P1: When an ambiguous in-flight mutation is retried with an external state probe

    confirming the write already succeeded, duplicate execution is cleanly suppressed.
    """
    state_box = {"balance": 100.0}

    @protect_tool(
        target="memory://accounts/user_1/balance",
        harness=clean_harness,
    )
    def debit_account(amount: float):
        state_box["balance"] -= amount
        raise SystemExit("Process killed in-flight")

    log_id = "tx_ambiguous_probe_1"

    with pytest.raises(SystemExit):
        debit_account(10.0, __logical_mutation_id=log_id)

    assert state_box["balance"] == 90.0

    # Caller provides a probe verifying the post-condition (balance == 90.0)
    @protect_tool(
        target="memory://accounts/user_1/balance",
        post_condition_probe=lambda: state_box["balance"],
        post_condition_validator=lambda probed: probed == 90.0,
        harness=clean_harness,
    )
    def debit_account_with_probe(amount: float):
        state_box["balance"] -= amount
        return f"Debited {amount}"

    res = debit_account_with_probe(10.0, __logical_mutation_id=log_id)
    # Duplicate is suppressed cleanly
    assert state_box["balance"] == 90.0
    assert res.get("cached") is True or res.get("reconciled") is True


def test_wrap_tool_isolates_driver_recovery_between_calls(clean_harness, tmp_path):
    """P1: wrap_tool must isolate driver recovery operations between calls.

    Updating files A and B in sequence, then reverting B, must only revert B
    and leave A untouched (does not leak ops across calls via shared context).
    """
    import json
    from evoundo.drivers.json_config import json_config

    file_a = tmp_path / "config_a.json"
    file_b = tmp_path / "config_b.json"

    file_a.write_text(json.dumps({"service": {"env": {"PORT": "1"}}}))
    file_b.write_text(json.dumps({"service": {"env": {"PORT": "2"}}}))

    def update_config(path: str, port: str):
        with json_config(path) as cfg:
            cfg.set_env("PORT", port)
        return f"Updated {path} to {port}"

    wrapped_update = wrap_tool(
        update_config,
        harness=clean_harness,
    )

    # 1. Update file A: PORT 1 -> 10
    res_a = wrapped_update(path=str(file_a), port="10", __logical_mutation_id="mut_update_a")
    assert json.loads(file_a.read_text())["service"]["env"]["PORT"] == "10"

    # 2. Update file B: PORT 2 -> 20
    res_b = wrapped_update(path=str(file_b), port="20", __logical_mutation_id="mut_update_b")
    assert json.loads(file_b.read_text())["service"]["env"]["PORT"] == "20"

    # 3. Revert mutation B
    clean_harness.revert("mut_update_b")

    # File B must be reverted back to 2
    assert json.loads(file_b.read_text())["service"]["env"]["PORT"] == "2"

    # CRITICAL: File A must strictly remain 10, NOT reverted back to 1!
    assert json.loads(file_a.read_text())["service"]["env"]["PORT"] == "10", (
        f"Context leak! Undoing B also reverted file A! File A was {json.loads(file_a.read_text())['service']['env']['PORT']}"
    )


def test_timeout_retry_refuses_duplicate_committed_write(clean_harness):
    """P1: Timeout retries must not duplicate committed writes.

    When a write succeeds but response times out, recording FAILED in the journal
    must not permit an unverified retry to debit again (100 -> 90 -> 80).
    Ambiguous retries must be refused with CONFLICT_DETECTED unless verified by probe.
    """
    state_box = {"balance": 100.0}

    @protect_tool(
        target="memory://accounts/user_timeout/balance",
        harness=clean_harness,
    )
    def debit_account(amount: float):
        state_box["balance"] -= amount
        raise TimeoutError("Read timed out waiting for gateway response")

    log_id = "tx_timeout_retry_1"

    # 1. First execution: write succeeds (100 -> 90) but timeout exception is raised
    with pytest.raises(TimeoutError, match="Read timed out"):
        debit_account(10.0, __logical_mutation_id=log_id)

    # Physical state was debited to 90.0
    assert state_box["balance"] == 90.0

    # Journal recorded failure
    entry = clean_harness.reconciler.get_entry(log_id)
    assert entry is not None
    assert entry.status == "FAILED"

    # 2. Retry arrives on client timeout without a probe
    @protect_tool(
        target="memory://accounts/user_timeout/balance",
        harness=clean_harness,
    )
    def debit_account_retry(amount: float):
        state_box["balance"] -= amount
        return f"Debited {amount}"

    with pytest.raises(RuntimeError) as excinfo:
        debit_account_retry(10.0, __logical_mutation_id=log_id)

    assert "CONFLICT_DETECTED" in str(excinfo.value)
    # CRITICAL: Balance must remain 90.0, NOT double-debited to 80.0!
    assert state_box["balance"] == 90.0, (
        f"Duplicate write occurred on timeout retry! Balance was {state_box['balance']} instead of 90.0"
    )

    # 3. Retry with post-condition probe confirming write succeeded suppresses duplicate
    @protect_tool(
        target="memory://accounts/user_timeout/balance",
        post_condition_probe=lambda: state_box["balance"],
        post_condition_validator=lambda probed: probed == 90.0,
        harness=clean_harness,
    )
    def debit_account_with_probe(amount: float):
        state_box["balance"] -= amount
        return f"Debited {amount}"

    res = debit_account_with_probe(10.0, __logical_mutation_id=log_id)
    assert state_box["balance"] == 90.0
    assert res.get("cached") is True or res.get("reconciled") is True


def test_undo_reexecute_crash_retry_suppresses_duplicate_write_declarative(clean_harness):
    """P1: Undo -> re-execute -> crash before registration must NOT duplicate write on retry.

    The earlier REVERTED status in the registry must not overwrite the newer
    MUTATION_COMPLETED status in the reconciler journal.
    Balance must remain 90.0, NOT double-debited to 80.0!
    """
    state_box = {"balance": 100.0}

    @protect_tool(
        target="memory://accounts/user_undo_replay/balance",
        inverse_fn=lambda wit, res: state_box.update({"balance": wit}),
        capture_fn=lambda *a, **k: state_box["balance"],
        harness=clean_harness,
    )
    def debit_account(amount: float):
        state_box["balance"] -= amount
        return f"Debited {amount}"

    tx_id = "tx_undo_replay_crash_decl_1"

    # 1. Call 1: debit 100 -> 90
    debit_account(10.0, __logical_mutation_id=tx_id)
    assert state_box["balance"] == 90.0
    assert clean_harness.mutation_registry.inspect_mutation(f"mut_{tx_id}").status == "ACTIVE"

    # 2. Revert Call 1: balance 90 -> 100
    clean_harness.revert(f"mut_{tx_id}", reason="Customer refund")
    assert state_box["balance"] == 100.0
    assert clean_harness.mutation_registry.inspect_mutation(f"mut_{tx_id}").status == "REVERTED"
    assert clean_harness.reconciler.get_entry(tx_id).status == "REVERTED"

    # 3. Call 2 (Replay / Re-execution after undo): debits 100 -> 90, but crashes before registration
    orig_record = clean_harness.record_external_protected_mutation

    def crashing_record(*args, **kwargs):
        raise RuntimeError("Crash before registry registration on re-execute")

    clean_harness.record_external_protected_mutation = crashing_record

    with pytest.raises(RuntimeError, match="Crash before registry registration on re-execute"):
        debit_account(10.0, __logical_mutation_id=tx_id)

    # Physical write succeeded: 90.0
    assert state_box["balance"] == 90.0
    # Journal shows MUTATION_COMPLETED
    assert clean_harness.reconciler.get_entry(tx_id).status == "MUTATION_COMPLETED"
    # Registry retains the earlier REVERTED status
    assert clean_harness.mutation_registry.inspect_mutation(f"mut_{tx_id}").status == "REVERTED"

    # Restore normal record function for recovered worker
    clean_harness.record_external_protected_mutation = orig_record

    # 4. Retry arrives on recovered worker
    # Earlier REVERTED in registry must NOT overwrite MUTATION_COMPLETED in journal!
    res = debit_account(10.0, __logical_mutation_id=tx_id)

    # CRITICAL: Balance must strictly remain 90.0, NOT double-debited to 80.0!
    assert state_box["balance"] == 90.0, (
        f"Duplicate write occurred on re-execute retry! Balance was {state_box['balance']} instead of 90.0"
    )
    assert res.get("cached") is True

    # Mutation registry must have been reconstructed to ACTIVE
    rec = clean_harness.mutation_registry.inspect_mutation(f"mut_{tx_id}")
    assert rec.status == "ACTIVE"

    # 5. Undo of re-executed mutation must cleanly restore balance to 100.0
    clean_harness.revert(f"mut_{tx_id}")
    assert state_box["balance"] == 100.0


def test_undo_reexecute_crash_retry_suppresses_duplicate_write_wrapper(clean_harness):
    """P1: wrap_tool Undo -> re-execute -> crash before registration must NOT duplicate write on retry.

    Balance must remain 90.0, NOT double-debited to 80.0!
    """
    state_box = {"balance": 100.0}

    def debit_func(amount: float):
        state_box["balance"] -= amount
        return {"balance": state_box["balance"]}

    wrapped = wrap_tool(
        debit_func,
        target="memory://accounts/user_undo_replay_wrap/balance",
        capture_fn=lambda *a, **k: state_box["balance"],
        inverse_fn=lambda wit, res: state_box.update({"balance": wit}),
        harness=clean_harness,
    )

    tx_id = "tx_undo_replay_crash_wrap_1"

    # 1. Call 1: debit 100 -> 90
    wrapped(10.0, __logical_mutation_id=tx_id)
    assert state_box["balance"] == 90.0

    # 2. Revert Call 1: balance 90 -> 100
    clean_harness.revert(f"mut_{tx_id}", reason="Customer refund")
    assert state_box["balance"] == 100.0

    # 3. Call 2 (Replay / Re-execution after undo): debits 100 -> 90, crashes before registration
    orig_record = clean_harness.record_external_protected_mutation

    def crashing_record(*args, **kwargs):
        raise RuntimeError("Crash before registry registration on re-execute")

    clean_harness.record_external_protected_mutation = crashing_record

    with pytest.raises(RuntimeError, match="Crash before registry registration on re-execute"):
        wrapped(10.0, __logical_mutation_id=tx_id)

    assert state_box["balance"] == 90.0
    assert clean_harness.reconciler.get_entry(tx_id).status == "MUTATION_COMPLETED"
    assert clean_harness.mutation_registry.inspect_mutation(f"mut_{tx_id}").status == "REVERTED"

    clean_harness.record_external_protected_mutation = orig_record

    # 4. Retry on recovered worker: duplicate suppressed!
    res = wrapped(10.0, __logical_mutation_id=tx_id)
    assert state_box["balance"] == 90.0, (
        f"Duplicate write occurred on re-execute retry in wrap_tool! Balance was {state_box['balance']} instead of 90.0"
    )
    assert res.get("cached") is True

    # 5. Undo Call 2 cleanly restores balance to 100.0
    clean_harness.revert(f"mut_{tx_id}")
    assert state_box["balance"] == 100.0


def test_failed_witness_capture_permits_safe_retry(clean_harness):
    """P2: Temporary witness capture failure must not block subsequent safe retries.

    Because capture failure occurs before forward tool execution, zero writes
    occurred, and retry must be permitted once capture recovers.
    """
    state_box = {"val": 10}
    capture_healthy = False

    def flaky_capture(*args, **kwargs):
        if not capture_healthy:
            raise ConnectionError("Witness capture datastore temporarily down")
        return state_box["val"]

    @protect_tool(
        target="memory://state_box/flaky_val",
        capture_fn=flaky_capture,
        inverse_fn=lambda wit, res: state_box.update({"val": wit}),
        harness=clean_harness,
    )
    def update_val(new_val: int):
        state_box["val"] = new_val
        return new_val

    tx_id = "tx_flaky_capture_retry_1"

    # 1. Attempt 1: Capture fails -> WitnessCaptureError raised
    with pytest.raises(WitnessCaptureError):
        update_val(20, __logical_mutation_id=tx_id)

    # State untouched
    assert state_box["val"] == 10

    # Journal recorded FAILED with CAPTURE failure stage
    entry = clean_harness.reconciler.get_entry(tx_id)
    assert entry is not None
    assert entry.status == MutationLifecycleState.FAILED.value
    assert entry.failure_stage == FailureStage.CAPTURE.value

    # 2. Datastore recovers
    capture_healthy = True

    # 3. Retry arrives with same logical ID: MUST succeed, NOT be refused with CONFLICT_DETECTED
    res = update_val(20, __logical_mutation_id=tx_id)
    assert res == 20
    assert state_box["val"] == 20

    # Journal and registry now show COMMITTED / ACTIVE
    assert clean_harness.reconciler.get_entry(tx_id).status == "COMMITTED"
    assert clean_harness.mutation_registry.inspect_mutation(f"mut_{tx_id}").status == "ACTIVE"

    # 4. Revert works cleanly
    clean_harness.revert(f"mut_{tx_id}")
    assert state_box["val"] == 10


def test_combined_mutation_lifecycle_and_retry_safety(clean_harness, tmp_path):
    """Adversarial end-to-end verification of the centralized reconciler lifecycle:

    1. Pre-execution witness capture failure -> FAILED with FailureStage.CAPTURE, zero writes.
    2. Retry attempt transitions journal to STARTED before execution.
    3. Worker crash mid-execution in STARTED without post-condition proof -> fails closed (CONFLICT_DETECTED).
    4. Successful safe execution -> debits balance, COMMITTED in journal, ACTIVE in registry (epoch 1).
    5. Timeout retry -> returns cached result, duplicate execution suppressed (balance unchanged, counter unchanged).
    6. Undo in Epoch 1 -> restores balance, REVERTED in journal & registry.
    7. Replay / re-execution -> starts Epoch 2, status STARTED, executes write (counter 1 -> 2, balance debited).
    8. Worker crash during Epoch 2 before registration:
       Registry has Epoch 1 REVERTED, journal has Epoch 2 completion.
       Retry in Epoch 2 suppresses duplicate execution, and promotes registry to Epoch 2 ACTIVE
       without letting older Epoch 1 REVERTED record overwrite newer execution!
    9. Final undo of Epoch 2 -> cleanly restores state to 100 again.
    """
    account = {"balance": 100}
    counter = {"exec": 0}
    capture_healthy = False

    def capture_balance(*args, **kwargs):
        if not capture_healthy:
            raise ConnectionError("Temporary capture failure")
        return account["balance"]

    tx_id = "tx_combined_lifecycle_42"
    mut_id = f"mut_{tx_id}"

    @protect_tool(
        target="account://checking/balance",
        capture_fn=capture_balance,
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        harness=clean_harness,
    )
    def debit_tx(amt: int):
        counter["exec"] += 1
        account["balance"] -= amt
        return {"balance": account["balance"]}

    # --- 1. Capture Failure ---
    with pytest.raises(WitnessCaptureError):
        debit_tx(10, __logical_mutation_id=tx_id)

    assert counter["exec"] == 0
    assert account["balance"] == 100
    j_entry = clean_harness.reconciler.get_entry(tx_id)
    assert j_entry is not None
    assert j_entry.status == MutationLifecycleState.FAILED.value
    assert j_entry.failure_stage == FailureStage.CAPTURE.value
    assert j_entry.epoch == 1
    assert j_entry.attempt == 1

    # --- 2 & 3. Retry transitions to STARTED; in-flight crash fails closed ---
    decision = clean_harness.reconciler.evaluate_request(
        identity=j_entry.identity,
        mutation_registry=clean_harness.mutation_registry,
    )
    assert decision.should_execute_fn is True
    assert decision.status.value == "RETRY_REQUIRED"

    # Verify journal on disk is now STARTED with attempt 2
    started_entry = clean_harness.reconciler.get_entry(tx_id)
    assert started_entry.status == MutationLifecycleState.STARTED.value
    assert started_entry.attempt == 2

    # If the process crashed right here (in-flight while STARTED) and restarts:
    # A fresh reconciler without a probe MUST refuse to retry (fail closed)
    crash_journal = MutationReconciler(journal_path=clean_harness.reconciler.journal_path)
    fresh_harness = EvoUndoHarness(
        registry_path=clean_harness.mutation_registry.storage_path,
        reconciler=crash_journal,
    )
    with pytest.raises(RuntimeError, match="CONFLICT_DETECTED"):
        @protect_tool(
            target="account://checking/balance",
            capture_fn=capture_balance,
            inverse_fn=lambda wit, res: account.update({"balance": wit}),
            harness=fresh_harness,
        )
        def debit_crash(amt: int):
            counter["exec"] += 1
            account["balance"] -= amt
            return {"balance": account["balance"]}

        debit_crash(10, __logical_mutation_id=tx_id)

    assert counter["exec"] == 0
    assert account["balance"] == 100

    # --- 4. Successful Safe Execution with Clean ID ---
    tx2_id = "tx_combined_lifecycle_full"
    mut2_id = f"mut_{tx2_id}"

    capture_healthy = True
    res1 = debit_tx(10, __logical_mutation_id=tx2_id)
    assert res1 == {"balance": 90}
    assert counter["exec"] == 1
    assert account["balance"] == 90

    # Journal COMMITTED and registry ACTIVE in epoch 1
    e1 = clean_harness.reconciler.get_entry(tx2_id)
    assert e1.status == MutationLifecycleState.COMMITTED.value
    assert e1.epoch == 1
    rec1 = clean_harness.mutation_registry.inspect_mutation(mut2_id)
    assert rec1.status == "ACTIVE"
    assert getattr(rec1, "epoch", 1) == 1

    # --- 5. Timeout Retry -> Duplicate Suppressed ---
    res2 = debit_tx(10, __logical_mutation_id=tx2_id)
    assert res2 == {"balance": 90, "cached": True, "logical_mutation_id": tx2_id, "status": "SUCCESS"}
    assert counter["exec"] == 1
    assert account["balance"] == 90

    # --- 6. Undo in Epoch 1 ---
    clean_harness.revert(mut2_id)
    assert account["balance"] == 100
    rec_rev = clean_harness.mutation_registry.inspect_mutation(mut2_id)
    assert rec_rev.status == MutationLifecycleState.REVERTED.value
    assert clean_harness.reconciler.get_entry(tx2_id).status == MutationLifecycleState.REVERTED.value

    # --- 7. Replay / Intentional Re-execution in Epoch 2 ---
    res3 = debit_tx(10, __logical_mutation_id=tx2_id)
    assert counter["exec"] == 2
    assert account["balance"] == 90
    e2 = clean_harness.reconciler.get_entry(tx2_id)
    assert e2.epoch == 2
    assert e2.status == MutationLifecycleState.COMMITTED.value
    rec2 = clean_harness.mutation_registry.inspect_mutation(mut2_id)
    assert rec2.status == "ACTIVE"
    assert rec2.epoch == 2

    # --- 8. Crash before registration in Epoch 2 + Retry ---
    stale_rec = clean_harness.mutation_registry.inspect_mutation(mut2_id)
    stale_rec.status = "REVERTED"
    stale_rec.epoch = 1
    clean_harness.mutation_registry._persist()

    res4 = debit_tx(10, __logical_mutation_id=tx2_id)
    assert res4["cached"] is True
    assert counter["exec"] == 2
    assert account["balance"] == 90

    recovered_rec = clean_harness.mutation_registry.inspect_mutation(mut2_id)
    assert recovered_rec.status == "ACTIVE"
    assert recovered_rec.epoch == 2

    # --- 9. Final Undo of Epoch 2 ---
    clean_harness.revert(mut2_id)
    assert account["balance"] == 100
    assert clean_harness.mutation_registry.inspect_mutation(mut2_id).status == "REVERTED"
    assert clean_harness.reconciler.get_entry(tx2_id).status == "REVERTED"


def test_wrap_tool_combined_lifecycle_safety(clean_harness):
    """Ensure wrap_tool exhibits identical centralized lifecycle semantics as protect_tool."""
    account = {"balance": 200}
    counter = {"exec": 0}

    def raw_withdraw(amount: int):
        counter["exec"] += 1
        account["balance"] -= amount
        return {"balance": account["balance"]}

    wrapped = wrap_tool(
        raw_withdraw,
        target="bank://checking/account2",
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        capture_fn=lambda amount: account["balance"],
        harness=clean_harness,
    )

    tx_id = "tx_wrap_lifecycle_1"
    mut_id = f"mut_{tx_id}"

    # 1. First execution
    res1 = wrapped(50, __logical_mutation_id=tx_id)
    assert res1 == {"balance": 150}
    assert counter["exec"] == 1
    assert account["balance"] == 150

    # 2. Duplicate suppression
    res2 = wrapped(50, __logical_mutation_id=tx_id)
    assert counter["exec"] == 1
    assert account["balance"] == 150

    # 3. Undo Epoch 1
    clean_harness.revert(mut_id)
    assert account["balance"] == 200
    assert clean_harness.reconciler.get_entry(tx_id).status == MutationLifecycleState.REVERTED.value

    # 4. Re-execution in Epoch 2
    res3 = wrapped(50, __logical_mutation_id=tx_id)
    assert counter["exec"] == 2
    assert account["balance"] == 150
    assert clean_harness.reconciler.get_entry(tx_id).epoch == 2

    # 5. Stale registry revert cannot overwrite Epoch 2 on retry
    stale_rec = clean_harness.mutation_registry.inspect_mutation(mut_id)
    stale_rec.status = "REVERTED"
    stale_rec.epoch = 1
    clean_harness.mutation_registry._persist()
    res4 = wrapped(50, __logical_mutation_id=tx_id)
    assert counter["exec"] == 2
    assert account["balance"] == 150
    rec = clean_harness.mutation_registry.inspect_mutation(mut_id)
    assert rec.status == "ACTIVE"
    assert rec.epoch == 2

    # 6. Revert Epoch 2
    clean_harness.revert(mut_id)
    assert account["balance"] == 200


def test_protected_tool_wrapper_combined_lifecycle_safety(clean_harness):
    """Ensure ProtectedToolWrapper exhibits identical centralized lifecycle semantics."""
    from evoundo.protection.decorator import ProtectedToolWrapper

    account = {"balance": 300}
    counter = {"exec": 0}

    def raw_debit(amt: int):
        counter["exec"] += 1
        account["balance"] -= amt
        return account["balance"]

    protected = ProtectedToolWrapper(
        fn=raw_debit,
        target="account://balance",
        capture_fn=lambda amt: account["balance"],
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        harness=clean_harness,
        reconciler=clean_harness.reconciler,
    )

    tx_id = "tx_ptw_lifecycle_1"

    # 1. Execute
    res1 = protected(50, __logical_mutation_id=tx_id)
    assert res1 == 250
    assert counter["exec"] == 1
    assert account["balance"] == 250

    # 2. Duplicate suppression
    res2 = protected(50, __logical_mutation_id=tx_id)
    assert res2 == 250
    assert counter["exec"] == 1

    # 3. Undo Epoch 1
    clean_harness.revert(f"mut_{tx_id}")
    assert account["balance"] == 300

    # 4. Re-execute Epoch 2
    res3 = protected(50, __logical_mutation_id=tx_id)
    assert res3 == 250
    assert counter["exec"] == 2
    assert account["balance"] == 250
    assert clean_harness.reconciler.get_entry(tx_id).epoch == 2

    # 5. Final undo Epoch 2
    clean_harness.revert(f"mut_{tx_id}")
    assert account["balance"] == 300


def test_missing_journal_with_active_registry_blocks_duplicate_write_declarative(clean_harness, tmp_path):
    """P1: If journal is lost/missing while registry records mutation as ACTIVE,
    retry must consult registry evidence, suppress duplicate write (no 100 -> 90 -> 80),
    hydrate journal, and allow subsequent revert.
    """
    account = {"balance": 100}
    counter = {"exec": 0}

    @protect_tool(
        target="account://checking/primary_balance",
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        capture_fn=lambda *a, **kw: account["balance"],
        harness=clean_harness,
    )
    def debit_tool(amt: int):
        counter["exec"] += 1
        account["balance"] -= amt
        return {"balance": account["balance"]}

    tx_id = "tx_journal_loss_decl_1"
    mut_id = f"mut_{tx_id}"

    # 1. First execution: balance 100 -> 90
    res1 = debit_tool(10, __logical_mutation_id=tx_id)
    assert res1 == {"balance": 90}
    assert counter["exec"] == 1
    assert account["balance"] == 90
    assert clean_harness.mutation_registry.inspect_mutation(mut_id).status == "ACTIVE"

    # 2. Simulate complete journal loss in temporary storage:
    fresh_journal_path = str(tmp_path / "fresh_lost_journal_decl.json")
    clean_harness.reconciler = MutationReconciler(journal_path=fresh_journal_path)

    # Verify fresh reconciler has no entry for this transaction
    assert clean_harness.reconciler.get_entry(tx_id) is None

    # 3. Retry arrives: MUST consult registry evidence and suppress duplicate execution
    res2 = debit_tool(10, __logical_mutation_id=tx_id)
    assert counter["exec"] == 1  # Crucial: did NOT execute again (no 100 -> 90 -> 80)!
    assert account["balance"] == 90
    assert res2["cached"] is True
    assert res2["balance"] == 90

    # 4. Verify journal entry was hydrated as COMMITTED
    hydrated = clean_harness.reconciler.get_entry(tx_id)
    assert hydrated is not None
    assert hydrated.status == MutationLifecycleState.COMMITTED.value

    # 5. Subsequent revert works cleanly
    clean_harness.revert(mut_id)
    assert account["balance"] == 100
    assert clean_harness.mutation_registry.inspect_mutation(mut_id).status == "REVERTED"
    assert clean_harness.reconciler.get_entry(tx_id).status == "REVERTED"


def test_missing_journal_with_active_registry_blocks_duplicate_write_wrapper(clean_harness, tmp_path):
    """P1: wrap_tool must safely block duplicate writes when journal is lost."""
    account = {"balance": 200}
    counter = {"exec": 0}

    def raw_withdraw(amt: int):
        counter["exec"] += 1
        account["balance"] -= amt
        return {"balance": account["balance"]}

    wrapped = wrap_tool(
        raw_withdraw,
        target="account://checking/wrapper_balance",
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        capture_fn=lambda *a, **kw: account["balance"],
        harness=clean_harness,
    )

    tx_id = "tx_journal_loss_wrap_1"
    mut_id = f"mut_{tx_id}"

    # 1. Execution
    res1 = wrapped(20, __logical_mutation_id=tx_id)
    assert res1 == {"balance": 180}
    assert counter["exec"] == 1
    assert account["balance"] == 180

    # 2. Simulate journal loss
    fresh_journal_path = str(tmp_path / "fresh_lost_journal_wrap.json")
    clean_harness.reconciler = MutationReconciler(journal_path=fresh_journal_path)
    assert clean_harness.reconciler.get_entry(tx_id) is None

    # 3. Retry arrives
    res2 = wrapped(20, __logical_mutation_id=tx_id)
    assert counter["exec"] == 1
    assert account["balance"] == 180
    assert res2["cached"] is True
    assert res2["balance"] == 180

    # 4. Revert
    clean_harness.revert(mut_id)
    assert account["balance"] == 200


def test_missing_journal_with_active_registry_blocks_duplicate_write_protected_tool_wrapper(clean_harness, tmp_path):
    """P1: ProtectedToolWrapper must safely block duplicate writes when journal is lost."""
    from evoundo.protection.decorator import ProtectedToolWrapper

    account = {"balance": 300}
    counter = {"exec": 0}

    def raw_pay(amt: int):
        counter["exec"] += 1
        account["balance"] -= amt
        return account["balance"]

    protected = ProtectedToolWrapper(
        fn=raw_pay,
        target="account://checking/ptw_balance",
        capture_fn=lambda *a, **kw: account["balance"],
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        harness=clean_harness,
    )

    tx_id = "tx_journal_loss_ptw_1"
    mut_id = f"mut_{tx_id}"

    # 1. Execution
    res1 = protected(50, __logical_mutation_id=tx_id)
    assert res1 == 250
    assert counter["exec"] == 1
    assert account["balance"] == 250

    # 2. Simulate journal loss
    fresh_journal_path = str(tmp_path / "fresh_lost_journal_ptw.json")
    clean_harness.reconciler = MutationReconciler(journal_path=fresh_journal_path)
    protected.reconciler = clean_harness.reconciler
    assert clean_harness.reconciler.get_entry(tx_id) is None

    # 3. Retry arrives
    res2 = protected(50, __logical_mutation_id=tx_id)
    assert counter["exec"] == 1
    assert account["balance"] == 250
    assert res2 == 250

    # 4. Revert
    clean_harness.revert(mut_id)
    assert account["balance"] == 300


def test_full_restart_with_missing_journal_blocks_duplicate_write_declarative(tmp_path):
    """P1 & P2: Full harness restart from disk with missing journal must:
    1. Not crash on MutationIdentity.from_dict()
    2. Reload metadata and return cached result (not None)
    3. Suppress duplicate execution (no 100 -> 90 -> 80)
    4. Allow subsequent revert to succeed cleanly.
    """
    account = {"balance": 100}
    counter = {"exec": 0}
    reg_file = str(tmp_path / "full_restart_decl_reg.json")
    journal_file = str(tmp_path / "full_restart_decl_journal.json")

    # 1. Initial harness execution
    harness1 = EvoUndoHarness(
        registry_path=reg_file,
        reconciler=MutationReconciler(journal_path=journal_file),
    )

    def raw_debit(amt: int):
        counter["exec"] += 1
        account["balance"] -= amt
        return {"balance": account["balance"]}

    tool1 = protect_tool(
        target="account://checking/full_restart_decl",
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        capture_fn=lambda *a, **kw: account["balance"],
        harness=harness1,
    )(raw_debit)

    tx_id = "tx_full_restart_decl"
    mut_id = f"mut_{tx_id}"

    res1 = tool1(10, __logical_mutation_id=tx_id)
    assert res1 == {"balance": 90}
    assert counter["exec"] == 1
    assert account["balance"] == 90

    # 2. Simulate complete journal loss on temporary storage
    lost_journal_file = str(tmp_path / "lost_journal_decl.json")

    # 3. RECONSTRUCT ENTIRE HARNESS FROM DISK (fresh processes / restarted worker)
    harness2 = EvoUndoHarness(
        registry_path=reg_file,
        reconciler=MutationReconciler(journal_path=lost_journal_file),
    )

    # Re-wrap tool with reconstructed harness
    tool2 = protect_tool(
        target="account://checking/full_restart_decl",
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        capture_fn=lambda *a, **kw: account["balance"],
        harness=harness2,
    )(raw_debit)

    # 4. Retry arrives: must not raise AttributeError and must suppress duplicate execution
    res2 = tool2(10, __logical_mutation_id=tx_id)
    assert counter["exec"] == 1  # No duplicate execution!
    assert account["balance"] == 90
    assert res2["cached"] is True
    assert res2["balance"] == 90  # P2: Cached result preserved, not lost!

    # 5. Subsequent revert on reconstructed harness works cleanly
    harness2.revert(mut_id)
    assert account["balance"] == 100
    assert harness2.mutation_registry.inspect_mutation(mut_id).status == "REVERTED"


def test_full_restart_with_missing_journal_blocks_duplicate_write_wrapper(tmp_path):
    """P1 & P2: Full harness restart with wrap_tool must suppress duplicate and preserve cached result."""
    account = {"balance": 200}
    counter = {"exec": 0}
    reg_file = str(tmp_path / "full_restart_wrap_reg.json")
    journal_file = str(tmp_path / "full_restart_wrap_journal.json")

    harness1 = EvoUndoHarness(
        registry_path=reg_file,
        reconciler=MutationReconciler(journal_path=journal_file),
    )

    def raw_withdraw(amt: int):
        counter["exec"] += 1
        account["balance"] -= amt
        return {"balance": account["balance"]}

    tool1 = wrap_tool(
        raw_withdraw,
        target="account://checking/full_restart_wrap",
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        capture_fn=lambda *a, **kw: account["balance"],
        harness=harness1,
    )

    tx_id = "tx_full_restart_wrap"
    mut_id = f"mut_{tx_id}"

    res1 = tool1(20, __logical_mutation_id=tx_id)
    assert res1 == {"balance": 180}
    assert counter["exec"] == 1
    assert account["balance"] == 180

    # Simulate journal loss & full restart
    lost_journal_file = str(tmp_path / "lost_journal_wrap.json")
    harness2 = EvoUndoHarness(
        registry_path=reg_file,
        reconciler=MutationReconciler(journal_path=lost_journal_file),
    )

    tool2 = wrap_tool(
        raw_withdraw,
        target="account://checking/full_restart_wrap",
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        capture_fn=lambda *a, **kw: account["balance"],
        harness=harness2,
    )

    res2 = tool2(20, __logical_mutation_id=tx_id)
    assert counter["exec"] == 1
    assert account["balance"] == 180
    assert res2["cached"] is True
    assert res2["balance"] == 180

    harness2.revert(mut_id)
    assert account["balance"] == 200


def test_full_restart_with_missing_journal_blocks_duplicate_write_protected_tool_wrapper(tmp_path):
    """P1 & P2: Full harness restart with ProtectedToolWrapper must preserve cached result (not None)."""
    from evoundo.protection.decorator import ProtectedToolWrapper

    account = {"balance": 300}
    counter = {"exec": 0}
    reg_file = str(tmp_path / "full_restart_ptw_reg.json")
    journal_file = str(tmp_path / "full_restart_ptw_journal.json")

    harness1 = EvoUndoHarness(
        registry_path=reg_file,
        reconciler=MutationReconciler(journal_path=journal_file),
    )

    def raw_pay(amt: int):
        counter["exec"] += 1
        account["balance"] -= amt
        return {"balance": account["balance"]}

    tool1 = ProtectedToolWrapper(
        fn=raw_pay,
        target="account://checking/full_restart_ptw",
        capture_fn=lambda *a, **kw: account["balance"],
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        harness=harness1,
    )

    tx_id = "tx_full_restart_ptw"
    mut_id = f"mut_{tx_id}"

    res1 = tool1(50, __logical_mutation_id=tx_id)
    assert res1 == {"balance": 250}
    assert counter["exec"] == 1
    assert account["balance"] == 250

    # Simulate journal loss & full restart
    lost_journal_file = str(tmp_path / "lost_journal_ptw.json")
    harness2 = EvoUndoHarness(
        registry_path=reg_file,
        reconciler=MutationReconciler(journal_path=lost_journal_file),
    )

    tool2 = ProtectedToolWrapper(
        fn=raw_pay,
        target="account://checking/full_restart_ptw",
        capture_fn=lambda *a, **kw: account["balance"],
        inverse_fn=lambda wit, res: account.update({"balance": wit}),
        harness=harness2,
    )

    # Crucial assertion: ProtectedToolWrapper must return {"balance": 250}, NOT None!
    res2 = tool2(50, __logical_mutation_id=tx_id)
    assert counter["exec"] == 1
    assert account["balance"] == 250
    assert res2 == {"balance": 250}

    harness2.revert(mut_id)
    assert account["balance"] == 300


def test_json_driver_recovery_preserves_independent_updates_after_restart_and_retry(tmp_path):
    """P1: After restart and retry of a JSON setting change, undoing that change must NOT
    erase a later, independently tracked setting update on the same JSON configuration file.
    The driver recovery operation must be preserved and not replaced by a whole-file restore.
    """
    import json
    from evoundo.drivers.json_config import json_config

    config_file = str(tmp_path / "service_config.json")
    with open(config_file, "w") as f:
        json.dump({"service": {"env": {"SETTING_A": "0", "SETTING_B": "0"}}}, f)

    reg_file = str(tmp_path / "driver_test_reg.json")
    journal_file = str(tmp_path / "driver_test_journal.json")

    harness1 = EvoUndoHarness(
        registry_path=reg_file,
        reconciler=MutationReconciler(journal_path=journal_file),
    )

    # Tool A updates SETTING_A
    @protect_tool(
        target=f"config://{config_file}/service/env/SETTING_A",
        inverse_fn=lambda wit, res: None,
        harness=harness1,
    )
    def update_setting_a(val: str):
        with json_config(config_file) as cfg:
            cfg.set_env("SETTING_A", val)
        return {"status": "A_updated"}

    # Tool B updates SETTING_B
    @protect_tool(
        target=f"config://{config_file}/service/env/SETTING_B",
        inverse_fn=lambda wit, res: None,
        harness=harness1,
    )
    def update_setting_b(val: str):
        with json_config(config_file) as cfg:
            cfg.set_env("SETTING_B", val)
        return {"status": "B_updated"}

    tx_a = "tx_setting_a"
    tx_b = "tx_setting_b"
    mut_a = f"mut_{tx_a}"
    mut_b = f"mut_{tx_b}"

    # 1. Execute mutation A: SETTING_A -> 1
    update_setting_a("1", __logical_mutation_id=tx_a)

    # 2. Execute mutation B: SETTING_B -> 2
    update_setting_b("2", __logical_mutation_id=tx_b)

    with open(config_file) as f:
        current_cfg = json.load(f)
    assert current_cfg["service"]["env"]["SETTING_A"] == "1"
    assert current_cfg["service"]["env"]["SETTING_B"] == "2"
    assert harness1.mutation_registry.inspect_mutation(mut_a).status == "ACTIVE"
    assert harness1.mutation_registry.inspect_mutation(mut_b).status == "ACTIVE"

    # 3. Simulate process restart and journal loss in temporary storage
    lost_journal = str(tmp_path / "lost_journal_driver.json")
    harness2 = EvoUndoHarness(
        registry_path=reg_file,
        reconciler=MutationReconciler(journal_path=lost_journal),
    )

    # Re-declare Tool A in restarted process with a whole-file restore inverse_fn
    file_snapshot = {"service": {"env": {"SETTING_A": "0", "SETTING_B": "0"}}}
    def whole_file_restore(wit, res):
        with open(config_file, "w") as f:
            json.dump(file_snapshot, f)

    tool_a_restarted = protect_tool(
        target=f"config://{config_file}/service/env/SETTING_A",
        inverse_fn=whole_file_restore,  # Whole file restore! Must NOT replace DriverRecoveryOp!
        harness=harness2,
    )(lambda val: None)

    # 4. Retry of Mutation A arrives
    res_retry = tool_a_restarted("1", __logical_mutation_id=tx_a)
    assert res_retry["cached"] is True

    # 5. Revert Mutation A: MUST NOT erase Mutation B!
    harness2.revert(mut_a)

    with open(config_file) as f:
        post_revert_cfg = json.load(f)

    # SETTING_A should be reverted to "0", but SETTING_B MUST REMAIN "2"!
    assert post_revert_cfg["service"]["env"]["SETTING_A"] == "0"
    assert post_revert_cfg["service"]["env"]["SETTING_B"] == "2"

    # Mutation B must still show ACTIVE
    assert harness2.mutation_registry.inspect_mutation(mut_b).status == "ACTIVE"
    assert harness2.mutation_registry.inspect_mutation(mut_a).status == "REVERTED"








