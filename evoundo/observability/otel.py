"""OpenTelemetry semantic conventions and tracer provider for EvoUndo.

Standardizes trace span hierarchy:
  agent.run
      └── tool.call
              └── evoundo.mutation
                      ├── mutation_id
                      ├── logical_mutation_id
                      ├── platform
                      ├── agent
                      ├── target
                      ├── action_class
                      ├── recovery_level
                      ├── status
                      ├── duplicate_suppressed
                      └── recovery_status
"""

from __future__ import annotations
from contextlib import contextmanager
import logging
import time
from typing import Any, Dict, Generator, Optional

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode, Tracer
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    _HAS_OTEL = True
except ImportError as e:
    _HAS_OTEL = False

logger = logging.getLogger("evoundo.observability.otel")


class EvoUndoTracer:
    """Manages OpenTelemetry tracing for agent runs, tool calls, and EvoUndo mutations."""

    _instance: Optional[EvoUndoTracer] = None

    def __init__(self, service_name: str = "evoundo-service"):
        self.service_name = service_name
        self.in_memory_exporter: Optional[Any] = None
        self.tracer: Optional[Any] = None

        if _HAS_OTEL:
            provider = TracerProvider()
            self.in_memory_exporter = InMemorySpanExporter()
            provider.add_span_processor(SimpleSpanProcessor(self.in_memory_exporter))
            trace.set_tracer_provider(provider)
            self.tracer = trace.get_tracer("evoundo", "0.1.0")
        else:
            logger.info("OpenTelemetry not installed; tracing will run in lightweight fallback mode")

    @classmethod
    def get_instance(cls) -> EvoUndoTracer:
        if cls._instance is None:
            cls._instance = EvoUndoTracer()
        return cls._instance

    @contextmanager
    def start_agent_run(self, agent_id: str, run_id: str, platform: str) -> Generator[Any, None, None]:
        """Start span for high-level agent.run."""
        if not self.tracer:
            yield None
            return

        with self.tracer.start_as_current_span(
            "agent.run",
            attributes={
                "agent.id": agent_id,
                "agent.run_id": run_id,
                "agent.platform": platform,
            },
        ) as span:
            yield span

    @contextmanager
    def start_tool_call(self, tool_name: str, tool_call_id: Optional[str] = None) -> Generator[Any, None, None]:
        """Start span for tool.call under active agent run."""
        if not self.tracer:
            yield None
            return

        with self.tracer.start_as_current_span(
            "tool.call",
            attributes={
                "tool.name": tool_name,
                "tool.call_id": tool_call_id or "",
            },
        ) as span:
            yield span

    @contextmanager
    def start_mutation(
        self,
        mutation_id: str,
        logical_mutation_id: str,
        platform: str,
        agent: str,
        target: str,
        action_class: str = "REVERSIBLE",
        recovery_level: str = "DRIVER",
    ) -> Generator[Any, None, None]:
        """Start span for evoundo.mutation with full semantic attributes."""
        if not self.tracer:
            yield None
            return

        attributes = {
            "evoundo.mutation_id": mutation_id,
            "evoundo.logical_mutation_id": logical_mutation_id,
            "evoundo.platform": platform,
            "evoundo.agent": agent,
            "evoundo.target": target,
            "evoundo.action_class": action_class,
            "evoundo.recovery_level": recovery_level,
            "evoundo.status": "PROPOSED",
            "evoundo.duplicate_suppressed": False,
            "evoundo.recovery_status": "NONE",
        }

        with self.tracer.start_as_current_span("evoundo.mutation", attributes=attributes) as span:
            yield span

    def get_finished_spans(self) -> list:
        """Retrieve exported spans from in-memory exporter for testing and assertions."""
        if self.in_memory_exporter:
            return self.in_memory_exporter.get_finished_spans()
        return []

    def clear(self) -> None:
        if self.in_memory_exporter:
            self.in_memory_exporter.clear()
