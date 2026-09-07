"""Pluggable persistence backends for EvoUndo."""

from __future__ import annotations
import os
from typing import Any, Optional

from evoundo.persistence.base import MutationStorageBackend
from evoundo.persistence.json_file import JsonFileStorage
from evoundo.persistence.sqlite import SqliteStorage


def create_storage_backend(
    backend: Optional[str] = None,
    storage_path: Optional[str] = None,
    **kwargs: Any,
) -> MutationStorageBackend:
    """Factory creating the appropriate pluggable mutation storage backend.
    
    Resolution order:
    1. Explicit `backend` parameter ('json' | 'sqlite')
    2. Environment variable `EVOUNDO_STORAGE_BACKEND`
    3. File path extension (.db or .sqlite -> sqlite)
    4. Default: 'json'
    """
    selected = backend or os.environ.get("EVOUNDO_STORAGE_BACKEND")
    if not selected:
        if storage_path and (storage_path.endswith(".db") or storage_path.endswith(".sqlite")):
            selected = "sqlite"
        else:
            selected = "json"

    selected = selected.lower().strip()
    if selected == "sqlite":
        db_path = storage_path or os.environ.get("EVOUNDO_SQLITE_PATH", ".evoundo/evoundo.db")
        return SqliteStorage(db_path=db_path)
    elif selected == "json":
        json_path = storage_path or os.environ.get("EVOUNDO_REGISTRY_PATH")
        return JsonFileStorage(file_path=json_path)
    else:
        raise ValueError(f"Unknown storage backend '{selected}'. Supported backends: 'json', 'sqlite'")


__all__ = [
    "MutationStorageBackend",
    "JsonFileStorage",
    "SqliteStorage",
    "create_storage_backend",
]
