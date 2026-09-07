# Agent & Framework Integrations

EvoUndo integrates with agent frameworks, execution runtimes, and tool protocols:

### Agent Frameworks
- [**LangGraph**](langgraph.md) — Graph workflows and node-level tool recovery.
- [**OpenAI Agents SDK**](openai.md) — Function calling and automatic schema derivation.
- [**CrewAI**](crewai.md) — Multi-agent crews and task delegation protection.
- [**Model Context Protocol (MCP)**](mcp.md) — Standardized tool server proxying.

### Meta-Harness & Platform Integrations
- [**Databricks Omnigent**](omnigent.md) — Meta-harness execution chaining and runner middleware.
- **Coding Agents & Runtimes** (Claude Code, Cursor, OpenAI Codex, OpenClaw, Hermes) — Protected via `evoundo wrap <agent>` or transparent MCP proxying.
