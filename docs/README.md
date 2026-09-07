# EvoUndo Documentation

Welcome to the EvoUndo documentation portal. Here you will find guides, architectural deep dives, datastore manuals, and API references.

---

## Getting Started

* **[Getting Started Guide](getting-started.md)** — Step-by-step tutorial on wrapping agents with `evoundo.wrap()`, handling failures, and using the local Studio UI.

---

## Core Concepts

Understand the formal foundations of recoverability and crash reconciliation:

* **[Core Recovery Model](concepts/recovery-model.md)** — Action classification taxonomy (`REVERSIBLE`, `COMPENSATABLE`, `RECONCILABLE`, `IRREVERSIBLE`) and mutation lifecycles.
* **[Crash & Retry Reconciliation](concepts/crash-reconciliation.md)** — Analysis of post-commit / pre-ACK failure windows, idempotency, and duplicate execution suppression.
* **[Selective Recovery & Conflict Refusal](concepts/selective-recovery.md)** — Resource-address isolation, causal dependency graphs, and fail-closed conflict detection.
* **[Semantic Compensation](concepts/compensation.md)** — Reversing non-transactional actions, payments, and external APIs.
* **[Memory Consistency](concepts/memory-consistency.md)** — Keeping agent working memory aligned with physical external state after rollbacks.

---

## Datastore Drivers

Dedicated manuals for capturing witnesses, executing inverses, and asserting physical state:

* **[Drivers Overview](drivers/README.md)**
* **[PostgreSQL Driver](drivers/postgres.md)**
* **[Redis Driver](drivers/redis.md)**
* **[SQLite Driver](drivers/sqlite.md)**
* **[MongoDB Driver](drivers/mongodb.md)**

---

## Agent Frameworks & Meta-Harness Integrations

Integration guides for autonomous agent runtimes:

* **[Integrations Overview](integrations/README.md)**
* **[Databricks Omnigent](integrations/omnigent.md)** — Meta-harness execution chaining and runner middleware.
* **[LangGraph](integrations/langgraph.md)** — StateGraph workflows and tool node recovery.
* **[OpenAI Agents SDK](integrations/openai.md)** — Automatic schema extraction and tool calling.
* **[CrewAI](integrations/crewai.md)** — Multi-agent crew delegation and shared state protection.
* **[Model Context Protocol (MCP)](integrations/mcp.md)** — Intercepting mutations via standardized MCP proxies.

---

## Reference Manuals

* **[Python API Reference](reference/api.md)** — Complete module and function signatures for `wrap()`, `@evoundo`, `revert()`, and `@protect_tool`.
* **[CLI Reference](reference/cli.md)** — Command-line syntax for `init`, `wrap`, `proxy`, `undo`, and `studio`.
* **[Local Studio UI](reference/studio.md)** — Guide to the browser-based control plane, diff inspector, and audit proofs.

---

## Security & Performance

* **[Security Architecture & Safeguards](security.md)** — SSRF defense, secret redaction, and SQL injection prevention.
* **[Crash Consistency Matrix](crash-consistency.md)** — 11-boundary fault injection and reconciliation guarantees.
* **[Performance Benchmarks](benchmarks.md)** — Latency percentiles, memory overhead, and disk footprint.
