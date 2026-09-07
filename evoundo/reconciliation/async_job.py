"""Asynchronous Long-Running Job Reconciler for EvoUndo.

Manages the complete lifecycle of expensive, long-running asynchronous jobs
(e.g., fine-tuning jobs, large-scale batch evaluations, multi-node training):
  SUBMITTED -> PENDING -> RUNNING -> SUCCEEDED / FAILED / CANCELLED (or UNKNOWN on network loss)

Enforces:
1. Mid-flight retry deduplication: if an agent framework retries an API call while a job is
   RUNNING or PENDING, duplicate submission is suppressed and the existing job descriptor is returned.
2. Terminal cached replay: if a job has already SUCCEEDED, retries return the cached result
   without incurring duplicate cloud/compute spend.
3. Safe external probe reconciliation under network / API failures (UNKNOWN state).
4. Orderly cancellation and terminal state verification.
"""

from __future__ import annotations
from enum import Enum
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("evoundo.reconciliation.async_job")


class JobStatus(str, Enum):
    """Canonical lifecycle states of an asynchronous compute/cloud job."""
    SUBMITTED = "SUBMITTED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass
class AsynchronousJobDescriptor:
    """Represents a tracked asynchronous execution unit."""
    logical_job_id: str
    external_job_id: Optional[str] = None
    status: JobStatus = JobStatus.SUBMITTED
    cost_credits: int = 0
    job_spec: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    submitted_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result_payload: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None

    def is_terminal(self) -> bool:
        return self.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "logical_job_id": self.logical_job_id,
            "external_job_id": self.external_job_id,
            "status": self.status.value if isinstance(self.status, JobStatus) else self.status,
            "cost_credits": self.cost_credits,
            "job_spec": dict(self.job_spec),
            "metadata": dict(self.metadata),
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
            "result_payload": dict(self.result_payload) if self.result_payload else None,
            "error_message": self.error_message,
        }


class AsynchronousJobReconciler:
    """Reconciles asynchronous jobs across agent retries and network uncertainty."""

    def __init__(self):
        self._jobs: Dict[str, AsynchronousJobDescriptor] = {}

    def submit(
        self,
        logical_job_id: str,
        job_spec: Dict[str, Any],
        external_submitter: Optional[Callable[[Dict[str, Any]], str]] = None,
        cost_credits: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AsynchronousJobDescriptor:
        """Submit a new asynchronous job or return existing active/completed job."""
        if logical_job_id in self._jobs:
            existing = self._jobs[logical_job_id]
            # If job is already active or succeeded, return existing without re-submitting
            if existing.status in (JobStatus.SUBMITTED, JobStatus.PENDING, JobStatus.RUNNING, JobStatus.SUCCEEDED):
                logger.info(
                    "Job %s already exists in state %s; returning existing descriptor without re-executing",
                    logical_job_id, existing.status.value
                )
                return existing

        external_id = None
        if external_submitter is not None:
            external_id = external_submitter(job_spec)

        now = time.time()
        desc = AsynchronousJobDescriptor(
            logical_job_id=logical_job_id,
            external_job_id=external_id,
            status=JobStatus.SUBMITTED,
            cost_credits=cost_credits,
            job_spec=job_spec,
            metadata=metadata or {},
            submitted_at=now,
            updated_at=now,
        )
        self._jobs[logical_job_id] = desc
        logger.info("Submitted asynchronous job %s (external_id=%s)", logical_job_id, external_id)
        return desc

    def update_status(
        self,
        logical_job_id: str,
        status: JobStatus,
        result_payload: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> AsynchronousJobDescriptor:
        """Manually or callback-driven status transition."""
        desc = self._jobs.get(logical_job_id)
        if not desc:
            raise KeyError(f"Job '{logical_job_id}' not found in reconciler")

        desc.status = status
        desc.updated_at = time.time()
        if result_payload is not None:
            desc.result_payload = result_payload
        if error_message is not None:
            desc.error_message = error_message

        logger.debug("Updated job %s status to %s", logical_job_id, status.value)
        return desc

    def probe(
        self,
        logical_job_id: str,
        external_probe: Optional[Callable[[str], Tuple[JobStatus, Optional[Dict[str, Any]], Optional[str]]]] = None,
    ) -> AsynchronousJobDescriptor:
        """Probe external system for current status of logical_job_id."""
        desc = self._jobs.get(logical_job_id)
        if not desc:
            raise KeyError(f"Job '{logical_job_id}' not found in reconciler")

        if external_probe is not None:
            try:
                ext_status, payload, err = external_probe(desc.external_job_id or logical_job_id)
                desc.status = ext_status
                if payload is not None:
                    desc.result_payload = payload
                if err is not None:
                    desc.error_message = err
                desc.updated_at = time.time()
            except Exception as e:
                logger.warning("External probe failed for job %s: %s; setting state to UNKNOWN", logical_job_id, e)
                desc.status = JobStatus.UNKNOWN
                desc.error_message = f"Probe failed: {e}"
                desc.updated_at = time.time()

        return desc

    def reconcile(
        self,
        logical_job_id: str,
        retry_spec: Dict[str, Any],
        external_probe: Optional[Callable[[str], Tuple[JobStatus, Optional[Dict[str, Any]], Optional[str]]]] = None,
    ) -> Tuple[bool, AsynchronousJobDescriptor]:
        """Reconcile a potential retry attempt against current external reality.
        
        Returns:
            (duplicate_suppressed: bool, descriptor: AsynchronousJobDescriptor)
            
        - If RUNNING, PENDING, or SUBMITTED: duplicate_suppressed = True (re-execution blocked).
        - If SUCCEEDED: duplicate_suppressed = True (cached result returned).
        - If FAILED or CANCELLED: duplicate_suppressed = False (caller may re-submit).
        - If UNKNOWN: duplicate_suppressed = True (fail-safe: do not launch duplicate expensive compute).
        """
        desc = self._jobs.get(logical_job_id)
        if not desc:
            # Not found: submit fresh job
            new_desc = self.submit(logical_job_id, retry_spec)
            return False, new_desc

        # Refresh state via probe if probe function available
        if external_probe is not None:
            self.probe(logical_job_id, external_probe)

        if desc.status in (JobStatus.SUBMITTED, JobStatus.PENDING, JobStatus.RUNNING):
            logger.info(
                "Reconcile: Suppressed duplicate submission for job %s currently in %s",
                logical_job_id, desc.status.value
            )
            return True, desc

        if desc.status == JobStatus.SUCCEEDED:
            logger.info("Reconcile: Job %s already SUCCEEDED; returning cached result", logical_job_id)
            return True, desc

        if desc.status == JobStatus.UNKNOWN:
            logger.warning(
                "Reconcile: Job %s is in UNKNOWN state. Suppressing duplicate execution to prevent duplicate spend",
                logical_job_id
            )
            return True, desc

        # Status is FAILED or CANCELLED
        logger.info("Reconcile: Job %s is %s; allowing re-submission", logical_job_id, desc.status.value)
        return False, desc

    def cancel(
        self,
        logical_job_id: str,
        external_canceller: Optional[Callable[[str], bool]] = None,
        reason: str = "Cancelled by user/agent",
    ) -> bool:
        """Cancel a running or pending asynchronous job."""
        desc = self._jobs.get(logical_job_id)
        if not desc:
            raise KeyError(f"Job '{logical_job_id}' not found in reconciler")

        if desc.status == JobStatus.SUCCEEDED:
            logger.warning("Cannot cancel job %s: already SUCCEEDED", logical_job_id)
            return False

        if desc.status == JobStatus.CANCELLED:
            return True

        if external_canceller is not None:
            try:
                ok = external_canceller(desc.external_job_id or logical_job_id)
                if not ok:
                    logger.warning("External cancellation returned False for job %s", logical_job_id)
            except Exception as e:
                logger.error("External cancellation failed for job %s: %s", logical_job_id, e)
                desc.error_message = f"Cancellation failed: {e}"
                return False

        desc.status = JobStatus.CANCELLED
        desc.error_message = reason
        desc.updated_at = time.time()
        logger.info("Cancelled asynchronous job %s (reason: %s)", logical_job_id, reason)
        return True

    def poll_until_terminal(
        self,
        logical_job_id: str,
        timeout_sec: float = 5.0,
        interval_sec: float = 0.05,
        external_probe: Optional[Callable[[str], Tuple[JobStatus, Optional[Dict[str, Any]], Optional[str]]]] = None,
    ) -> AsynchronousJobDescriptor:
        """Synchronously poll until job enters a terminal status (SUCCEEDED, FAILED, CANCELLED)."""
        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            desc = self.probe(logical_job_id, external_probe)
            if desc.is_terminal():
                return desc
            time.sleep(interval_sec)

        raise TimeoutError(
            f"Job '{logical_job_id}' did not reach terminal state within {timeout_sec:.2f}s (status: {self._jobs[logical_job_id].status.value})"
        )

    def get_job(self, logical_job_id: str) -> Optional[AsynchronousJobDescriptor]:
        return self._jobs.get(logical_job_id)

    def clear(self) -> None:
        self._jobs.clear()
