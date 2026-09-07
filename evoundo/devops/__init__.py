"""EvoUndo DevOps & SRE Recovery Package.

Exposes drivers and verifiers for Kubernetes, Terraform, Helm, Argo CD,
and the Autonomous SRE Agent flagship demo.
"""

from evoundo.devops.kubernetes import K8sSurfaceDriver, K8sDeploymentHealthVerifier
from evoundo.devops.terraform import TerraformDriver
from evoundo.devops.helm_argo import HelmDriver, ArgoCdDriver
from evoundo.devops.flagship_sre_agent import AutonomousSREAgent

__all__ = [
    "K8sSurfaceDriver",
    "K8sDeploymentHealthVerifier",
    "TerraformDriver",
    "HelmDriver",
    "ArgoCdDriver",
    "AutonomousSREAgent",
    "register_all_devops_drivers",
]


def register_all_devops_drivers() -> None:
    K8sSurfaceDriver.register()
    TerraformDriver.register()
    HelmDriver.register()
    ArgoCdDriver.register()


register_all_devops_drivers()
