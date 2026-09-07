"""EvoUndo: Generic Payload Protection, Redaction, and Customization Hook."""

from __future__ import annotations
import re
from typing import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class PayloadProtector(Protocol):
    """Protocol defining serialization security, encryption, and redaction."""

    def protect(self, payload: Any) -> Any:
        """Transform payload for safe persistent storage."""
        ...

    def unprotect(self, protected_payload: Any) -> Any:
        """Reconstruct original payload from storage representation."""
        ...

    def redact(self, payload: Any) -> Any:
        """Mask sensitive credentials or secrets in payload."""
        ...


class DefaultPayloadProtector:
    """Default payload protector providing transparent passthrough serialization with local credential redaction."""

    SENSITIVE_PATTERNS = [
        re.compile(r"password", re.IGNORECASE),
        re.compile(r"secret", re.IGNORECASE),
        re.compile(r"token", re.IGNORECASE),
        re.compile(r"api[_-]?key", re.IGNORECASE),
    ]

    def protect(self, payload: Any) -> Any:
        return payload

    def unprotect(self, protected_payload: Any) -> Any:
        return protected_payload

    def redact(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            redacted = {}
            for k, v in payload.items():
                if any(p.search(str(k)) for p in self.SENSITIVE_PATTERNS):
                    redacted[k] = "[REDACTED]"
                elif isinstance(v, (dict, list)):
                    redacted[k] = self.redact(v)
                else:
                    redacted[k] = v
            return redacted
        elif isinstance(payload, list):
            return [self.redact(item) for item in payload]
        return payload


_CURRENT_PAYLOAD_PROTECTOR: PayloadProtector = DefaultPayloadProtector()


def get_payload_protector() -> PayloadProtector:
    """Get the active global PayloadProtector."""
    return _CURRENT_PAYLOAD_PROTECTOR


def set_payload_protector(protector: PayloadProtector) -> None:
    """Set the active global PayloadProtector customization hook."""
    global _CURRENT_PAYLOAD_PROTECTOR
    _CURRENT_PAYLOAD_PROTECTOR = protector

