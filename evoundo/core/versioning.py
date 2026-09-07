"""Harness version lineage graph and version transition tracking."""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class VersionNode:
    """Node in the harness version lineage graph."""
    version: int
    parent_version: Optional[int]
    mutation_id: Optional[str]
    timestamp: float = field(default_factory=time.time)
    state_hash: str = ""
    description: str = ""
    status: str = "ACTIVE"  # ACTIVE, REVERTED, BRANCHED
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "parent_version": self.parent_version,
            "mutation_id": self.mutation_id,
            "timestamp": self.timestamp,
            "state_hash": self.state_hash,
            "description": self.description,
            "status": self.status,
            "metadata": self.metadata,
        }


class VersionLineageGraph:
    """Manages the version tree and evolution history of an EvoUndo harness."""

    def __init__(self, initial_version: int = 1, initial_hash: str = ""):
        self.nodes: Dict[int, VersionNode] = {}
        self.root_version = initial_version
        self.current_version = initial_version
        self.nodes[initial_version] = VersionNode(
            version=initial_version,
            parent_version=None,
            mutation_id=None,
            state_hash=initial_hash,
            description="Initial baseline harness",
        )

    def append_version(
        self,
        new_version: int,
        parent_version: int,
        mutation_id: str,
        state_hash: str,
        description: str,
    ) -> VersionNode:
        """Register a new admitted harness version."""
        node = VersionNode(
            version=new_version,
            parent_version=parent_version,
            mutation_id=mutation_id,
            state_hash=state_hash,
            description=description,
        )
        self.nodes[new_version] = node
        self.current_version = new_version
        return node

    def get_lineage(self, version: Optional[int] = None) -> List[VersionNode]:
        """Return the ancestor path from root to the specified version."""
        target_v = version if version is not None else self.current_version
        path = []
        curr = self.nodes.get(target_v)
        while curr:
            path.append(curr)
            if curr.parent_version is None:
                break
            curr = self.nodes.get(curr.parent_version)
        return list(reversed(path))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root_version": self.root_version,
            "current_version": self.current_version,
            "nodes": {str(k): v.to_dict() for k, v in self.nodes.items()},
        }
