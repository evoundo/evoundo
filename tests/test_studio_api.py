import uuid
import pytest
from fastapi.testclient import TestClient

from evoundo.studio.app import app, harness_instance
from evoundo.identity import MutationIdentity
from evoundo.recovery.operations import RecoveryProgram, CustomRecoveryOp
from evoundo.witness.stores import Witness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType


@pytest.fixture
def client():
    return TestClient(app)


def test_studio_static_and_status(client):
    # Test HTML UI root
    res_root = client.get("/")
    assert res_root.status_code == 200
    assert "EvoUndo" in res_root.text

    # Test API status
    res_status = client.get("/api/status")
    assert res_status.status_code == 200
    data = res_status.json()
    assert data.get("product_version") == "0.1.0"


def test_operator_api_workflow(client):
    # Stage and commit a test mutation using harness with unique ID
    unique_suffix = uuid.uuid4().hex[:8]
    state = {"theme": "dark"}
    mut_id = f"mut_studio_{unique_suffix}"
    op = CustomRecoveryOp(name="RevertTheme", inverse_fn=lambda s, w: state.update({"theme": "light"}))
    harness_instance.record_external_protected_mutation(
        mutation_id=mut_id,
        identity=MutationIdentity(f"log_studio_{unique_suffix}", "studio_tool", "set_theme"),
        recovery_program=RecoveryProgram([op]),
        witness=Witness(mut_id, {"theme": "light"}),
        declared_effects=[
            Effect(category=EffectCategory.RESOURCES, target="users.settings/u1/theme", op_type=EffectOpType.UPDATE)
        ],
        description="Set user theme to dark",
    )

    # 1. List mutations
    res_list = client.get("/api/v1/operator/mutations")
    assert res_list.status_code == 200
    mutations = res_list.json()
    assert any(m["mutation_id"] == mut_id for m in mutations)

    # 2. Inspect mutation
    res_inspect = client.get(f"/api/v1/operator/mutations/{mut_id}/inspect")
    assert res_inspect.status_code == 200
    inspect_data = res_inspect.json()
    assert inspect_data["mutation_id"] == mut_id

    # 3. Diff mutation
    res_diff = client.get(f"/api/v1/operator/mutations/{mut_id}/diff")
    assert res_diff.status_code == 200

    # 4. Feasibility check
    res_feas = client.post(f"/api/v1/operator/mutations/{mut_id}/feasibility")
    assert res_feas.status_code == 200
    assert res_feas.json()["can_revert"] is True

    # 5. Revert via Operator API (using OperatorUser defaults)
    payload = {
        "user_email": "developer@localhost",
        "user_roles": ["OPERATOR"],
        "reason": "Studio operator revert test",
    }
    res_revert = client.post(f"/api/v1/operator/mutations/{mut_id}/revert", json=payload)
    assert res_revert.status_code == 200
    revert_data = res_revert.json()
    assert revert_data["status"] == "REVERTED"

    # 6. Retrieve audit record
    res_audit = client.get(f"/api/v1/operator/mutations/{mut_id}/audit")
    assert res_audit.status_code == 200
    audit_data = res_audit.json()
    assert audit_data["mutation_id"] == mut_id
    assert audit_data["physical_recovery_verified"] is True
