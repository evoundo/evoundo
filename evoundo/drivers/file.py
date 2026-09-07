"""Filesystem Driver with automatic byte-offset pre-state capture and truncation inversion."""

from __future__ import annotations
import datetime
import json
import os
from typing import Any, Dict, Optional

from evoundo.context import get_current_context


class AuditFileDriver:
    """Manages append-only file operations with automatic truncation inversion."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.initial_offset = 0

    def __enter__(self) -> AuditFileDriver:
        if os.path.exists(self.file_path):
            self.initial_offset = os.path.getsize(self.file_path)
        else:
            self.initial_offset = 0
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def append_json(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Append a JSON record to the audit file and register truncation inverse."""
        event = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            **event_data,
        }

        os.makedirs(os.path.dirname(os.path.abspath(self.file_path)), exist_ok=True)
        with open(self.file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

        file_path = self.file_path

        def _inverse_fn(witness, result):
            if os.path.exists(file_path) and witness is not None:
                with open(file_path, "r+", encoding="utf-8") as f:
                    f.truncate(witness)

        # Attach to active EvoUndo context
        ctx = get_current_context()
        target_key = ctx.active_target or f"file:{os.path.basename(file_path)}"
        ctx.active_target = target_key
        ctx.active_witness[target_key] = self.initial_offset
        ctx.active_inverse_fn = _inverse_fn

        from evoundo.recovery.operations import DriverRecoveryOp
        op = DriverRecoveryOp(
            driver_type="audit_file",
            target=target_key,
            operation="append",
            parameters={"file_path": os.path.abspath(file_path)},
            witness_data=self.initial_offset,
            inverse_fn=lambda s, w: _inverse_fn(self.initial_offset, None),
        )
        ctx.active_recovery_ops.append(op)

        return event


def audit_file(file_path: str) -> AuditFileDriver:
    """Convenience context manager constructor for append-only audit logs."""
    return AuditFileDriver(file_path)
