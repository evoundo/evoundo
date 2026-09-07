"""JSON and YAML Configuration Driver with scoped key pre-state capture and inverse generation."""

from __future__ import annotations
import json
import os
from typing import Any, Dict, Optional

from evoundo.context import get_current_context


class JSONConfigDriver:
    """Manages JSON file modifications with automatic key-level pre-state capture and inversion."""

    def __init__(self, manifest_path: str):
        self.manifest_path = manifest_path
        self.data: Dict[str, Any] = {}

    def __enter__(self) -> JSONConfigDriver:
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        else:
            self.data = {}
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            os.makedirs(os.path.dirname(os.path.abspath(self.manifest_path)), exist_ok=True)
            with open(self.manifest_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)

    def set_env(self, key: str, value: Any) -> Dict[str, Any]:
        """Update an environment variable under service.env with automatic scoped inverse."""
        service_dict = self.data.setdefault("service", {})
        env_dict = service_dict.setdefault("env", {})
        old_val = env_dict.get(key)

        # Update
        env_dict[key] = str(value)

        manifest_path = self.manifest_path

        def _inverse_fn(witness, result):
            if os.path.exists(manifest_path):
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                e = manifest.setdefault("service", {}).setdefault("env", {})
                if witness is None:
                    e.pop(key, None)
                else:
                    e[key] = witness
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, indent=2)

        # Register with active EvoUndo context
        ctx = get_current_context()
        target_key = ctx.active_target or f"config:{os.path.basename(manifest_path)}:{key}"
        ctx.active_target = target_key
        ctx.active_witness[target_key] = old_val
        ctx.active_inverse_fn = _inverse_fn

        from evoundo.recovery.operations import DriverRecoveryOp
        key_existed = old_val is not None
        op = DriverRecoveryOp(
            driver_type="json_config",
            target=target_key,
            operation="set_env",
            parameters={
                "manifest_path": os.path.abspath(manifest_path),
                "key": key,
                "key_existed": key_existed,
            },
            witness_data=old_val,
            inverse_fn=lambda s, w: _inverse_fn(old_val, None),
        )
        ctx.active_recovery_ops.append(op)

        return {"key": key, "old_value": old_val, "new_value": str(value)}


def json_config(manifest_path: str) -> JSONConfigDriver:
    """Convenience context manager constructor for JSON configuration files."""
    return JSONConfigDriver(manifest_path)
