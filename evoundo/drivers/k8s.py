"""Kubernetes Surface Driver for deployment manifests and live cluster synchronization."""

from __future__ import annotations
import datetime
import json
import os
import shutil
from typing import Any, Dict, Optional

from evoundo.context import get_current_context


class KubernetesDriver:
    """Manages Kubernetes manifest applications and rollbacks."""

    @staticmethod
    def deploy(manifest_path: str, live_path: str) -> Dict[str, Any]:
        """Deploy manifest to live cluster controller with automatic snapshot inverse."""
        # 1. Capture pre-state snapshot of live deployment
        prior_snapshot = None
        if os.path.exists(live_path):
            with open(live_path, "r", encoding="utf-8") as f:
                prior_snapshot = f.read()

        # 2. Apply new manifest
        os.makedirs(os.path.dirname(os.path.abspath(live_path)), exist_ok=True)
        shutil.copyfile(manifest_path, live_path)

        with open(live_path, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)

        # 3. Construct inverse rollback
        def _inverse_fn(witness, result):
            if witness is not None:
                with open(live_path, "w", encoding="utf-8") as f:
                    f.write(witness)
            elif os.path.exists(live_path):
                os.remove(live_path)

        # 4. Attach to active EvoUndo context
        ctx = get_current_context()
        target_key = ctx.active_target or "k8s:manifest"
        ctx.active_witness[target_key] = prior_snapshot
        ctx.active_inverse_fn = _inverse_fn

        from evoundo.recovery.operations import DriverRecoveryOp
        op = DriverRecoveryOp(
            driver_type="k8s",
            target=target_key,
            operation="deploy",
            parameters={
                "live_path": os.path.abspath(live_path),
                "manifest_path": os.path.abspath(manifest_path),
            },
            witness_data=prior_snapshot,
            inverse_fn=lambda s, w: _inverse_fn(prior_snapshot, None),
        )
        ctx.active_recovery_ops.append(op)

        return {
            "applied": True,
            "manifest_file": manifest_path,
            "replicas": manifest_data.get("service", {}).get("replicas", 1),
            "applied_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }


def k8s_deploy(manifest_path: str, live_path: str) -> Dict[str, Any]:
    """Convenience functional helper for Kubernetes manifest deployments."""
    return KubernetesDriver.deploy(manifest_path=manifest_path, live_path=live_path)
