"""FastAPI and Starlette web application middleware plugin for EvoUndo."""

from __future__ import annotations
import time
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from evoundo.core.harness import EvoUndoHarness


class EvoUndoAPIMiddleware(BaseHTTPMiddleware):
    """FastAPI/Starlette middleware injecting EvoUndo harness headers and request telemetry."""

    def __init__(self, app, harness: Optional[EvoUndoHarness] = None):
        super().__init__(app)
        self.harness = harness or EvoUndoHarness()

    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.perf_counter()
        response = await call_next(request)
        latency_ms = (time.perf_counter() - start_time) * 1000

        # Inject EvoUndo Control Plane headers
        state = self.harness.current_state
        response.headers["X-EvoUndo-Harness-Version"] = str(state.version)
        response.headers["X-EvoUndo-State-Hash"] = state.canonical_hash()[:8]
        response.headers["X-EvoUndo-Latency-Ms"] = f"{latency_ms:.2f}"

        return response
