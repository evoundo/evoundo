"""Mutation Identity and Execution Context Model for EvoUndo.

This module provides clear separation between:
1. Physical Execution ID (unique per physical run/retry attempt)
2. Logical Mutation ID (identifies the logical intent of the agent/user)
3. Framework Run / Thread ID (LangGraph thread_id, CrewAI task_id, AutoGen chat_id)
4. Tool Call ID (framework's tool invocation identifier)
5. Retry Attempt (monotonic integer counter)
6. Idempotency Key (semantic reconciliation key)

Disambiguation Rule:
- Framework retries of the SAME (logical_mutation_id, framework_run_id, tool_call_id)
  trigger state inspection and duplicate mutation suppression.
- Two distinct sequential calls (e.g. transfer($10) followed by another transfer($10))
  receive distinct logical_mutation_ids and are both executed independently.
"""

from __future__ import annotations
import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class MutationIdentity:
    """Strongly-typed identity descriptor for a state-changing mutation."""
    logical_mutation_id: str
    execution_id: str = field(default_factory=lambda: f"exec_{uuid.uuid4().hex[:12]}")
    framework: str = "generic"                    # "langgraph", "openai", "crewai", "autogen", "generic"
    framework_run_id: Optional[str] = None        # e.g., thread_id, task_id, run_id
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None            # Framework tool call identifier
    retry_attempt: int = 0
    idempotency_key: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        logical_mutation_id: Optional[str] = None,
        framework: str = "generic",
        framework_run_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        retry_attempt: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "MutationIdentity":
        """Factory creating a new or retry identity with deterministic idempotency key."""
        if not logical_mutation_id:
            # If tool_call_id and framework_run_id exist, create stable logical mutation ID
            if tool_call_id and framework_run_id:
                logical_mutation_id = f"mut_{framework}_{framework_run_id}_{tool_call_id}"
            else:
                logical_mutation_id = f"mut_{uuid.uuid4().hex[:12]}"

        # Compute semantic idempotency key from logical ID + framework context + args
        args_str = json.dumps(tool_args or {}, sort_keys=True, default=str)
        key_raw = f"{framework}:{framework_run_id}:{logical_mutation_id}:{tool_name}:{args_str}"
        idempotency_key = hashlib.sha256(key_raw.encode()).hexdigest()[:16]

        return cls(
            logical_mutation_id=logical_mutation_id,
            framework=framework,
            framework_run_id=framework_run_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            retry_attempt=retry_attempt,
            idempotency_key=idempotency_key,
            metadata=metadata or {},
        )

    def next_retry(self) -> "MutationIdentity":
        """Create a new physical execution attempt for the same logical mutation."""
        return MutationIdentity(
            logical_mutation_id=self.logical_mutation_id,
            execution_id=f"exec_{uuid.uuid4().hex[:12]}",
            framework=self.framework,
            framework_run_id=self.framework_run_id,
            tool_name=self.tool_name,
            tool_call_id=self.tool_call_id,
            retry_attempt=self.retry_attempt + 1,
            idempotency_key=self.idempotency_key,
            metadata=self.metadata.copy(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Union[Dict[str, Any], "MutationIdentity"]) -> "MutationIdentity":
        """Deserialize a MutationIdentity from a dictionary or instance."""
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            return cls(logical_mutation_id=str(data) if data else "")

        logical_id = data.get("logical_mutation_id") or data.get("mutation_id") or ""
        execution_id = data.get("execution_id") or f"exec_{uuid.uuid4().hex[:12]}"
        framework = data.get("framework", "generic")
        framework_run_id = data.get("framework_run_id")
        tool_name = data.get("tool_name")
        tool_call_id = data.get("tool_call_id")
        retry_attempt = data.get("retry_attempt", 0)
        idempotency_key = data.get("idempotency_key")
        created_at = data.get("created_at", time.time())
        metadata = data.get("metadata", {})

        return cls(
            logical_mutation_id=str(logical_id),
            execution_id=str(execution_id),
            framework=str(framework),
            framework_run_id=str(framework_run_id) if framework_run_id is not None else None,
            tool_name=str(tool_name) if tool_name is not None else None,
            tool_call_id=str(tool_call_id) if tool_call_id is not None else None,
            retry_attempt=int(retry_attempt) if retry_attempt is not None else 0,
            idempotency_key=str(idempotency_key) if idempotency_key is not None else None,
            created_at=float(created_at) if created_at is not None else time.time(),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )


def canonicalize_value(val: Any) -> Any:
    """Recursively canonicalize data structures for deterministic hashing."""
    if isinstance(val, dict):
        # Sort keys alphabetically, canonicalize each value, filter internal private keys
        return {
            k: canonicalize_value(v)
            for k, v in sorted(val.items(), key=lambda item: str(item[0]))
            if not str(k).startswith("__")
        }
    elif isinstance(val, (list, tuple)):
        return [canonicalize_value(elem) for elem in val]
    elif isinstance(val, float):
        # Normalize integer-valued floats (e.g. 42.0 -> 42)
        if math.isfinite(val) and val.is_integer():
            return int(val)
        return round(val, 8)
    elif isinstance(val, int) and not isinstance(val, bool):
        return int(val)
    elif isinstance(val, str):
        return val
    elif isinstance(val, (bool, type(None))):
        return val
    else:
        return str(val)


def canonicalize_arguments_json(args: Dict[str, Any]) -> str:
    """Serialize tool arguments into a compact, deterministic canonical JSON string."""
    normalized = canonicalize_value(args)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def hash_canonical_arguments(args: Dict[str, Any]) -> str:
    """Compute SHA-256 hex digest of canonicalized arguments."""
    canonical_str = canonicalize_arguments_json(args)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()[:16]


def derive_canonical_identity(
    framework: str,
    tool_name: str,
    arguments: Dict[str, Any],
    platform_run_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
    step_or_turn_index: Optional[int] = None,
    explicit_logical_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    parent_logical_id: Optional[str] = None,
) -> str:
    """Derive deterministic logical mutation identity.

    Equivalent tool arguments with reordered keys or integer/float variance
    derive IDENTICAL hashes. Distinct steps/turns derive DISTINCT identities.
    """
    if explicit_logical_id:
        return explicit_logical_id

    args_hash = hash_canonical_arguments(arguments)
    canonical_tool = tool_name.strip().lower()

    components = [
        framework.strip().lower(),
        (platform_run_id or "run").strip(),
        (subagent_id or "root").strip(),
        canonical_tool,
        str(step_or_turn_index if step_or_turn_index is not None else ""),
        (tool_call_id or "").strip(),
    ]
    if parent_logical_id:
        components.append(parent_logical_id.strip())
    components.append(args_hash)
    raw = ":".join(components)
    derived_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"mut_{canonical_tool}_{derived_hash}"
