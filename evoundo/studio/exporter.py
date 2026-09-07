"""Code exporter for converting active HarnessState to standalone Python/LangGraph code."""

from typing import Any, Dict
from evoundo.core.state import HarnessState


class HarnessCodeExporter:
    """Exports active HarnessState to executable Python code and LangGraph agent pipelines."""

    @classmethod
    def export_python_code(cls, state: HarnessState) -> str:
        code_lines = [
            "# =================================================================",
            f"# EvoUndo Agent Harness Export — Version {state.version}",
            f"# State Hash: #{state.canonical_hash()[:8]}",
            "# Generated automatically by EvoUndo Control Plane Studio",
            "# =================================================================",
            "",
            "from dataclasses import dataclass",
            "from typing import Any, Dict, List, Optional",
            "import math, time",
            "",
            "# --- 1. Harness Configuration ---",
            "CONFIG = {",
        ]

        for k, v in state.config.items():
            code_lines.append(f"    {repr(k)}: {repr(v)},")
        code_lines.append("}\n")

        # Tools
        code_lines.append("# --- 2. Tool Registry ---")
        code_lines.append("class AgentTools:")
        for t_name, t_desc in state.tools.items():
            if t_name == "calculator":
                code_lines.extend([
                    "    @staticmethod",
                    "    def calculator(expression: str) -> float:",
                    "        \"\"\"Evaluates mathematical expressions safely.\"\"\"",
                    "        safe_dict = {'__builtins__': None, 'math': math}",
                    "        return eval(expression, safe_dict, {})",
                    "",
                ])
            elif t_name == "web_search":
                code_lines.extend([
                    "    @staticmethod",
                    "    def web_search(query: str) -> List[str]:",
                    "        \"\"\"Search technical web sources and papers.\"\"\"",
                    "        return [f\"Results for '{query}' [Source 1, Source 2]\"]",
                    "",
                ])
            else:
                code_lines.extend([
                    "    @staticmethod",
                    f"    def {t_name}(*args, **kwargs) -> Any:",
                    f"        \"\"\"{t_desc.description}\"\"\"",
                    f"        return f\"Executed {t_name}\"",
                    "",
                ])

        # Middleware
        code_lines.append("# --- 3. Middleware Pipeline ---")
        code_lines.append("MIDDLEWARE_PIPELINE = [")
        for m in sorted(state.middleware, key=lambda x: x.priority):
            code_lines.append(f"    {{'id': '{m.id}', 'name': '{m.name}', 'priority': {m.priority}, 'enabled': {m.enabled}}},")
        code_lines.append("]\n")

        # Main Agent Runtime Loop
        code_lines.extend([
            "# --- 4. Agent Runtime Execution ---",
            "class EvoUndoDeployedAgent:",
            "    def __init__(self, config=CONFIG):",
            "        self.config = config",
            "        self.cache = {}",
            "",
            "    def run(self, query: str) -> Dict[str, Any]:",
            "        # Check caching middleware if present",
            "        if 'cache_mw' in [m['id'] for m in MIDDLEWARE_PIPELINE]:",
            "            if query in self.cache:",
            "                return {'output': self.cache[query], 'cached': True, 'latency_ms': 0.5}",
            "",
            "        # Tool execution",
            "        if 'calculator' in dir(AgentTools) and any(op in query for op in ['+', '*', '/', 'math']):",
            "            res = str(AgentTools.calculator(query))",
            "        elif 'web_search' in dir(AgentTools):",
            "            res = str(AgentTools.web_search(query))",
            "        else:",
            "            res = f\"Processed: '{query}'\"",
            "",
            "        if 'cache_mw' in [m['id'] for m in MIDDLEWARE_PIPELINE]:",
            "            self.cache[query] = res",
            "",
            "        return {'output': res, 'cached': False}",
            "",
            "if __name__ == '__main__':",
            "    agent = EvoUndoDeployedAgent()",
            "    print(\"EvoUndo Deployed Agent Initialized.\")",
            "    print(agent.run(\"Sample test query\"))",
        ])

        return "\n".join(code_lines)
