import pytest

from evoundo.core.causal_tree import MultiAgentCausalTree, CausalContingency
from evoundo.effects.address import ResourceAddress
from evoundo.core.harness import EvoUndoHarness
from evoundo.identity import MutationIdentity
from evoundo.recovery.operations import RecoveryProgram
from evoundo.witness.stores import Witness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType


def test_resource_address_normalization():
    addr1 = ResourceAddress.parse("sqlite://accounts/users/42#balance")
    addr2 = ResourceAddress.parse("sqlite://accounts/users/42#balance")
    assert addr1 == addr2
    assert addr1.scheme == "sqlite"
    assert "accounts" in addr1.segments
    assert addr1.canonical_uri == "sqlite://accounts/users/42#balance"


def test_multi_agent_causal_tree():
    tree = MultiAgentCausalTree()
    tree.register_parent("incident_101")

    # Agent 1 performs M1
    tree.register_child_action(
        action_id="act_1",
        parent_id="incident_101",
        child_agent="agent_alpha",
        action_type="UPDATE",
        mutation_id="mut_1",
        contingency=CausalContingency.HYPOTHESIS_DEPENDENT,
    )

    # Agent 2 performs dependent action M2
    tree.register_child_action(
        action_id="act_2",
        parent_id="incident_101",
        child_agent="agent_beta",
        action_type="UPDATE",
        mutation_id="mut_2",
        contingency=CausalContingency.HYPOTHESIS_DEPENDENT,
    )

    # Agent 3 performs independent action M3
    tree.register_child_action(
        action_id="act_3",
        parent_id="incident_101",
        child_agent="agent_gamma",
        action_type="INSERT",
        mutation_id="mut_3",
        contingency=CausalContingency.INDEPENDENT,
    )

    candidates = tree.get_contingent_mutations("incident_101")
    assert "mut_1" in candidates
    assert "mut_2" in candidates
    assert "mut_3" not in candidates


def test_conflict_detection_on_same_resource(tmp_path):
    reg_path = str(tmp_path / "conflict_reg.json")
    harness = EvoUndoHarness(registry_path=reg_path)

    # Mutation 1 on resource target
    harness.record_external_protected_mutation(
        mutation_id="mut_user_1",
        identity=MutationIdentity("log_1", "tool", "update"),
        recovery_program=RecoveryProgram([]),
        witness=Witness("mut_user_1", {}),
        declared_effects=[
            Effect(category=EffectCategory.RESOURCES, target="postgres://users/42/tier", op_type=EffectOpType.UPDATE)
        ],
        description="Update tier to gold",
    )

    # Mutation 2 modifying the same resource target subsequently
    harness.record_external_protected_mutation(
        mutation_id="mut_user_2",
        identity=MutationIdentity("log_2", "tool", "update"),
        recovery_program=RecoveryProgram([]),
        witness=Witness("mut_user_2", {}),
        declared_effects=[
            Effect(category=EffectCategory.RESOURCES, target="postgres://users/42/tier", op_type=EffectOpType.UPDATE)
        ],
        description="Update tier to platinum",
    )

    # Attempting to revert mut_user_1 out-of-order must be refused due to conflict
    with pytest.raises(Exception) as exc_info:
        harness.revert("mut_user_1")
    assert "conflict" in str(exc_info.value).lower() or "CONFLICT_DETECTED" in str(exc_info.value)
