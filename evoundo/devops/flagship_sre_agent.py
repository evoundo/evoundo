"""Flagship SRE Demo: Autonomous Kubernetes deployment protection and rollback.

Demonstrates:
  SRE Agent modifies deployment (image tag upgrade to broken image)
  Health check probe fails (pod crashes / 0 replicas ready)
  EvoUndo detects failed health condition
  EvoUndo automatically triggers recovery to restore previous healthy deployment
  Physical external state verification confirms restoration
"""

from __future__ import annotations
import copy
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from evoundo.core.harness import EvoUndoHarness
from evoundo.devops.kubernetes import K8sSurfaceDriver, K8sDeploymentHealthVerifier
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.identity import MutationIdentity
from evoundo.recovery.operations import DriverRecoveryOp, RecoveryProgram
from evoundo.witness.stores import Witness

logger = logging.getLogger("evoundo.devops.flagship_sre")


class AutonomousSREAgent:
    """Autonomous SRE Agent protected by EvoUndo."""

    def __init__(self, harness: Optional[EvoUndoHarness] = None):
        self.harness = harness or EvoUndoHarness()
        self.health_verifier = K8sDeploymentHealthVerifier(harness=self.harness)
        K8sSurfaceDriver.register()

    def deploy_initial_healthy_service(
        self,
        namespace: str = "production",
        name: str = "payment-gateway",
        image: str = "payment-gateway:v2.0-stable",
    ) -> Dict[str, Any]:
        """Establish initial healthy production deployment."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace, "generation": 1},
            "spec": {
                "replicas": 3,
                "template": {
                    "spec": {
                        "containers": [{"name": "app", "image": image, "port": 8080}]
                    }
                },
            },
            "status": {"availableReplicas": 3, "readyReplicas": 3},
        }
        key = f"deployment:{namespace}:{name}"
        K8sSurfaceDriver._mock_cluster_state[key] = copy.deepcopy(manifest)
        logger.info("Initialized healthy deployment %s with image %s", key, image)
        return manifest

    def perform_upgrade_with_evoundo_protection(
        self,
        namespace: str = "production",
        name: str = "payment-gateway",
        new_image: str = "payment-gateway:v2.1-broken",
        health_probe_fn: Optional[Callable[[], bool]] = None,
    ) -> Tuple[str, bool]:
        """SRE Agent applies image update wrapped in EvoUndo recovery envelope.

        Returns: (mutation_id, deployment_survived_healthy)
        """
        key = f"deployment:{namespace}:{name}"
        # 1. Capture pre-mutation manifest witness
        witness_data = K8sSurfaceDriver.capture_manifest_witness("Deployment", namespace, name)
        orig_gen = witness_data.get("generation", 1)

        mutation_id = f"sre_deploy_{int(time.time()*1000)}"

        # 2. Build durable recovery operation
        recovery_op = DriverRecoveryOp(
            driver_type="k8s_resource",
            target=f"k8s://{namespace}/Deployment/{name}",
            operation="APPLY",
            parameters={
                "kind": "Deployment",
                "namespace": namespace,
                "name": name,
                "expected_post_generation": orig_gen + 1,
            },
            witness_data=witness_data,
        )
        prog = RecoveryProgram(operations=[recovery_op])

        # 3. Record mutation with EvoUndo
        identity = MutationIdentity(
            logical_mutation_id=mutation_id,
            framework="sre_agent",
            tool_name="kubectl_apply",
            metadata={"namespace": namespace, "name": name, "image": new_image},
        )
        self.harness.record_external_protected_mutation(
            mutation_id=mutation_id,
            description=f"SRE Agent upgrade {name} to {new_image}",
            witness=Witness(mutation_id=mutation_id, data=witness_data),
            recovery_program=prog,
            declared_effects=[Effect(category=EffectCategory.RESOURCES, target=name, op_type=EffectOpType.UPDATE)],
            identity=identity,
        )

        # 4. SRE Agent modifies the deployment in cluster
        current_cluster_manifest = copy.deepcopy(K8sSurfaceDriver._mock_cluster_state[key])
        current_cluster_manifest["metadata"]["generation"] = orig_gen + 1
        current_cluster_manifest["spec"]["template"]["spec"]["containers"][0]["image"] = new_image
        # If new image is broken, pods crash and availableReplicas drops to 0
        is_broken = "broken" in new_image or "failing" in new_image
        if is_broken:
            current_cluster_manifest["status"] = {"availableReplicas": 0, "readyReplicas": 0}
        else:
            current_cluster_manifest["status"] = {"availableReplicas": 3, "readyReplicas": 3}
        K8sSurfaceDriver._mock_cluster_state[key] = current_cluster_manifest

        # 5. Health check probe
        default_probe = lambda: K8sSurfaceDriver._mock_cluster_state[key].get("status", {}).get("availableReplicas", 0) > 0
        probe = health_probe_fn or default_probe

        # 6. Reconcile health: if probe fails, automatically trigger rollback
        healthy = self.health_verifier.check_and_reconcile(
            mutation_id=mutation_id,
            kind="Deployment",
            namespace=namespace,
            name=name,
            health_check_fn=probe,
        )

        return mutation_id, healthy
