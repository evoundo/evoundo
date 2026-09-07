# Model Context Protocol (MCP) Integration

The Model Context Protocol (MCP) standardizes how LLMs interact with local and remote tools, prompts, and resources. EvoUndo provides an intercepting proxy layer for MCP server-client exchanges.

---

## 1. Installation

```bash
pip install "evoundo[mcp]"
```

---

## 2. Protecting MCP Tool Invocations

When an MCP client issues a `tools/call` JSON-RPC request to a server, EvoUndo wraps the request with mutation tracking and recovery envelopes:

```python
from evoundo.integrations.mcp_proxy import MCPProxyHandler
from evoundo import protect_tool

proxy = MCPProxyHandler()

# Register protection rules for MCP server tool namespaces
@proxy.protect_mcp_tool("filesystem/write_file")
@protect_tool(
    target="file://{path}",
    capture_fn=lambda path, content: Path(path).read_text() if Path(path).exists() else "",
    inverse_fn=lambda witness, result, path, **kwargs: Path(path).write_text(witness),
)
def handle_mcp_write_file(path: str, content: str):
    Path(path).write_text(content)
    return {"content": [{"type": "text", "text": f"Wrote {len(content)} bytes"}]}
```

---

## 3. Remote Tool Call Interception

With the proxy active:
1. Incoming `tools/call` JSON-RPC messages are inspected.
2. The MCP client request ID is bound to the logical mutation identity.
3. If an MCP client retries a timed-out call, EvoUndo returns the cached JSON-RPC tool result, preventing duplicate writes to external services.
