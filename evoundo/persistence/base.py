"""Abstract interface for pluggable EvoUndo mutation storage backends."""

from __future__ import annotations
import abc
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional


class MutationStorageBackend(abc.ABC):
    """Abstract contract for persistent mutation and recovery state stores."""

    @abc.abstractmethod
    def save_mutation(self, record: Any) -> None:
        """Persist or update an admitted mutation record."""
        pass

    @abc.abstractmethod
    def get_mutation(self, mutation_id: str, tenant_id: Optional[str] = None) -> Optional[Any]:
        """Fetch a mutation record by ID with optional tenant isolation."""
        pass

    @abc.abstractmethod
    def list_mutations(
        self,
        status: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Any]:
        """List mutation records filtered by status and tenant."""
        pass

    @abc.abstractmethod
    def update_status(
        self,
        mutation_id: str,
        new_status: str,
        audit_entry: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """Update the lifecycle status of a mutation record."""
        pass

    @contextmanager
    def acquire_lock(self, timeout_sec: float = 10.0) -> Generator[None, None, None]:
        """Context manager providing mutual exclusion across concurrent processes."""
        yield

    def close(self) -> None:
        """Clean up open resources or connections."""
        pass
