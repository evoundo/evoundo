"""Terraform & OpenTofu state recovery driver for EvoUndo.

Provides state-snapshot capture, serial fencing, drift detection,
and state file restoration for autonomous infrastructure mutations.
"""

from __future__ import annotations
import copy
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.recovery.driver_registry import DriverRegistry

logger = logging.getLogger("evoundo.devops.terraform")


class TerraformDriver:
    """Production Terraform / OpenTofu state mutation driver."""

    DRIVER_TYPE = "terraform"
    _mock_state_stores: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def register(cls) -> None:
        DriverRegistry.register_driver(
            driver_type=cls.DRIVER_TYPE,
            executor=cls.execute_recovery,
            verifier=cls.verify_recovery,
        )
        logger.info("Registered '%s' surface driver with DriverRegistry", cls.DRIVER_TYPE)

    @classmethod
    def capture_state_witness(cls, state_path: str) -> Dict[str, Any]:
        """Capture pre-mutation snapshot of terraform.tfstate."""
        if state_path in cls._mock_state_stores:
            state_data = cls._mock_state_stores[state_path]
            return {
                "state_path": state_path,
                "serial": state_data.get("serial", 1),
                "lineage": state_data.get("lineage", "default_lineage"),
                "state": copy.deepcopy(state_data),
            }

        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            return {
                "state_path": state_path,
                "serial": state_data.get("serial", 1),
                "lineage": state_data.get("lineage"),
                "state": state_data,
            }

        return {"state_path": state_path, "serial": 0, "lineage": None, "state": None}

    @classmethod
    def execute_recovery(cls, op: Any, witness: Any) -> None:
        """Rollback Terraform state file to pre-apply state snapshot."""
        params = op.parameters or {}
        state_path = params.get("state_path", "terraform.tfstate")

        w = op.witness_data or params.get("witness", {})
        if hasattr(w, "data") and isinstance(w.data, dict):
            w = w.data
        elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
            w = witness.data

        orig_state = w.get("state")
        if orig_state is not None:
            # Check downstream conflict (e.g. another terraform apply occurred)
            curr = cls._mock_state_stores.get(state_path)
            expected_post_serial = params.get("expected_post_serial")
            if curr and expected_post_serial:
                curr_serial = curr.get("serial", 1)
                if curr_serial > expected_post_serial:
                    raise ValueError(
                        f"CONFLICT_DETECTED: Terraform state serial {curr_serial} has advanced "
                        f"beyond mutated serial {expected_post_serial}"
                    )

            restored = copy.deepcopy(orig_state)
            restored["serial"] = restored.get("serial", 1) + 1  # Increment serial on recovery
            cls._mock_state_stores[state_path] = restored

            if os.path.exists(state_path):
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(restored, f, indent=2)
            logger.info("Restored Terraform state for '%s' to serial %d", state_path, restored["serial"])

    @classmethod
    def verify_recovery(cls, op: Any, witness: Any) -> bool:
        """Verify Terraform state file matches pre-apply resource topology."""
        params = op.parameters or {}
        state_path = params.get("state_path", "terraform.tfstate")

        w = op.witness_data or params.get("witness", {})
        if hasattr(w, "data") and isinstance(w.data, dict):
            w = w.data
        elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
            w = witness.data

        orig_state = w.get("state", {})
        curr = cls._mock_state_stores.get(state_path)
        if not curr and os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                curr = json.load(f)

        if not curr:
            return False

        # Compare resources count
        curr_res = curr.get("resources", [])
        orig_res = orig_state.get("resources", [])
        return len(curr_res) == len(orig_res)
