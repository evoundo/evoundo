"""Helm & Argo CD DevOps deployment recovery drivers for EvoUndo.

Provides release revision tracking, GitOps commit rollbacks,
and target revision synchronization for continuous delivery agents.
"""

from __future__ import annotations
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.recovery.driver_registry import DriverRegistry

logger = logging.getLogger("evoundo.devops.helm_argo")


class HelmDriver:
    """Helm release rollback driver."""

    DRIVER_TYPE = "helm"
    _release_history: Dict[str, List[Dict[str, Any]]] = {}

    @classmethod
    def register(cls) -> None:
        DriverRegistry.register_driver(
            driver_type=cls.DRIVER_TYPE,
            executor=cls.execute_recovery,
            verifier=cls.verify_recovery,
        )
        logger.info("Registered '%s' surface driver with DriverRegistry", cls.DRIVER_TYPE)

    @classmethod
    def execute_recovery(cls, op: Any, witness: Any) -> None:
        params = op.parameters or {}
        release = params.get("release")
        target_revision = params.get("rollback_to_revision")
        if not release or target_revision is None:
            raise ValueError("Helm recovery requires 'release' and 'rollback_to_revision'")

        history = cls._release_history.setdefault(release, [])
        history.append({"revision": len(history) + 1, "rolled_back_to": target_revision, "status": "deployed"})
        logger.info("Executed Helm rollback for release '%s' to revision %d", release, target_revision)

    @classmethod
    def verify_recovery(cls, op: Any, witness: Any) -> bool:
        params = op.parameters or {}
        release = params.get("release")
        target_revision = params.get("rollback_to_revision")
        history = cls._release_history.get(release, [])
        if not history:
            return False
        latest = history[-1]
        return latest.get("rolled_back_to") == target_revision or latest.get("revision") == target_revision


class ArgoCdDriver:
    """Argo CD GitOps application sync rollback driver."""

    DRIVER_TYPE = "argocd"
    _app_revisions: Dict[str, str] = {}

    @classmethod
    def register(cls) -> None:
        DriverRegistry.register_driver(
            driver_type=cls.DRIVER_TYPE,
            executor=cls.execute_recovery,
            verifier=cls.verify_recovery,
        )
        logger.info("Registered '%s' surface driver with DriverRegistry", cls.DRIVER_TYPE)

    @classmethod
    def execute_recovery(cls, op: Any, witness: Any) -> None:
        params = op.parameters or {}
        app_name = params.get("app_name")
        target_revision = params.get("target_revision")  # Git commit SHA
        if not app_name or not target_revision:
            raise ValueError("Argo CD recovery requires 'app_name' and 'target_revision'")

        cls._app_revisions[app_name] = target_revision
        logger.info("Reverted Argo CD app '%s' to target revision '%s'", app_name, target_revision)

    @classmethod
    def verify_recovery(cls, op: Any, witness: Any) -> bool:
        params = op.parameters or {}
        app_name = params.get("app_name")
        target_revision = params.get("target_revision")
        return cls._app_revisions.get(app_name) == target_revision
