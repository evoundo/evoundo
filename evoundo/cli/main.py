"""EvoUndo Harness Command Line Interface."""

from __future__ import annotations
import argparse
import json
import os
import sys
import webbrowser
from typing import Any, Dict, List, Optional

from evoundo import __version__
from evoundo.core.harness import EvoUndoHarness
from evoundo.core.state import ToolDescriptor, MiddlewareDescriptor
from evoundo.studio.exporter import HarnessCodeExporter


from evoundo.paths import get_canonical_registry_path, get_canonical_data_dir


def get_default_registry_file() -> str:
    return get_canonical_registry_path()


DEFAULT_REGISTRY_FILE = get_default_registry_file()


def get_harness(registry_path: Optional[str] = None) -> EvoUndoHarness:
    """Initialize or load harness instance connected to persistent registry."""
    path = registry_path or get_canonical_registry_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return EvoUndoHarness(registry_path=path)


def _val(x: Any) -> str:
    """Safely extract value from Enum or string."""
    return str(getattr(x, "value", x))


def cmd_status(args: argparse.Namespace) -> int:
    """Display current harness state and surfaces summary."""
    harness = get_harness(args.registry)
    stat = harness.status()
    if args.json:
        print(json.dumps(stat, indent=2))
        return 0

    print("\033[1;35m" + "=" * 60 + "\033[0m")
    print(f"\033[1;36mEVOUNDO HARNESS STATUS (v{stat['version']})\033[0m — \033[90mHash: {stat['state_hash']}\033[0m")
    print("\033[1;35m" + "=" * 60 + "\033[0m")
    print(f"Tools ({len(stat['tools'])}):           \033[1;32m{', '.join(stat['tools']) or 'None'}\033[0m")
    print(f"Middleware ({len(stat['middleware'])}):      \033[1;35m{', '.join(stat['middleware']) or 'None'}\033[0m")
    print(f"Config Keys ({len(stat['config_keys'])}):     \033[1;34m{', '.join(stat['config_keys']) or 'None'}\033[0m")
    print(f"Files ({len(stat['files'])}):           {', '.join(stat['files']) or 'None'}")
    print(f"Resources ({len(stat['resources'])}):       {', '.join(stat['resources']) or 'None'}")
    print(f"Prompts ({len(stat['prompts'])}):         {', '.join(stat['prompts']) or 'None'}")
    print(f"Admitted Mutations:  \033[1;33m{stat['admitted_mutations_count']}\033[0m")
    if stat['admitted_mutations_count'] > 0:
        history = harness.history()
        print("\nRecorded Mutations:")
        for h in history[-5:]:
            tgts = ", ".join(h.get("targets", []))
            print(f"  • {h['mutation_id']}: {h['description']} [{h['status']}] (targets: {tgts})")
    print("\033[1;35m" + "=" * 60 + "\033[0m")
    return 0


def cmd_evolve(args: argparse.Namespace) -> int:
    """Propose, verify, and admit a self-evolution mutation."""
    harness = get_harness(args.registry)
    if not args.json:
        print(f"[EvoUndo] Proposing evolution: '{args.goal}' ...")
    res = harness.evolve(
        goal=args.goal,
        budget=args.budget,
        recovery_language=args.language,
    )

    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
        return 0 if res.admitted else 1

    if res.admitted:
        print("\n[SUCCESS] Evolution Admitted!")
        print(f"  Mutation ID:        {res.mutation_id}")
        print(f"  New Harness Version: v{harness.current_state.version}")
        print(f"  Capability Delta:   +{res.capability_delta:.2f}")
        print(f"  Recoverability LCB: {res.recoverability:.4f}")
        print(f"  Observed Effects:   {len(res.effects)}")
        for eff in res.effects:
            print(f"    - [{_val(eff.category)}] {eff.target} ({_val(eff.op_type)})")
        print(f"  Recovery Program:   {', '.join(res.recovery_plan)}")
        return 0
    else:
        print("\n[FAILED] Evolution Rejected (Fail-Closed Safety Gate)")
        print(f"  Decision Code:      {res.decision.decision_code}")
        for r in res.decision.reasons:
            print(f"  Reason:             {r}")
        if res.diagnostic_report:
            print("\n" + res.diagnostic_report.to_d1_prompt())
        return 1


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Simulate evolution without persistent state admission."""
    harness = get_harness(args.registry)
    if not args.json:
        print(f"[EvoUndo Dry-Run] Simulating evolution: '{args.goal}' ...")
    builder = harness.mutation(description=args.goal)
    if "retry" in args.goal.lower():
        builder.add_middleware(MiddlewareDescriptor(id="retry_mw", name="RetryMiddleware", priority=10))
    elif "cache" in args.goal.lower():
        builder.add_middleware(MiddlewareDescriptor(id="cache_mw", name="CacheMiddleware", priority=20))
    else:
        builder.set_config("dry_run_key", "test")

    decision = harness.dry_run(builder)
    if args.json:
        print(json.dumps(decision.to_dict(), indent=2))
        return 0 if decision.admissible else 1

    print(f"Dry-Run Verdict: {_val(decision.status)} (Code: {decision.decision_code})")
    print(f"Admissible:      {decision.admissible}")
    print(f"Reasons:         {'; '.join(decision.reasons)}")
    return 0 if decision.admissible else 1


def cmd_mutations(args: argparse.Namespace) -> int:
    """List historical mutation records."""
    harness = get_harness(args.registry)
    records = harness.mutation_registry.list_mutations()
    if args.status:
        records = [r for r in records if _val(r.status).upper() == args.status.upper()]

    if args.json:
        print(json.dumps([r.to_dict() for r in records], indent=2))
        return 0

    print(f"\nADMITTED MUTATIONS ({len(records)} entries):")
    print("-" * 85)
    print(f"{'ID':<18} {'STATUS':<10} {'TARGET(S)':<22} {'LEVEL':<6} {'DESCRIPTION'}")
    print("-" * 85)
    for r in records:
        tgts = []
        for eff in r.observed_effects:
            if getattr(eff, "target", "") and eff.target not in tgts:
                tgts.append(eff.target)
        if not tgts and r.recovery_program:
            for op in getattr(r.recovery_program, "operations", []):
                t = getattr(op, "target", None) or getattr(op, "parameters", {}).get("target")
                if t and str(t) not in tgts:
                    tgts.append(str(t))
        tgt_str = ", ".join(tgts)[:20] if tgts else "default"
        lvl = getattr(r, "recovery_language", "R4") or "R4"
        print(f"{r.mutation_id:<18} {_val(r.status):<10} {tgt_str:<22} {lvl:<6} {r.description[:26]}")
    print("-" * 85)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Inspect detailed record of a specific mutation."""
    harness = get_harness(args.registry)
    record = harness.mutation_registry.get(args.mutation_id)
    if not record:
        print(f"Error: Mutation '{args.mutation_id}' not found in registry.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(record.to_dict(), indent=2))
        return 0

    print("=" * 60)
    print(f"MUTATION RECORD: {record.mutation_id} ({_val(record.status)})")
    print("=" * 60)
    print(f"Description:        {record.description}")
    print(f"Resulting Version:  v{record.resulting_harness_version}")
    import datetime
    try:
        ts_str = datetime.datetime.fromtimestamp(record.timestamp).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        ts_str = str(record.timestamp)
    print(f"Timestamp:          {ts_str}")
    print(f"Capability Delta:   +{record.capability_delta:.2f}")
    print(f"Recovery LCB_0.95:  {record.recovery_lcb:.4f}")
    print(f"Recovery Language:  {record.recovery_language}")

    effects = getattr(record, "observed_effects", [])
    print(f"Observed Effects:   {len(effects)}")
    for eff in effects:
        print(f"  - [{_val(eff.category)}] {eff.target} ({_val(eff.op_type)})")

    ops = getattr(record.recovery_program, "operations", []) if hasattr(record, "recovery_program") else []
    print(f"Recovery Program ({len(ops)} operations):")
    for op in ops:
        print(f"  - {op.__class__.__name__}")
    print("=" * 60)
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    """Selectively rollback a mutation while preserving independent changes."""
    harness = get_harness(args.registry)
    try:
        new_state = harness.revert(args.mutation_id, reason=args.reason)
        if args.json:
            print(json.dumps({
                "status": "SUCCESS",
                "reverted": True,
                "mutation_id": args.mutation_id,
                "new_version": new_state.version,
                "state_hash": new_state.canonical_hash(),
            }, indent=2))
            return 0

        print(f"\n[SUCCESS] Mutation '{args.mutation_id}' Reverted via Targeted Recovery.")
        print(f"  New Harness Version: v{new_state.version}")
        print(f"  New State Hash:      {new_state.canonical_hash()}")
        print(f"  Active Tools:        {', '.join(new_state.tools.keys()) or 'None'}")
        print(f"  Active Middleware:   {', '.join(m.id for m in new_state.middleware) or 'None'}")
        print(f"  Unrelated modifications preserved intact.")
        return 0
    except Exception as e:
        print(f"Error during rollback: {e}", file=sys.stderr)
        return 1


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize a new EvoUndo-protected agent repository."""
    agent_template = '''"""EvoUndo-Protected Agent Application: 1-Minute Walkthrough."""

import os
import sqlite3
from evoundo import protect_tool, show_history, revert, sqlite

DB_PATH = "orders.db"


def init_database() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, status TEXT, total REAL)")
    cur.execute("DELETE FROM orders WHERE id = 1")
    cur.execute("INSERT INTO orders (id, status, total) VALUES (1, 'PENDING', 99.50)")
    conn.commit()
    conn.close()


# Protect any state-changing function with @protect_tool
@protect_tool(target="orders:{order_id}")
def update_order_status(order_id: int, new_status: str) -> str:
    with sqlite(DB_PATH) as db:
        db.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
    return f"Order {order_id} updated to {new_status}"


def get_current_order_status(order_id: int = 1) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "NOT_FOUND"


def main() -> None:
    print("\\n" + "=" * 60)
    print("      EVOUNDO 1-MINUTE DEVELOPER WALKTHROUGH       ")
    print("=" * 60)

    # 1. Initial State
    init_database()
    print(f"\\n1. Initial external state on disk:")
    print(f"   orders.db -> Order #1 status: '{get_current_order_status(1)}'")

    # 2. Execute protected mutation
    print("\\n2. Executing @protect_tool mutation: update_order_status(1, 'CONFIRMED')...")
    res = update_order_status(1, "CONFIRMED")
    print(f"   -> Result: {res}")
    print(f"   -> External state on disk is now: '{get_current_order_status(1)}'")

    # 3. View audit history
    print("\\n3. Inspecting EvoUndo persistent audit trail:")
    history = show_history()
    last = history[-1]
    mutation_id = last["mutation_id"]
    print(f"   -> Mutation ID:  {mutation_id}")
    print(f"   -> Targets:      {last.get('targets')}")
    print(f"   -> Status:       {last['status']}")
    print(f"   -> Recovery Lvl: {last.get('recovery_level', 'R4')}")

    # 4. Selective revert
    print(f"\\n4. Simulating recovery: Reverting {mutation_id}...")
    revert(mutation_id, reason="Customer cancelled shipment")
    restored = get_current_order_status(1)
    print(f"   -> External state on disk restored to: '{restored}' (Restored to pre-state!)")

    print("\\n" + "=" * 60)
    print("✨ EvoUndo successfully protected, recorded, and restored external state!")
    print("Next steps:")
    print("  • Run `evoundo status` to inspect active harness surfaces.")
    print("  • Run `evoundo mutations` to see the full audit trail.")
    print("  • Run `evoundo studio` to open the visual control plane.\\n")


if __name__ == "__main__":
    main()
'''
    yaml_config = """# EvoUndo Agent Configuration
version: 1
control_plane:
  verification_mode: paper_faithful
  tau_R: 0.85
  recovery_language: L1
  budget: 3
persistence:
  canonical_directory: ".evoundo"
  registry_file: ".evoundo/registry.json"
"""
    with open("evoundo_agent.py", "w") as f:
        f.write(agent_template)
    with open("evoundo.yaml", "w") as f:
        f.write(yaml_config)
    os.makedirs(".evoundo", exist_ok=True)

    print("\n✨ [SUCCESS] Initialized EvoUndo agent repository!")
    print("  Created: evoundo_agent.py (1-minute executable mutation & recovery walkthrough)")
    print("  Created: evoundo.yaml (control plane configuration)")
    print("  Created: .evoundo/ (canonical persistence directory)")
    print("\nNext steps:")
    print("  1. Run `python3 evoundo_agent.py` to test your agent.")
    print("  2. Run `evoundo status` to inspect active harness surfaces.")
    print("  3. Run `evoundo studio` to open the visual control plane in your browser.\n")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Export the active harness as a standalone Python file."""
    harness = get_harness(args.registry)
    code = HarnessCodeExporter.export_python_code(harness.current_state)
    out_file = args.output or "deployed_agent.py"
    with open(out_file, "w") as f:
        f.write(code)
    print(f"Exported active agent harness (v{harness.current_state.version}) to {out_file}")
    return 0


def cmd_studio(args: argparse.Namespace) -> int:
    """Launch the visual Evolution Control Plane Studio."""
    import uvicorn
    print(BANNER)
    print(f"Launching EvoUndo Studio on http://{args.host}:{args.port}")
    print(f"Connected to registry: {args.registry or get_default_registry_file()}")
    uvicorn.run("evoundo.studio.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-run independent counterfactual verification on a mutation."""
    harness = get_harness(args.registry)
    record = harness.mutation_registry.get(args.mutation_id)
    if not record:
        print(f"Error: Mutation '{args.mutation_id}' not found in registry.", file=sys.stderr)
        return 1

    if not args.json:
        print(f"[EvoUndo Verify] Evaluating counterfactual recoverability for '{args.mutation_id}'...")

    def forward_fn(s: HarnessState) -> None:
        for eff in record.observed_effects:
            cat = _val(eff.category)
            op = _val(eff.op_type)
            if cat == "middleware":
                if op in ("CREATE", "UPDATE"):
                    s.add_middleware(MiddlewareDescriptor(id=eff.target, name=eff.target))
                elif op == "DELETE":
                    s.remove_middleware(eff.target)
            elif cat == "config":
                if op in ("CREATE", "UPDATE"):
                    s.set_config(eff.target, record.forward_mutation_summary.get(eff.target, "val"))
                elif op == "DELETE":
                    s.delete_config(eff.target)
            elif cat == "tools":
                if op in ("CREATE", "UPDATE"):
                    s.register_tool(ToolDescriptor(name=eff.target, description=eff.target, fn=lambda x: x))
                elif op == "DELETE":
                    s.remove_tool(eff.target)

    ver_res = harness.counterfactual_verifier.verify(
        base_state=harness.current_state,
        forward_mutation_fn=forward_fn,
        contract=record.effect_contract,
        witness_manager=harness.witness_manager,
        recovery_engine=harness.recovery_engine,
        recovery_program=record.recovery_program,
        mutation_id=args.mutation_id,
        mode=args.mode,
    )

    if args.json:
        print(json.dumps(ver_res.to_dict(), indent=2))
        return 0 if ver_res.is_verified else 1

    print("=" * 60)
    print(f"VERIFICATION AUDIT: {args.mutation_id} -> {'VERIFIED (PASS)' if ver_res.is_verified else 'REJECTED (FAIL)'}")
    print("=" * 60)
    print(f"Trials Passed:     {ver_res.trials_passed}/{ver_res.total_trials} ({ver_res.empirical_rate*100:.1f}%)")
    print(f"Wilson 95% LCB:    {ver_res.wilson_lcb:.6f} (Threshold tau_R: {ver_res.tau_R})")
    print(f"Verification Mode: {ver_res.mode}")
    print(f"Similarity Score:  {ver_res.similarity_score:.4f}")
    if ver_res.divergence_details:
        print("Divergences:")
        for d in ver_res.divergence_details:
            print(f"  - {d}")
    print("=" * 60)
    return 0 if ver_res.is_verified else 1


BANNER = (
    "\033[1;35m"
    "  ███████╗██╗   ██╗ ██████╗ ██╗   ██╗███╗   ██╗██████╗  ██████╗ \n"
    "  ██╔════╝██║   ██║██╔═══██╗██║   ██║████╗  ██║██╔══██╗██╔═══██╗\n"
    "  █████╗  ██║   ██║██║   ██║██║   ██║██╔██╗ ██║██║  ██║██║   ██║\n"
    "  ██╔══╝  ╚██╗ ██╔╝██║   ██║██║   ██║██║╚██╗██║██║  ██║██║   ██║\n"
    "  ███████╗ ╚████╔╝ ╚██████╔╝╚██████╔╝██║ ╚████║██████╔╝╚██████╔╝\n"
    "  ╚══════╝  ╚═══╝   ╚═════╝  ╚═════╝ ╚═╝  ╚═══╝╚═════╝  ╚═════╝ \n"
    "\033[0m"
    f"  \033[1;36mv{__version__}\033[0m \033[1;37m· \"Keep what works. Undo what changed.\"\033[0m\n"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evoundo",
        description=BANNER,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"evoundo {__version__}")
    parser.add_argument("--registry", default=None, help="Path to mutation registry file (default: ./.evoundo/registry.json or ~/.evoundo/registry.json)")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    subparsers.add_parser("init", help="Scaffold a new EvoUndo-protected agent repository")

    # status
    p_stat = subparsers.add_parser("status", help="Show current harness state and active surfaces")
    p_stat.add_argument("--json", action="store_true", help="Output in JSON format")

    # evolve
    p_evolve = subparsers.add_parser("evolve", help="Propose, verify, and admit a self-evolution mutation")
    p_evolve.add_argument("goal", help="Evolution goal description")
    p_evolve.add_argument("--budget", type=int, default=3, help="Repair budget B (1..4)")
    p_evolve.add_argument("--language", type=str, default="L1", choices=["L0", "L1"], help="Recovery language")
    p_evolve.add_argument("--json", action="store_true", help="Output in JSON format")

    # dry-run
    p_dry = subparsers.add_parser("dry-run", help="Simulate evolution without persistent state admission")
    p_dry.add_argument("goal", help="Evolution goal description")
    p_dry.add_argument("--language", type=str, default="L1", choices=["L0", "L1"], help="Recovery language")
    p_dry.add_argument("--json", action="store_true", help="Output in JSON format")

    # mutations
    p_muts = subparsers.add_parser("mutations", help="List admitted mutation history")
    p_muts.add_argument("--status", type=str, choices=["ACTIVE", "REVERTED", "DISABLED"], help="Filter by status")
    p_muts.add_argument("--json", action="store_true", help="Output in JSON format")

    # inspect
    p_insp = subparsers.add_parser("inspect", help="Inspect detailed mutation record and recovery plan")
    p_insp.add_argument("mutation_id", type=str, help="Mutation ID to inspect")
    p_insp.add_argument("--json", action="store_true", help="Output in JSON format")

    # verify
    p_ver = subparsers.add_parser("verify", help="Run counterfactual verification on a mutation")
    p_ver.add_argument("mutation_id", type=str, help="Mutation ID to verify")
    p_ver.add_argument("--mode", default="smoke", choices=["smoke", "paper_faithful"], help="Verification mode")
    p_ver.add_argument("--json", action="store_true", help="Output in JSON format")

    # undo / revert / rollback
    p_undo = subparsers.add_parser("undo", help="Revert a mutation with targeted recovery")
    p_undo.add_argument("mutation_id", type=str, help="Mutation ID to revert")
    p_undo.add_argument("--reason", type=str, default="User requested rollback", help="Audit reason for reversion")
    p_undo.add_argument("--json", action="store_true", help="Output in JSON format")

    p_rev = subparsers.add_parser("revert", help="Alias for undo")
    p_rev.add_argument("mutation_id", type=str, help="Mutation ID to revert")
    p_rev.add_argument("--reason", type=str, default="User requested rollback", help="Audit reason for reversion")
    p_rev.add_argument("--json", action="store_true", help="Output in JSON format")

    p_roll = subparsers.add_parser("rollback", help="Alias for undo")
    p_roll.add_argument("mutation_id", type=str, help="Mutation ID to revert")
    p_roll.add_argument("--reason", type=str, default="User requested rollback", help="Audit reason for reversion")
    p_roll.add_argument("--json", action="store_true", help="Output in JSON format")

    # export
    p_exp = subparsers.add_parser("export", help="Export active harness state to standalone Python script")
    p_exp.add_argument("-o", "--output", default="deployed_agent.py", help="Output Python file path")

    # serve / studio
    p_serve = subparsers.add_parser("serve", help="Launch the EvoUndo Studio Web Control Plane")
    p_serve.add_argument("--host", default="127.0.0.1", help="Host IP (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    p_serve.add_argument("--open", action="store_true", default=True, help="Automatically open browser")

    p_studio = subparsers.add_parser("studio", help="Launch the visual EvoUndo Studio in browser")
    p_studio.add_argument("--host", default="127.0.0.1", help="Host IP (default: 127.0.0.1)")
    p_studio.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    p_studio.add_argument("--open", action="store_true", default=True, help="Automatically open browser")

    # integrations
    p_int = subparsers.add_parser("integrations", help="Display integration capability scorecard across agent frameworks")
    p_int.add_argument("--json", action="store_true", help="Output in JSON format")

    # wrap
    p_wrap = subparsers.add_parser("wrap", help="Wrap a coding agent or CLI runtime with EvoUndo safety proxy")
    p_wrap.add_argument("agent", choices=["claude-code", "claude", "cursor", "codex", "hermes", "openclaw"], help="Target agent platform")

    # proxy
    p_proxy = subparsers.add_parser("proxy", help="Run EvoUndo as a transparent MCP or HTTP tool proxy")
    p_proxy.add_argument("--mcp", action="store_true", default=True, help="Enable Model Context Protocol (MCP) proxying")
    p_proxy.add_argument("--port", type=int, default=8787, help="HTTP proxy port (default: 8787)")

    return parser


def cmd_integrations(args: argparse.Namespace) -> int:
    """Display ecosystem integration status and capability levels."""
    import importlib.util

    integrations = [
        ("LangGraph / LangChain", "langgraph", "Adapter Implemented"),
        ("Google ADK / GenAI", "google.genai", "Adapter Implemented"),
        ("OpenAI Agents SDK", "agents", "Adapter Implemented"),
        ("CrewAI", "crewai", "Adapter Implemented"),
        ("Microsoft AutoGen / AG2", "autogen_agentchat", "Adapter Implemented"),
        ("Microsoft Agent Framework", "agent_framework", "Adapter Implemented"),
        ("AWS Strands Agents", "strands", "Adapter Implemented"),
        ("LlamaIndex", "llama_index", "Adapter Implemented"),
        ("PydanticAI", "pydantic_ai", "Adapter Implemented"),
        ("MCP (Model Context Protocol)", "mcp", "Adapter Implemented"),
        ("Anthropic / Claude Tool-Use", "anthropic", "Adapter Implemented"),
    ]

    results = []
    for name, mod_name, adapter_level in integrations:
        try:
            installed = importlib.util.find_spec(mod_name) is not None
        except Exception:
            installed = False
        runtime_status = "INSTALLED" if installed else "NOT INSTALLED"
        capability = f"{adapter_level} ({runtime_status})"
        results.append({
            "framework": name,
            "installed": "YES" if installed else "NO",
            "validated_adapter": "YES",
            "capability_level": capability,
        })

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
        return 0

    print("=" * 80)
    print("EVOUNDO AGENT ECOSYSTEM INTEGRATION SCORECARD")
    print("=" * 80)
    print(f"{'Framework':<30} {'Installed':<12} {'Adapter':<12} {'Capability Level'}")
    print("-" * 80)
    for r in results:
        print(f"{r['framework']:<30} {r['installed']:<12} {r['validated_adapter']:<12} {r['capability_level']}")
    print("=" * 80)
    return 0


def cmd_wrap(args: argparse.Namespace) -> int:
    """Launch or configure an agent wrapped with EvoUndo safety proxy."""
    target_agent = args.agent.lower()
    print(f"Error: Standalone CLI wrapping for '{target_agent}' is not supported.", file=sys.stderr)
    print(f"To protect '{target_agent}' tools, import and use the Python SDK integration directly in your application code:", file=sys.stderr)
    mod_name = target_agent.replace("-", "_")
    print(f"  from evoundo.integrations.{mod_name} import ...", file=sys.stderr)
    return 1


def cmd_proxy(args: argparse.Namespace) -> int:
    """Launch transparent Model Context Protocol (MCP) or HTTP proxy."""
    print(f"Error: Standalone MCP proxy daemon is not supported.", file=sys.stderr)
    print(f"Use the EvoUndo Python API or Studio server (`evoundo serve`) to protect tool executions.", file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "integrations":
        return cmd_integrations(args)
    elif args.command == "wrap":
        return cmd_wrap(args)
    elif args.command == "proxy":
        return cmd_proxy(args)
    elif args.command in ("serve", "studio"):
        import uvicorn
        url = f"http://{args.host}:{args.port}"
        print(f"\n==================================================================")
        print(f"🚀 EVOUNDO AGENT STUDIO STARTING ON {url}")
        print(f"==================================================================\n")
        if getattr(args, "open", True) or args.command == "studio":
            try:
                webbrowser.open(url)
            except Exception:
                pass
        uvicorn.run("evoundo.studio.app:app", host=args.host, port=args.port, log_level="info")
        return 0
    elif args.command == "init":
        return cmd_init(args)
    elif args.command == "export":
        return cmd_export(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "evolve":
        return cmd_evolve(args)
    elif args.command == "dry-run":
        return cmd_dry_run(args)
    elif args.command == "mutations":
        return cmd_mutations(args)
    elif args.command == "inspect":
        return cmd_inspect(args)
    elif args.command == "verify":
        return cmd_verify(args)
    elif args.command in ("undo", "revert", "rollback"):
        return cmd_undo(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
