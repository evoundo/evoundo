"""Canonical persistence paths and data directory layout for EvoUndo."""

from __future__ import annotations
import os


def get_canonical_data_dir() -> str:
    """Resolve the canonical EvoUndo data directory (.evoundo or ~/.evoundo)."""
    if "EVOUNDO_DATA_DIR" in os.environ:
        return os.path.abspath(os.environ["EVOUNDO_DATA_DIR"])
    if os.path.exists(".evoundo"):
        return os.path.abspath(".evoundo")
    return os.path.expanduser("~/.evoundo")


def get_canonical_registry_path() -> str:
    """Resolve canonical path to mutation registry file."""
    if "EVOUNDO_REGISTRY_PATH" in os.environ:
        return os.path.abspath(os.environ["EVOUNDO_REGISTRY_PATH"])
    return os.path.join(get_canonical_data_dir(), "registry.json")


def get_canonical_journal_path() -> str:
    """Resolve canonical path to reconciliation write-ahead journal."""
    if "EVOUNDO_JOURNAL_PATH" in os.environ:
        return os.path.abspath(os.environ["EVOUNDO_JOURNAL_PATH"])
    journal_dir = os.environ.get("EVOUNDO_JOURNAL_DIR", get_canonical_data_dir())
    return os.path.join(journal_dir, "journal.json")
