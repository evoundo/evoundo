"""Strongly-typed harness state and descriptor models for EvoUndo Harness."""

from __future__ import annotations
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


@dataclass
class ToolDescriptor:
    """Descriptor and execution binding for a registered agent tool."""
    name: str
    description: str
    fn: Optional[Callable[..., Any]] = None
    parameters_schema: Dict[str, Any] = field(default_factory=dict)
    version: str = "1.0.0"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        if self.fn is None:
            raise RuntimeError(f"Tool '{self.name}' has no executable function attached.")
        return self.fn(*args, **kwargs)

    def canonical_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters_schema": self.parameters_schema,
            "version": self.version,
            "metadata": self.metadata,
        }


@dataclass(init=False)
class MiddlewareDescriptor:
    """Descriptor and callable for agent execution pipeline middleware."""
    id: str
    name: str
    priority: int
    fn: Optional[Callable[..., Any]]
    enabled: bool
    metadata: Dict[str, Any]

    def __init__(
        self,
        id: Optional[str] = None,
        name: Optional[str] = None,
        priority: int = 100,
        fn: Optional[Callable[..., Any]] = None,
        enabled: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        middleware_id: Optional[str] = None,
    ):
        actual_id = id if id is not None else (middleware_id or "")
        self.id = actual_id
        self.name = name if name is not None else actual_id
        self.priority = priority
        self.fn = fn
        self.enabled = enabled
        self.metadata = metadata if metadata is not None else {}

    @property
    def middleware_id(self) -> str:
        return self.id

    @middleware_id.setter
    def middleware_id(self, val: str) -> None:
        self.id = val

    def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if self.fn and self.enabled:
            return self.fn(context)
        return context

    def canonical_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "priority": self.priority,
            "enabled": self.enabled,
            "metadata": self.metadata,
        }


@dataclass(init=False)
class ListenerDescriptor:
    """Descriptor for an event listener attached to the harness lifecycle."""
    id: str
    event: str
    callback: Optional[Callable[..., Any]]
    priority: int
    metadata: Dict[str, Any]

    def __init__(
        self,
        id: Optional[str] = None,
        event: str = "",
        callback: Optional[Callable[..., Any]] = None,
        priority: int = 100,
        metadata: Optional[Dict[str, Any]] = None,
        listener_id: Optional[str] = None,
    ):
        actual_id = id if id is not None else (listener_id or "")
        self.id = actual_id
        self.event = event
        self.callback = callback
        self.priority = priority
        self.metadata = metadata if metadata is not None else {}

    @property
    def listener_id(self) -> str:
        return self.id

    @listener_id.setter
    def listener_id(self, val: str) -> None:
        self.id = val

    def handle(self, *args: Any, **kwargs: Any) -> Any:
        if self.callback:
            return self.callback(*args, **kwargs)
        return None

    def canonical_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "event": self.event,
            "priority": self.priority,
            "metadata": self.metadata,
        }


@dataclass
class FileDescriptor:
    """Descriptor for a virtual/sandboxed file resource."""
    path: str
    content: str
    mode: str = "text"
    is_deleted: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def canonical_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "content": self.content,
            "mode": self.mode,
            "is_deleted": self.is_deleted,
            "content_hash": self.content_hash,
            "metadata": self.metadata,
        }


@dataclass
class ResourceDescriptor:
    """Descriptor for a managed external or runtime resource."""
    id: str
    resource_type: str
    state: str = "active"  # active, closed, paused
    descriptor: Dict[str, Any] = field(default_factory=dict)
    cleanup_fn: Optional[Callable[..., Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        if self.cleanup_fn and self.state != "closed":
            self.cleanup_fn()
        self.state = "closed"

    def canonical_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "resource_type": self.resource_type,
            "state": self.state,
            "descriptor": self.descriptor,
            "metadata": self.metadata,
        }


@dataclass
class HarnessState:
    """Complete, strongly-typed state of the agent harness across all surfaces."""
    version: int = 1
    parent_version: Optional[int] = None
    config: Dict[str, Any] = field(default_factory=dict)
    tools: Dict[str, ToolDescriptor] = field(default_factory=dict)
    middleware: List[MiddlewareDescriptor] = field(default_factory=list)
    event_listeners: Dict[str, List[ListenerDescriptor]] = field(default_factory=dict)
    files: Dict[str, FileDescriptor] = field(default_factory=dict)
    resources: Dict[str, ResourceDescriptor] = field(default_factory=dict)
    prompts: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def listeners(self) -> Dict[str, List[ListenerDescriptor]]:
        return self.event_listeners

    @listeners.setter
    def listeners(self, val: Dict[str, List[ListenerDescriptor]]) -> None:
        self.event_listeners = val

    def clone(self) -> HarnessState:
        """Create a deep copy of the state while preserving callables."""
        new_state = HarnessState(
            version=self.version,
            parent_version=self.parent_version,
            config=copy.deepcopy(self.config),
            prompts=copy.deepcopy(self.prompts),
            metadata=copy.deepcopy(self.metadata),
        )

        # Clone tools
        for name, tool in self.tools.items():
            new_state.tools[name] = ToolDescriptor(
                name=tool.name,
                description=tool.description,
                fn=tool.fn,
                parameters_schema=copy.deepcopy(tool.parameters_schema),
                version=tool.version,
                metadata=copy.deepcopy(tool.metadata),
            )

        # Clone middleware
        for m in self.middleware:
            new_state.middleware.append(MiddlewareDescriptor(
                id=m.id,
                name=m.name,
                priority=m.priority,
                fn=m.fn,
                enabled=m.enabled,
                metadata=copy.deepcopy(m.metadata),
            ))

        # Clone event listeners
        for evt, listeners in self.event_listeners.items():
            new_state.event_listeners[evt] = [
                ListenerDescriptor(
                    id=l.id,
                    event=l.event,
                    callback=l.callback,
                    priority=l.priority,
                    metadata=copy.deepcopy(l.metadata),
                )
                for l in listeners
            ]

        # Clone files
        for path, f in self.files.items():
            new_state.files[path] = FileDescriptor(
                path=f.path,
                content=f.content,
                mode=f.mode,
                is_deleted=f.is_deleted,
                metadata=copy.deepcopy(f.metadata),
            )

        # Clone resources
        for rid, r in self.resources.items():
            new_state.resources[rid] = ResourceDescriptor(
                id=r.id,
                resource_type=r.resource_type,
                state=r.state,
                descriptor=copy.deepcopy(r.descriptor),
                cleanup_fn=r.cleanup_fn,
                metadata=copy.deepcopy(r.metadata),
            )

        return new_state

    def canonical_dict(self) -> Dict[str, Any]:
        """Produce deterministic, canonical dictionary representation."""
        return {
            "config": self.config,
            "prompts": self.prompts,
            "tools": {k: self.tools[k].canonical_dict() for k in sorted(self.tools.keys())},
            "middleware": [m.canonical_dict() for m in sorted(self.middleware, key=lambda x: (x.priority, x.id))],
            "event_listeners": {
                k: [l.canonical_dict() for l in sorted(v, key=lambda x: (x.priority, x.id))]
                for k, v in sorted(self.event_listeners.items())
            },
            "files": {
                k: self.files[k].canonical_dict()
                for k in sorted(self.files.keys())
                if not self.files[k].is_deleted
            },
            "resources": {
                k: self.resources[k].canonical_dict()
                for k in sorted(self.resources.keys())
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        """Produce JSON-serializable dictionary representation of state."""
        return {
            "version": self.version,
            "parent_version": self.parent_version,
            "config": copy.deepcopy(self.config),
            "prompts": copy.deepcopy(self.prompts),
            "tools": {k: {"name": t.name, "description": t.description, "version": t.version} for k, t in self.tools.items()},
            "middleware": [{"id": m.id, "name": m.name, "priority": m.priority, "enabled": m.enabled} for m in self.middleware],
            "event_listeners": {k: [{"id": l.id, "event": l.event, "priority": l.priority} for l in v] for k, v in self.event_listeners.items()},
            "files": {k: {"path": f.path, "content": f.content, "mode": f.mode, "is_deleted": f.is_deleted} for k, f in self.files.items()},
            "resources": {k: {"id": r.id, "resource_type": r.resource_type, "state": r.state} for k, r in self.resources.items()},
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> HarnessState:
        """Construct strongly-typed HarnessState from serialized dictionary."""
        s = cls(
            version=d.get("version", 1),
            parent_version=d.get("parent_version"),
            config=copy.deepcopy(d.get("config", {})),
            prompts=copy.deepcopy(d.get("prompts", {})),
            metadata=copy.deepcopy(d.get("metadata", {})),
        )
        for name, t_data in d.get("tools", {}).items():
            s.tools[name] = ToolDescriptor(
                name=t_data.get("name", name),
                description=t_data.get("description", ""),
                version=t_data.get("version", 1),
            )
        for m_data in d.get("middleware", []):
            s.middleware.append(MiddlewareDescriptor(
                id=m_data.get("id", ""),
                name=m_data.get("name", ""),
                priority=m_data.get("priority", 100),
                enabled=m_data.get("enabled", True),
            ))
        for evt, l_list in d.get("event_listeners", {}).items():
            s.event_listeners[evt] = [
                ListenerDescriptor(id=l.get("id", ""), event=evt, priority=l.get("priority", 100))
                for l in l_list
            ]
        for path, f_data in d.get("files", {}).items():
            s.files[path] = FileDescriptor(
                path=path,
                content=f_data.get("content", ""),
                mode=f_data.get("mode", "text"),
                is_deleted=f_data.get("is_deleted", False),
            )
        for rid, r_data in d.get("resources", {}).items():
            s.resources[rid] = ResourceDescriptor(
                id=rid,
                resource_type=r_data.get("resource_type", "socket"),
                state=r_data.get("state", "active"),
            )
        return s

    def canonical_hash(self) -> str:
        """Compute stable SHA256 hash of the canonical state."""
        dumped = json.dumps(self.canonical_dict(), sort_keys=True, default=str)
        return hashlib.sha256(dumped.encode("utf-8")).hexdigest()

    # --- Surface Helpers ---

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        self.config[key] = value

    def delete_config(self, key: str) -> bool:
        if key in self.config:
            del self.config[key]
            return True
        return False

    def get_tool(self, name: str) -> Optional[ToolDescriptor]:
        return self.tools.get(name)

    def register_tool(self, tool: Any) -> None:
        if isinstance(tool, dict):
            tool = ToolDescriptor(
                name=tool.get("name", "unnamed"),
                description=tool.get("description", ""),
                parameters_schema=tool.get("parameters_schema", {}),
                version=tool.get("version", "1.0.0"),
            )
        self.tools[tool.name] = tool

    def remove_tool(self, name: str) -> bool:
        if name in self.tools:
            del self.tools[name]
            return True
        return False

    def add_middleware(self, m: Any) -> None:
        if isinstance(m, dict):
            m = MiddlewareDescriptor(
                id=m.get("id", "unnamed_mw"),
                name=m.get("name", m.get("id", "unnamed_mw")),
                priority=m.get("priority", 100),
                enabled=m.get("enabled", True),
            )
        # replace if same id, else append
        self.middleware = [item for item in self.middleware if item.id != m.id]
        self.middleware.append(m)
        self.middleware.sort(key=lambda x: x.priority)

    def remove_middleware(self, m_id: str) -> bool:
        initial_len = len(self.middleware)
        self.middleware = [m for m in self.middleware if m.id != m_id]
        return len(self.middleware) < initial_len

    def get_middleware(self, m_id: str) -> Optional[MiddlewareDescriptor]:
        for m in self.middleware:
            if m.id == m_id:
                return m
        return None

    def add_listener(self, event: str, listener: Any) -> None:
        if isinstance(listener, dict):
            listener = ListenerDescriptor(
                id=listener.get("id", "unnamed_listener"),
                event=listener.get("event", event),
                priority=listener.get("priority", 100),
            )
        if event not in self.event_listeners:
            self.event_listeners[event] = []
        self.event_listeners[event] = [l for l in self.event_listeners[event] if l.id != listener.id]
        self.event_listeners[event].append(listener)
        self.event_listeners[event].sort(key=lambda x: x.priority)

    def remove_listener(self, event: str, listener_id: str) -> bool:
        if event in self.event_listeners:
            initial_len = len(self.event_listeners[event])
            self.event_listeners[event] = [l for l in self.event_listeners[event] if l.id != listener_id]
            return len(self.event_listeners[event]) < initial_len
        return False

    def write_file(self, path: str, content: str, mode: str = "text") -> None:
        self.files[path] = FileDescriptor(path=path, content=content, mode=mode, is_deleted=False)

    def read_file(self, path: str) -> Optional[str]:
        f = self.files.get(path)
        if f and not f.is_deleted:
            return f.content
        return None

    def delete_file(self, path: str) -> bool:
        if path in self.files and not self.files[path].is_deleted:
            self.files[path].is_deleted = True
            return True
        return False

    def register_resource(self, resource: Any) -> None:
        if isinstance(resource, dict):
            resource = ResourceDescriptor(
                id=resource.get("id", "unnamed_resource"),
                resource_type=resource.get("resource_type", "generic"),
            )
        self.resources[resource.id] = resource

    def close_resource(self, resource_id: str) -> bool:
        if resource_id in self.resources:
            self.resources[resource_id].close()
            return True
        return False

    def get_prompt(self, name: str) -> Optional[str]:
        return self.prompts.get(name)

    def set_prompt(self, name: str, template: str) -> None:
        self.prompts[name] = template
