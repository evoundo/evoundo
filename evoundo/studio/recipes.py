from __future__ import annotations
from typing import Any, Dict, List, Optional
from evoundo.compiler.schemas import DeclarativeMutationSpec, OpSpec


RECIPES: List[Dict[str, Any]] = [
    {
        "id": "recipe_cache",
        "title": "⚡ Smart Response Caching",
        "category": "Performance",
        "icon": "zap",
        "badge": "+85% Faster",
        "description": "Adds an in-memory CacheMiddleware with 300s TTL to eliminate duplicate search queries and drop latency from 200ms to <1ms.",
        "prompt": "Improve the agent so repeated web searches are cached with a 300s TTL.",
        "spec": DeclarativeMutationSpec(
            mutation_id="recipe_cache",
            description="Add CacheMiddleware with a 300s TTL to optimize query latency.",
            forward_ops=[
                OpSpec(
                    op_type="add_middleware",
                    target="cache_mw",
                    surface="middleware",
                    value={"name": "CacheMiddleware", "priority": 20, "ttl_sec": 300},
                ),
                OpSpec(
                    op_type="set_config",
                    target="cache_ttl_sec",
                    surface="config",
                    value=300,
                ),
            ],
        ),
    },
    {
        "id": "recipe_calculator",
        "title": "🧮 Math & Arithmetic Tool",
        "category": "Capability",
        "icon": "calculator",
        "badge": "New Tool",
        "description": "Registers a mathematical evaluation tool capable of computing complex arithmetic, percentages, powers, and trigonometry.",
        "prompt": "Register a calculator tool to perform arithmetic and mathematical evaluations.",
        "spec": DeclarativeMutationSpec(
            mutation_id="recipe_calculator",
            description="Register calculator tool for arithmetic and mathematical calculations.",
            forward_ops=[
                OpSpec(
                    op_type="register_tool",
                    target="calculator",
                    surface="tools",
                    value={"description": "Evaluates arithmetic and mathematical expressions."},
                ),
            ],
        ),
    },
    {
        "id": "recipe_python",
        "title": "🐍 Python Code Sandbox",
        "category": "Capability",
        "icon": "code",
        "badge": "Code Execution",
        "description": "Registers a sandboxed Python execution tool allowing the agent to run data analysis and algorithmic scripts.",
        "prompt": "Register a sandboxed Python code execution tool for programmatic data processing.",
        "spec": DeclarativeMutationSpec(
            mutation_id="recipe_python",
            description="Register python_exec tool for running sandboxed Python code snippets.",
            forward_ops=[
                OpSpec(
                    op_type="register_tool",
                    target="python_exec",
                    surface="tools",
                    value={"description": "Executes sandboxed Python code and returns stdout/return values."},
                ),
            ],
        ),
    },
    {
        "id": "recipe_retry",
        "title": "🔁 Exponential Retry Middleware",
        "category": "Reliability",
        "icon": "refresh-cw",
        "badge": "99.9% Uptime",
        "description": "Inserts a fault-tolerant RetryMiddleware that intercepts transient API timeouts and automatically retries with backoff.",
        "prompt": "Add a retry middleware to automatically handle transient network errors.",
        "spec": DeclarativeMutationSpec(
            mutation_id="recipe_retry",
            description="Add RetryMiddleware with exponential backoff for transient failures.",
            forward_ops=[
                OpSpec(
                    op_type="add_middleware",
                    target="retry_mw",
                    surface="middleware",
                    value={"name": "RetryMiddleware", "priority": 10, "max_retries": 3},
                ),
                OpSpec(
                    op_type="set_config",
                    target="retries",
                    surface="config",
                    value=3,
                ),
            ],
        ),
    },
    {
        "id": "recipe_safety",
        "title": "🛡️ Output Safety Validator",
        "category": "Safety",
        "icon": "shield",
        "badge": "Guardrail",
        "description": "Adds a SafetyValidatorMiddleware pipeline stage to inspect tool inputs and sanitise model outputs against sensitive leaks.",
        "prompt": "Add an output safety validation middleware to guard against data leaks.",
        "spec": DeclarativeMutationSpec(
            mutation_id="recipe_safety",
            description="Add SafetyValidatorMiddleware to inspect inputs and sanitize outputs.",
            forward_ops=[
                OpSpec(
                    op_type="add_middleware",
                    target="safety_validator_mw",
                    surface="middleware",
                    value={"name": "SafetyValidatorMiddleware", "priority": 5},
                ),
                OpSpec(
                    op_type="set_config",
                    target="strict_safety_mode",
                    surface="config",
                    value=True,
                ),
            ],
        ),
    },
    {
        "id": "recipe_telemetry",
        "title": "📡 Live Query Telemetry Hook",
        "category": "Observability",
        "icon": "activity",
        "badge": "Audit Logging",
        "description": "Registers an on_query event listener that streams execution metrics, tool parameters, and timing traces to the audit log.",
        "prompt": "Register a query telemetry listener to monitor agent execution duration.",
        "spec": DeclarativeMutationSpec(
            mutation_id="recipe_telemetry",
            description="Register on_query event listener for telemetry and latency tracking.",
            forward_ops=[
                OpSpec(
                    op_type="add_listener",
                    target="on_query",
                    surface="listeners",
                    value={"event": "on_query", "handler": "telemetry_logger"},
                ),
            ],
        ),
    },
    {
        "id": "recipe_timeout",
        "title": "⏱️ Extended Execution Timeout",
        "category": "Configuration",
        "icon": "clock",
        "badge": "60s Timeout",
        "description": "Increases execution timeout from 30s to 60s for deep research queries.",
        "prompt": "Update the agent execution timeout to 60 seconds.",
        "spec": DeclarativeMutationSpec(
            mutation_id="recipe_timeout",
            description="Update execution timeout to 60 seconds.",
            forward_ops=[
                OpSpec(
                    op_type="set_config",
                    target="timeout_sec",
                    surface="config",
                    value=60,
                ),
            ],
        ),
    },
]


def get_all_recipes() -> List[Dict[str, Any]]:
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "category": r["category"],
            "icon": r["icon"],
            "badge": r["badge"],
            "description": r["description"],
            "prompt": r["prompt"],
        }
        for r in RECIPES
    ]


def get_recipe_by_id(recipe_id: str) -> Optional[Dict[str, Any]]:
    for r in RECIPES:
        if r["id"] == recipe_id:
            return r
    return None
