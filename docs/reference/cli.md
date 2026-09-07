# Command Line Interface (CLI) Reference

EvoUndo provides a developer CLI for initializing workspaces, inspecting recorded mutations, running recoverability verifications, and launching the local Studio UI.

---

## Commands

### `evoundo init`
Initialize an EvoUndo workspace in the current directory.

```bash
evoundo init
```
Creates `.evoundo/` and configures local registry and journal paths.

---

### `evoundo status`
Show current workspace health, active mutations count, and storage status.

```bash
evoundo status
```

---

### `evoundo mutations`
List recorded mutations in the registry.

```bash
evoundo mutations [--status ACTIVE|REVERTED] [--limit 20]
```

---

### `evoundo inspect`
Display full metadata, pre-state witness, declared effects, and lineage for a specific mutation.

```bash
evoundo inspect <mutation-id>
```

---

### `evoundo verify`
Verify the physical recoverability and conflict feasibility of a mutation without executing rollback.

```bash
evoundo verify <mutation-id>
```

---

### `evoundo undo`
Execute selective rollback for a specified mutation.

```bash
evoundo undo <mutation-id> [--reason "Optional audit reason"]
```

---

### `evoundo studio`
Launch the local Studio UI web interface.

```bash
evoundo studio [--port 8923] [--host 127.0.0.1]
```

---

### `evoundo wrap`
Wrap and launch an external agent module or script with EvoUndo recovery protection.

```bash
evoundo wrap <agent_module_or_class>
```

---

### `evoundo proxy`
Start the transparent EvoUndo proxy server for agent frameworks and MCP clients.

```bash
evoundo proxy [--port 8000] [--mcp]
```
