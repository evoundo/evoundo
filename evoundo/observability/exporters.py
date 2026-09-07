"""EvoUndo Observability Exporters.

Transforms OpenTelemetry mutation spans into native schemas for:
- Datadog (APM tags, metrics)
- Grafana / Prometheus (counter & histogram time series)
- Splunk (HEC audit log events)
- New Relic (distributed trace spans)
- Sentry (breadcrumbs & error captures)
"""

from __future__ import annotations
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("evoundo.observability.exporters")


class DatadogExporter:
    """Formats EvoUndo telemetry for Datadog APM & Metrics."""

    @staticmethod
    def format_span(span_data: Dict[str, Any]) -> Dict[str, Any]:
        attrs = span_data.get("attributes", {})
        return {
            "trace_id": span_data.get("trace_id", "0"),
            "span_id": span_data.get("span_id", "0"),
            "name": "evoundo.mutation",
            "service": "evoundo-agent-harness",
            "resource": attrs.get("evoundo.target", "unknown_resource"),
            "meta": {
                "platform": attrs.get("evoundo.platform", ""),
                "agent": attrs.get("evoundo.agent", ""),
                "mutation_id": attrs.get("evoundo.mutation_id", ""),
                "action_class": attrs.get("evoundo.action_class", ""),
                "status": attrs.get("evoundo.status", ""),
            },
            "metrics": {
                "duplicate_suppressed": 1 if attrs.get("evoundo.duplicate_suppressed") else 0,
            },
        }


class GrafanaMetricsExporter:
    """Formats EvoUndo mutation counters and latencies for Prometheus/Grafana."""

    _counters: Dict[str, int] = {
        "evoundo_mutations_total": 0,
        "evoundo_recoveries_total": 0,
        "evoundo_duplicates_suppressed_total": 0,
        "evoundo_conflicts_refused_total": 0,
    }

    @classmethod
    def record_mutation(cls, status: str, duplicate_suppressed: bool = False) -> None:
        cls._counters["evoundo_mutations_total"] += 1
        if duplicate_suppressed:
            cls._counters["evoundo_duplicates_suppressed_total"] += 1
        if status == "REVERTED":
            cls._counters["evoundo_recoveries_total"] += 1
        elif status == "CONFLICT_DETECTED":
            cls._counters["evoundo_conflicts_refused_total"] += 1

    @classmethod
    def get_metrics_payload(cls) -> str:
        """Render Prometheus exposition format."""
        lines = []
        for k, v in cls._counters.items():
            lines.append(f"# TYPE {k} counter")
            lines.append(f"{k} {v}")
        return "\n".join(lines)


class SplunkAuditExporter:
    """Formats EvoUndo audit logs for Splunk HTTP Event Collector (HEC)."""

    @staticmethod
    def format_event(mutation_id: str, action: str, details: Dict[str, Any]) -> str:
        payload = {
            "time": time.time(),
            "sourcetype": "evoundo:audit",
            "source": "evoundo-harness",
            "event": {
                "mutation_id": mutation_id,
                "action": action,
                **details,
            },
        }
        return json.dumps(payload)


class SentryErrorAdapter:
    """Captures breadcrumbs and exceptions for Sentry integration."""

    _breadcrumbs: List[Dict[str, Any]] = []

    @classmethod
    def add_breadcrumb(cls, message: str, category: str = "evoundo", level: str = "info", data: Optional[Dict[str, Any]] = None) -> None:
        cls._breadcrumbs.append({
            "timestamp": time.time(),
            "category": category,
            "message": message,
            "level": level,
            "data": data or {},
        })
