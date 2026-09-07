"""LlamaIndex RAG query engine plugin for EvoUndo."""

from __future__ import annotations
import time
from typing import Any, Callable, Dict, List, Optional
from evoundo.core.harness import EvoUndoHarness
from evoundo.studio.runtime import QueryCache


class EvoUndoQueryEngine:
    """Wraps LlamaIndex query engines with EvoUndo caching and recoverability guardrails."""

    def __init__(self, query_fn: Callable[[str], str], harness: Optional[EvoUndoHarness] = None):
        self.query_fn = query_fn
        self.harness = harness or EvoUndoHarness()

    def query(self, query_str: str) -> Dict[str, Any]:
        """Execute query with automatic caching if cache_mw is active."""
        start_time = time.perf_counter()
        has_cache_mw = any(m.id == "cache_mw" for m in self.harness.current_state.middleware)
        cache_ttl = float(self.harness.current_state.config.get("cache_ttl_sec", 300))

        if has_cache_mw:
            cached_val = QueryCache.get(query_str, ttl_sec=cache_ttl)
            if cached_val is not None:
                return {
                    "response": cached_val,
                    "cached": True,
                    "latency_ms": round((time.perf_counter() - start_time) * 1000, 3),
                    "harness_version": self.harness.current_state.version,
                }

        # Fresh retrieval execution
        response_text = self.query_fn(query_str)

        if has_cache_mw:
            QueryCache.set(query_str, response_text)

        return {
            "response": response_text,
            "cached": False,
            "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
            "harness_version": self.harness.current_state.version,
        }
