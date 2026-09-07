"""Witness data structures and storage engines."""

from __future__ import annotations
import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Witness:
    """Strongly-typed pre-mutation state witness $w$ captured for recovery.
    
    Contains strictly the recovery-relevant prior state elements for declared targets.
    """
    mutation_id: str
    tenant_id: str = "default"
    data: Dict[str, Any] = field(default_factory=dict)
    schema_name: Optional[str] = None
    version: int = 1
    timestamp: float = field(default_factory=time.time)
    secret_references: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "tenant_id": self.tenant_id,
            "data": self.data,
            "schema_name": self.schema_name,
            "version": self.version,
            "timestamp": self.timestamp,
            "secret_references": self.secret_references,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Witness:
        return cls(
            mutation_id=d.get("mutation_id", ""),
            tenant_id=d.get("tenant_id", "default"),
            data=d.get("data", {}),
            schema_name=d.get("schema_name"),
            version=d.get("version", 1),
            timestamp=d.get("timestamp", time.time()),
            secret_references=d.get("secret_references", {}),
            metadata=d.get("metadata", {}),
        )


class BaseWitnessStore:
    """Base interface for witness persistence."""

    def save_witness(self, witness: Witness) -> None:
        raise NotImplementedError

    def get_witness(self, mutation_id: str) -> Optional[Witness]:
        raise NotImplementedError

    def delete_witness(self, mutation_id: str) -> bool:
        raise NotImplementedError


class InMemoryWitnessStore(BaseWitnessStore):
    """In-memory witness repository."""

    def __init__(self) -> None:
        self._store: Dict[str, Witness] = {}

    def save_witness(self, witness: Witness) -> None:
        self._store[witness.mutation_id] = copy.deepcopy(witness)

    def get_witness(self, mutation_id: str) -> Optional[Witness]:
        w = self._store.get(mutation_id)
        return copy.deepcopy(w) if w else None

    def delete_witness(self, mutation_id: str) -> bool:
        if mutation_id in self._store:
            del self._store[mutation_id]
            return True
        return False
