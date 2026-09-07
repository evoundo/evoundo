"""Immutable Artifact Ledger (Gap 14 Formalization).

Protects user-submitted or student-submitted work from destructive in-place mutations.
Raw artifact payloads are stored with SHA-256 integrity hashes in an append-only ledger.
Evaluations or grading updates create forward-pointing metadata records referencing
the immutable original hash, rejecting in-place edits to raw work.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("evoundo.core.artifact")


class ImmutableArtifactError(Exception):
    """Base exception for immutable artifact operations."""


class ImmutableArtifactModificationError(ImmutableArtifactError):
    """Raised when an in-place modification of an immutable artifact is attempted."""


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    author_id: str
    content: str
    content_hash: str
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ArtifactEvaluation:
    evaluation_id: str
    artifact_id: str
    content_hash: str
    score: Any
    feedback: Optional[str] = None
    evaluator_id: str = "system"
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class ImmutableArtifactLedger:
    """Append-only store for immutable student/user artifacts with SHA-256 integrity."""

    def __init__(self) -> None:
        self._artifacts: Dict[str, ArtifactRecord] = {}
        self._evaluations: Dict[str, List[ArtifactEvaluation]] = {}

    @staticmethod
    def compute_hash(content: str | bytes) -> str:
        if isinstance(content, str):
            content = content.encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def store_artifact(
        self,
        artifact_id: str,
        author_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRecord:
        """Store an immutable artifact. Re-storing identical content is idempotent; modifying content is rejected."""
        content_hash = self.compute_hash(content)

        if artifact_id in self._artifacts:
            existing = self._artifacts[artifact_id]
            if existing.content_hash != content_hash:
                raise ImmutableArtifactModificationError(
                    f"Cannot modify immutable artifact {artifact_id}! "
                    f"Existing hash {existing.content_hash[:10]}... != new hash {content_hash[:10]}..."
                )
            return existing

        record = ArtifactRecord(
            artifact_id=artifact_id,
            author_id=author_id,
            content=content,
            content_hash=content_hash,
            metadata=metadata or {},
        )
        self._artifacts[artifact_id] = record
        self._evaluations[artifact_id] = []
        logger.info("Stored immutable artifact %s (author=%s, hash=%s)", artifact_id, author_id, content_hash[:8])
        return record

    def get_artifact(self, artifact_id: str) -> Optional[ArtifactRecord]:
        """Retrieve an artifact by ID."""
        return self._artifacts.get(artifact_id)

    def verify_integrity(self, artifact_id: str) -> bool:
        """Verify the cryptographic integrity of an artifact record."""
        record = self.get_artifact(artifact_id)
        if not record:
            return False
        return self.compute_hash(record.content) == record.content_hash

    def record_evaluation(
        self,
        artifact_id: str,
        evaluation_id: str,
        score: Any,
        feedback: Optional[str] = None,
        evaluator_id: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ArtifactEvaluation:
        """Record a forward-pointing evaluation against the immutable artifact."""
        record = self.get_artifact(artifact_id)
        if not record:
            raise ImmutableArtifactError(f"Artifact {artifact_id} not found in ledger")

        eval_rec = ArtifactEvaluation(
            evaluation_id=evaluation_id,
            artifact_id=artifact_id,
            content_hash=record.content_hash,
            score=score,
            feedback=feedback,
            evaluator_id=evaluator_id,
            metadata=metadata or {},
        )
        self._evaluations[artifact_id].append(eval_rec)
        logger.info("Recorded evaluation %s for artifact %s (score=%s)", evaluation_id, artifact_id, score)
        return eval_rec

    def get_evaluations(self, artifact_id: str) -> List[ArtifactEvaluation]:
        """Retrieve all evaluations associated with an artifact."""
        return list(self._evaluations.get(artifact_id, []))
