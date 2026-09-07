"""Canonical Hierarchical Resource Identity (ResourceAddress) for EvoUndo.

Provides structured hierarchical resource addressing and conflict reasoning:
    scheme / domain / entity_id / attribute...
    e.g. postgres/users/42/tier
         mysql/accounts/991/balance
         redis/customer:42/preferences/email
         file/repo/app/config.py
         k8s/default/deployment/api/image

Supports formal hierarchical conflict detection:
- Disjoint entities (users/42 vs users/43): NO conflict
- Sibling fields (users/42/tier vs users/42/balance): NO conflict (independent fields)
- Exact matches (users/42/tier vs users/42/tier): CONFLICT
- Ancestor / Descendant overlap (users/42 vs users/42/tier): CONFLICT (subsumption)
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class ResourceAddress:
    """Immutable, canonical hierarchical resource address."""
    scheme: str
    segments: Tuple[str, ...]

    @property
    def canonical_uri(self) -> str:
        """Return standardized canonical string representation."""
        return f"{self.scheme}://{'/'.join(self.segments)}"

    @property
    def path(self) -> str:
        """Return path without scheme: domain/entity/attribute."""
        return "/".join(self.segments)

    @classmethod
    def parse(cls, raw: str) -> ResourceAddress:
        """Parse a URI or slash-delimited address into a ResourceAddress."""
        if not raw or not isinstance(raw, str):
            return cls(scheme="unknown", segments=())

        clean = raw.strip()
        # Handle scheme://... or scheme/...
        if "://" in clean:
            scheme, rest = clean.split("://", 1)
        elif "/" in clean:
            parts = clean.split("/", 1)
            # Check if first part looks like a known scheme
            known_schemes = {"postgres", "postgresql", "mysql", "redis", "file", "git", "k8s", "kubernetes", "s3", "mongo", "mongodb", "mssql", "sqlserver", "config"}
            if parts[0].lower() in known_schemes:
                scheme = parts[0].lower()
                rest = parts[1]
            else:
                scheme = "general"
                rest = clean
        else:
            return cls(scheme="general", segments=(clean,))

        # Normalize scheme
        scheme = scheme.lower()
        if scheme == "postgresql":
            scheme = "postgres"
        elif scheme == "kubernetes":
            scheme = "k8s"
        elif scheme in ("sqlserver", "azure_sql"):
            scheme = "mssql"

        # Split rest into non-empty segments, stripping leading/trailing slashes
        raw_segs = [s.strip() for s in rest.split("/") if s.strip()]
        return cls(scheme=scheme, segments=tuple(raw_segs))

    @classmethod
    def from_string(cls, raw: str) -> ResourceAddress:
        return cls.parse(raw)

    def is_exact(self, other: ResourceAddress) -> bool:
        """True if both addresses have identical scheme and segments."""
        return self.scheme == other.scheme and self.segments == other.segments

    def is_ancestor_of(self, other: ResourceAddress) -> bool:
        """True if self is a proper prefix/parent of other (e.g. users/42 is ancestor of users/42/tier)."""
        if self.scheme != other.scheme:
            return False
        if len(self.segments) >= len(other.segments):
            return False
        return other.segments[:len(self.segments)] == self.segments

    def is_descendant_of(self, other: ResourceAddress) -> bool:
        """True if self is a proper subpath/child of other."""
        return other.is_ancestor_of(self)

    def is_sibling_of(self, other: ResourceAddress) -> bool:
        """True if self and other share the same parent path but differ in leaf attribute.
        
        Example: users/42/tier is sibling of users/42/balance.
        """
        if self.scheme != other.scheme:
            return False
        if len(self.segments) != len(other.segments) or len(self.segments) < 2:
            return False
        return (self.segments[:-1] == other.segments[:-1]) and (self.segments[-1] != other.segments[-1])

    def is_disjoint(self, other: ResourceAddress) -> bool:
        """True if addresses diverge before the leaf level (e.g. users/42 vs users/43)."""
        if self.scheme != other.scheme:
            return True
        min_len = min(len(self.segments), len(other.segments))
        if min_len <= 1:
            return self.segments != other.segments
        # Diverges at table/domain or entity_id
        return self.segments[:min_len - 1] != other.segments[:min_len - 1]

    def conflicts_with(self, other: ResourceAddress) -> Optional[str]:
        """Evaluate hierarchical conflict between two resource addresses.
        
        Returns error reason string if conflict detected, or None if safe/orthogonal.
        """
        if self.scheme != other.scheme:
            return None

        # 1. Exact collision on identical target
        if self.is_exact(other):
            return f"Exact resource collision on '{self.canonical_uri}'"

        # 2. Ancestor / Descendant subsumption
        if self.is_ancestor_of(other):
            return f"Parent container '{self.canonical_uri}' subsumes downstream modification on '{other.canonical_uri}'"

        if self.is_descendant_of(other):
            return f"Resource attribute '{self.canonical_uri}' conflicts with parent modification on '{other.canonical_uri}'"

        # 3. Sibling fields on same entity: ORTHOGONAL! (Selective rollback safe)
        if self.is_sibling_of(other):
            return None

        # 4. Disjoint entities (different IDs/rows/files): ORTHOGONAL!
        if self.is_disjoint(other):
            return None

        # Fallback conflict if any unhandled overlap
        return f"Resource '{self.canonical_uri}' overlaps with '{other.canonical_uri}'"

    def __str__(self) -> str:
        return self.canonical_uri

    def __repr__(self) -> str:
        return f"ResourceAddress({self.canonical_uri})"
