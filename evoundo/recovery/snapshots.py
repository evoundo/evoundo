"""Snapshot and comparison recovery strategies."""

from __future__ import annotations
import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional
from evoundo.core.state import HarnessState
from evoundo.effects.contracts import EffectCategory, EffectContract
from evoundo.recovery.operations import RecoveryProgram
from evoundo.witness.stores import Witness


class RecoveryStrategy(str, Enum):
    """Supported recovery engine execution strategies."""
    EVOUNDO_RECOVERY = "EVOUNDO_RECOVERY"              # Minimal semantic inversion targeting exact effects
    FULL_SNAPSHOT = "FULL_SNAPSHOT"                    # Complete blind snapshot restoration
    EFFECT_SCOPED_SNAPSHOT = "EFFECT_SCOPED_SNAPSHOT"  # Whole-subsystem snapshot rollback (e.g. all config/tools)


@dataclass
class SnapshotStore:
    """Stores full and scoped snapshots for comparative recovery benchmarking."""
    full_snapshots: Dict[str, HarnessState] = field(default_factory=dict)
    scoped_snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def capture_full(self, mutation_id: str, state: HarnessState) -> None:
        self.full_snapshots[mutation_id] = state.clone()

    def capture_scoped(self, mutation_id: str, state: HarnessState, contract: EffectContract) -> None:
        scoped: Dict[str, Any] = {}
        for cat, _ in contract.all_declared():
            if cat == EffectCategory.CONFIG:
                scoped["config"] = copy.deepcopy(state.config)
            elif cat == EffectCategory.TOOLS:
                scoped["tools"] = {k: copy.deepcopy(v) for k, v in state.tools.items()}
            elif cat == EffectCategory.MIDDLEWARE:
                scoped["middleware"] = [copy.deepcopy(m) for m in state.middleware]
            elif cat == EffectCategory.EVENT_LISTENERS:
                scoped["event_listeners"] = {k: [copy.deepcopy(l) for l in v] for k, v in state.event_listeners.items()}
            elif cat == EffectCategory.FILES:
                scoped["files"] = {k: copy.deepcopy(v) for k, v in state.files.items()}
            elif cat == EffectCategory.RESOURCES:
                scoped["resources"] = {k: copy.deepcopy(v) for k, v in state.resources.items()}
            elif cat == EffectCategory.PROMPTS:
                scoped["prompts"] = copy.deepcopy(state.prompts)
        self.scoped_snapshots[mutation_id] = scoped

    def restore_full(self, mutation_id: str) -> Optional[HarnessState]:
        snap = self.full_snapshots.get(mutation_id)
        return snap.clone() if snap else None

    def restore_scoped(self, mutation_id: str, current_state: HarnessState) -> Optional[HarnessState]:
        scoped = self.scoped_snapshots.get(mutation_id)
        if not scoped:
            return None
        res_state = current_state.clone()
        if "config" in scoped:
            res_state.config = copy.deepcopy(scoped["config"])
        if "tools" in scoped:
            res_state.tools = copy.deepcopy(scoped["tools"])
        if "middleware" in scoped:
            res_state.middleware = copy.deepcopy(scoped["middleware"])
        if "event_listeners" in scoped:
            res_state.event_listeners = copy.deepcopy(scoped["event_listeners"])
        if "files" in scoped:
            res_state.files = copy.deepcopy(scoped["files"])
        if "resources" in scoped:
            res_state.resources = copy.deepcopy(scoped["resources"])
        if "prompts" in scoped:
            res_state.prompts = copy.deepcopy(scoped["prompts"])
        return res_state
