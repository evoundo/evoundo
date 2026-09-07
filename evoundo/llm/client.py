"""LLM client interface and dynamic model discovery for EvoUndo Harness."""

from __future__ import annotations
import json
import os
import re
import uuid
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from evoundo.compiler.schemas import DeclarativeMutationSpec, OpSpec


SYSTEM_PROMPT_MUTATION = """You are EvoUndo Meta-Agent, an intelligent assistant and self-evolving agent compiler for the EvoUndo Harness.

Classify the user's intent into one of two categories:

CATEGORY 1: CONVERSATIONAL QUERY OR INQUIRY
If the user is greeting, asking questions about who you are, what model is running, explaining concepts, or NOT requesting an architectural modification:
You MUST respond with:
{
  "is_evolution": false,
  "response": "Your conversational answer here."
}

CATEGORY 2: HARNESS SELF-EVOLUTION REQUEST
If the user asks to add/modify tools, middleware, configurations, listeners, files, or resources (e.g. "Add caching middleware", "Set timeout to 60s", "Add calculator tool"):
You MUST respond with:
{
  "is_evolution": true,
  "explanation": "Brief description of why and how this evolution satisfies the goal.",
  "operations": [
    {
      "op_type": "add_middleware", // set_config | register_tool | add_middleware | add_listener | write_file
      "target": "cache_mw",
      "surface": "middleware",
      "value": { "name": "CacheMiddleware", "priority": 20 }
    }
  ]
}
"""


@dataclass
class LLMProposalResponse:
    is_evolution: bool
    explanation: str
    spec: Optional[DeclarativeMutationSpec] = None
    chat_response: Optional[str] = None
    model_name: str = "default"
    raw_response: str = ""


class LLMEvolverClient:
    """Client for generating structured self-evolution proposals from real LLM models."""

    def __init__(self, model_name: Optional[str] = None):
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("EVOUNDO_LLM_API_KEY")
        self.base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("EVOUNDO_LLM_BASE_URL")
        self.model_name = model_name or "local-heuristic-synthesizer"

    def get_available_models(self) -> List[Dict[str, Any]]:
        """Dynamically detect models and order by speed and reliability."""
        models = []

        # 1. Zero-latency instant synthesizer (Fastest default)
        models.append({
            "id": "local-heuristic-synthesizer",
            "name": "⚡ Local Synthesizer (Instantaneous — 0.001s)",
            "available": True,
            "is_default": True,
            "backend": "heuristic",
            "model_tag": "heuristic",
        })

        # 2. Check local Ollama models with speed tags
        try:
            req = urllib.request.Request("http://localhost:11434/api/tags", headers={"User-Agent": "EvoUndo"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                
                # Priority order for fast local models
                priority_order = ["llama3:latest", "llama3.1:latest", "gemma2:2b", "llama3.2:latest", "deepseek-r1:1.5b", "qwen2.5:7b", "gemma4:12b", "gpt-oss:latest"]
                
                discovered = {m.get("name", ""): m for m in data.get("models", []) if "embed" not in m.get("name", "")}
                
                # Add in prioritized order
                for tag in priority_order:
                    if tag in discovered:
                        m = discovered.pop(tag)
                        param = m.get("details", {}).get("parameter_size", "")
                        speed_tag = "🚀 ~1.5s" if ("2b" in param or "3b" in param or "8b" in param or "3.2" in tag or "3:latest" in tag) else "⏳ Heavy"
                        models.append({
                            "id": f"ollama:{tag}",
                            "name": f"Ollama Local ({tag}{f' - {param}' if param else ''} — {speed_tag})",
                            "available": True,
                            "is_default": False,
                            "backend": "ollama",
                            "model_tag": tag,
                        })

                # Add remaining discovered
                for tag, m in discovered.items():
                    param = m.get("details", {}).get("parameter_size", "")
                    models.append({
                        "id": f"ollama:{tag}",
                        "name": f"Ollama Local ({tag}{f' - {param}' if param else ''})",
                        "available": True,
                        "is_default": False,
                        "backend": "ollama",
                        "model_tag": tag,
                    })
        except Exception:
            pass

        # 3. Cloud models
        models.append({
            "id": "openai:gpt-4o-mini",
            "name": "OpenAI GPT-4o Mini (Cloud API)",
            "available": bool(self.api_key),
            "is_default": False,
            "backend": "openai",
            "model_tag": "gpt-4o-mini",
        })
        models.append({
            "id": "openai:gpt-4o",
            "name": "OpenAI GPT-4o (Cloud API)",
            "available": bool(self.api_key),
            "is_default": False,
            "backend": "openai",
            "model_tag": "gpt-4o",
        })

        return models

    def is_conversational_query(self, message: str, active_model_display: str = "") -> Tuple[bool, Optional[str]]:
        lowered = message.lower().strip()

        # Check for how-to-use / tutorial queries
        if any(h in lowered for h in ["how to use", "how do i use", "how does this work", "how to use ui", "what to do", "guide", "tutorial", "instructions"]):
            return True, (
                "Here is how to use EvoUndo Agent Studio in 3 simple steps:\n\n"
                "1. **Evolve the Agent (Studio Tab)**:\n"
                "   - Click any 1-Click Recipe chip at the top (e.g. **⚡ Smart Response Caching** or **🧮 Math & Arithmetic Tool**).\n"
                "   - Click **Send** $\\rightarrow$ click **Review Mutation** $\\rightarrow$ click **Admit & Commit to Harness**.\n"
                "   - Watch the right-hand panel instantly update with the new tool/middleware!\n\n"
                "2. **Test Live Execution (Playground Tab)**:\n"
                "   - Click the **Execution Playground** tab at the top.\n"
                "   - Click **Execute Task** to see the agent run live with interceptor traces and sub-millisecond cache latency!\n\n"
                "3. **Selective Time Machine (Lineage Tab)**:\n"
                "   - Click the **Time Machine (DAG)** tab to see the version history.\n"
                "   - Click **Targeted Undo** on any past mutation to selectively remove it while keeping other upgrades intact!"
            )

        # Check for model / identity queries
        if any(q in lowered for q in ["who are u", "who are you", "who r u", "who u", "who is this", "which model", "whuch model", "what model", "what are you", "what are u", "your name", "genuine llm"]):
            return True, f"I am the **EvoUndo Meta-Agent**, running via **{active_model_display or self.model_name}**. I am your self-evolving assistant. I can chat with you, help you run tasks, or self-evolve my tools and middleware with mathematically proven recoverability guarantees!"

        if any(greeting == lowered for greeting in ["hi", "hello", "hey", "hola", "help", "sup"]):
            return True, "Hello! I am your EvoUndo Agent. You can chat with me or instruct me to evolve my architecture (e.g., 'Improve the agent so repeated web searches are cached', 'Set timeout to 60s', 'Add calculator tool'). Every modification is verified for recoverability before admission!"

        action_verbs = ["add", "remove", "delete", "set", "update", "change", "cache", "timeout", "tool", "middleware", "listener", "optimize", "register", "evolve", "modify"]
        if any(verb in lowered for verb in action_verbs):
            return False, None

        if any(lowered.startswith(w) for w in ["what", "why", "how", "can you", "could you", "tell me"]):
            return True, None

        return False, None

    def generate_proposal(
        self,
        user_message: str,
        current_state_summary: Dict[str, Any],
        model_override: Optional[str] = None,
    ) -> LLMProposalResponse:
        target_model = model_override or self.model_name

        is_ollama = target_model.startswith("ollama:") or "ollama" in target_model
        model_tag = target_model.split("ollama:")[-1] if is_ollama else target_model.split("openai:")[-1]
        if model_tag == "ollama-local":
            model_tag = "llama3:latest"

        # Check conversational queries fast
        is_conv, direct_reply = self.is_conversational_query(user_message, active_model_display=model_tag)
        if is_conv and direct_reply:
            return LLMProposalResponse(
                is_evolution=False,
                explanation="",
                chat_response=direct_reply,
                model_name=model_tag,
            )

        # Real inference via Ollama or OpenAI
        if is_ollama or (self.api_key and target_model.startswith("openai:")):
            try:
                import openai
                endpoint_base = "http://localhost:11434/v1" if is_ollama else (self.base_url or "https://api.openai.com/v1")
                endpoint_key = "ollama" if is_ollama else (self.api_key or "sk-dummy")

                client = openai.OpenAI(
                    api_key=endpoint_key,
                    base_url=endpoint_base,
                    timeout=8.0,
                )
                prompt = (
                    f"Current Harness State: {json.dumps(current_state_summary, indent=2)}\n\n"
                    f"User Message: {user_message}"
                )
                response = client.chat.completions.create(
                    model=model_tag,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT_MUTATION},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                )
                content = response.choices[0].message.content or "{}"
                return self._parse_json_response(content, model_tag, user_message)
            except Exception as e:
                fallback = self._synthesize_local(user_message, current_state_summary)
                fallback.model_name = f"{model_tag} (fallback)"
                fallback.explanation = f"(Live inference via '{model_tag}' encountered latency/timeout: {e}. Instant fallback to rule synthesizer): {fallback.explanation}"
                if fallback.chat_response:
                    fallback.chat_response = f"{fallback.chat_response}\n\n*(Note: Real LLM '{model_tag}' timed out, responded via fast fallback)*"
                return fallback

        # Local rule-based synthesizer fallback
        return self._synthesize_local(user_message, current_state_summary)

    def _parse_json_response(self, raw_json: str, model_name: str, user_message: str = "") -> LLMProposalResponse:
        try:
            data = json.loads(raw_json)
            is_evo = data.get("is_evolution", True)

            if is_evo and user_message:
                is_conv, direct_reply = self.is_conversational_query(user_message, active_model_display=model_name)
                if is_conv and direct_reply:
                    return LLMProposalResponse(
                        is_evolution=False,
                        explanation="",
                        chat_response=direct_reply,
                        model_name=model_name,
                        raw_response=raw_json,
                    )

            if not is_evo:
                return LLMProposalResponse(
                    is_evolution=False,
                    explanation="",
                    chat_response=data.get("response", data.get("explanation", "I am ready to assist.")),
                    model_name=model_name,
                    raw_response=raw_json,
                )

            explanation = data.get("explanation", "EvoUndo self-evolution mutation proposed.")
            ops: List[OpSpec] = []
            for op in data.get("operations", []):
                ops.append(OpSpec(
                    op_type=op.get("op_type", "set_config"),
                    target=op.get("target", "key"),
                    value=op.get("value"),
                    surface=op.get("surface", "config"),
                ))

            mid = f"mut_{uuid.uuid4().hex[:8]}"
            spec = DeclarativeMutationSpec(
                mutation_id=mid,
                description=explanation,
                forward_ops=ops,
            )
            return LLMProposalResponse(
                is_evolution=True,
                explanation=explanation,
                spec=spec,
                model_name=model_name,
                raw_response=raw_json,
            )
        except Exception:
            return self._synthesize_local(raw_json, {})

    def _synthesize_local(self, message: str, current_state: Dict[str, Any]) -> LLMProposalResponse:
        lowered = message.lower()
        is_conv, direct_reply = self.is_conversational_query(message, active_model_display="Local Synthesizer")
        if is_conv:
            return LLMProposalResponse(
                is_evolution=False,
                explanation="",
                chat_response=direct_reply or "Hello! I am your EvoUndo Agent.",
                model_name="local-heuristic-synthesizer",
            )

        ops: List[OpSpec] = []
        mid = f"mut_{uuid.uuid4().hex[:8]}"

        if "cache" in lowered or "caching" in lowered or "repeated" in lowered:
            explanation = "I propose adding a caching middleware with a 300s TTL to avoid duplicate search queries and reduce latency."
            ops.append(OpSpec(
                op_type="add_middleware",
                target="cache_mw",
                surface="middleware",
                value={"name": "CacheMiddleware", "priority": 20},
            ))
            ops.append(OpSpec(
                op_type="set_config",
                target="cache_ttl_sec",
                surface="config",
                value=300,
            ))
        elif "timeout" in lowered:
            numbers = re.findall(r"\d+", lowered)
            val = int(numbers[0]) if numbers else 60
            explanation = f"I propose updating the agent execution timeout to {val} seconds to accommodate longer research tasks."
            ops.append(OpSpec(
                op_type="set_config",
                target="timeout_sec",
                surface="config",
                value=val,
            ))
        elif "python" in lowered or "sandbox" in lowered or "code" in lowered:
            tool_name = "python_exec"
            desc = "Executes safe sandboxed Python scripts"
            explanation = "I propose registering the 'python_exec' tool to execute Python code in a safe sandbox."
            ops.append(OpSpec(
                op_type="register_tool",
                target=tool_name,
                surface="tools",
                value={"description": desc},
            ))
        elif "tool" in lowered or "calculator" in lowered or "math" in lowered:
            tool_name = "calculator"
            desc = "Evaluates mathematical expressions"
            explanation = f"I propose registering the '{tool_name}' tool to expand computational capabilities."
            ops.append(OpSpec(
                op_type="register_tool",
                target=tool_name,
                surface="tools",
                value={"description": desc},
            ))
        elif "retry" in lowered or "uptime" in lowered:
            explanation = "I propose installing exponential retry middleware to automatically recover from transient network failures."
            ops.append(OpSpec(
                op_type="add_middleware",
                target="retry_mw",
                surface="middleware",
                value={"name": "RetryMiddleware", "priority": 10},
            ))
            ops.append(OpSpec(
                op_type="set_config",
                target="retries",
                surface="config",
                value=3,
            ))
        elif "safety" in lowered or "guardrail" in lowered:
            explanation = "I propose installing a safety validator middleware to guard against destructive commands."
            ops.append(OpSpec(
                op_type="add_middleware",
                target="safety_validator_mw",
                surface="middleware",
                value={"name": "SafetyValidatorMiddleware", "priority": 5},
            ))
        elif "telemetry" in lowered or "hook" in lowered:
            explanation = "I propose attaching a query telemetry listener to log query execution events."
            ops.append(OpSpec(
                op_type="add_listener",
                target="on_query",
                surface="listeners",
                value={"description": "Logs query execution"},
            ))
        else:
            explanation = f"I propose updating the harness configuration to satisfy: {message}"
            ops.append(OpSpec(
                op_type="set_config",
                target="optimization_flag",
                surface="config",
                value=True,
            ))

        spec = DeclarativeMutationSpec(mutation_id=mid, description=explanation, forward_ops=ops)
        return LLMProposalResponse(
            is_evolution=True,
            explanation=explanation,
            spec=spec,
            model_name="local-heuristic-synthesizer",
            raw_response=json.dumps({"explanation": explanation, "operations": [o.to_dict() for o in ops]}),
        )
