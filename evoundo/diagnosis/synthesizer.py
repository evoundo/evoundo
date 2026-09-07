"""Recovery synthesizer performing iterative repair and joint (m, w, u, C_e) optimization."""

from __future__ import annotations
import copy
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING
from evoundo.core.mutation import MutationProposal
from evoundo.core.state import HarnessState
from evoundo.diagnosis.engine import DiagnosticEngine, DiagnosticReport, FailureType, RepairAction
from evoundo.effects.contracts import EffectCategory, EffectContract
from evoundo.recovery.operations import RecoveryProgram

if TYPE_CHECKING:
    from evoundo.core.harness import EvoUndoHarness


class RecoverySynthesizer:
    """Iterative synthesis engine repairing failed recovery proposals under diagnostic guidance."""

    @classmethod
    def synthesize_repaired_proposal(
        cls,
        original_proposal: MutationProposal,
        diagnosis: DiagnosticReport,
    ) -> MutationProposal:
        """Construct an updated candidate proposal with repaired contract and recovery semantics."""
        repaired_contract = copy.deepcopy(original_proposal.effect_contract)

        # 1. Apply Contract Repairs (Include previously undeclared effects)
        for repair in diagnosis.recommended_repairs:
            if repair.action_type == "declare_effect":
                try:
                    cat = EffectCategory(repair.surface.lower())
                    repaired_contract.declare(cat, repair.target_key)
                except Exception:
                    pass

        # Also inspect hidden effects directly
        for h in diagnosis.hidden_effects:
            if "[" in h and "]" in h:
                parts = h.split("]", 1)
                cat_str = parts[0].replace("[", "").strip().lower()
                tgt = parts[1].strip()
                try:
                    cat = EffectCategory(cat_str)
                    repaired_contract.declare(cat, tgt)
                except Exception:
                    pass

        # 2. Synthesize updated complete recovery program from repaired contract
        repaired_recovery_program = RecoveryProgram.synthesize_from_contract(repaired_contract)

        # 3. Create updated MutationProposal
        repaired_proposal = MutationProposal(
            mutation_id=f"{original_proposal.mutation_id}_repaired",
            description=f"{original_proposal.description} (Auto-Repaired via EvoUndo Synthesizer)",
            forward_mutation=original_proposal.forward_mutation,
            recovery_program=repaired_recovery_program,
            effect_contract=repaired_contract,
            proposer=original_proposal.proposer,
            metadata={
                **original_proposal.metadata,
                "repaired_from": original_proposal.mutation_id,
                "applied_repairs_count": len(diagnosis.recommended_repairs),
            },
        )

        return repaired_proposal
