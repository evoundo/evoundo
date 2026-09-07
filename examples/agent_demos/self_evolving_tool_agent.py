"""Demo agent demonstrating self-evolution and targeted recovery with EvoUndo Harness."""

from __future__ import annotations
import json
from evoundo.admission.policies import CapabilityResult
from evoundo.agent.agent import EvoAgent
from evoundo.core.harness import EvoUndoHarness
from evoundo.core.state import HarnessState, ToolDescriptor


def run_demo() -> None:
    print("=" * 70)
    print("EvoUndo Harness: Self-Evolving Tool Agent Demo")
    print("Tagline: Keep what works. Undo what changed.")
    print("=" * 70)

    # 1. Initialize Baseline Harness with Calculator and Search tools
    initial_state = HarnessState()
    initial_state.register_tool(ToolDescriptor(
        name="calculator",
        description="Evaluates mathematical expressions",
        fn=lambda expr: f"calc_result({expr})",
    ))
    initial_state.register_tool(ToolDescriptor(
        name="search",
        description="Searches knowledge base",
        fn=lambda query: f"search_results_for({query})",
    ))
    initial_state.set_config("tool_routing", {
        "math": "calculator",
        "lookup": "search",
    })
    initial_state.set_config("max_retries", 3)

    harness = EvoUndoHarness(initial_state=initial_state)
    agent = EvoAgent(name="EvoAssistant", harness=harness)

    print("\n--- Initial Agent State ---")
    print(f"Harness Version: {harness.current_state.version}")
    print(f"Registered Tools: {list(harness.current_state.tools.keys())}")
    print(f"Routing Config: {harness.current_state.get_config('tool_routing')}")
    print(f"Agent executing 'math' task: {agent.run('math', expr='2 + 2')}")

    # 2. Agent Proposes Evolution: Add 'weather' tool + update routing config
    print("\n--- Agent Proposing Self-Evolution: Adding Weather Tool ---")

    def weather_tool_fn(location: str) -> str:
        return f"Current weather in {location}: 22°C, Sunny"

    weather_tool = ToolDescriptor(
        name="weather",
        description="Retrieves real-time weather forecasts",
        fn=weather_tool_fn,
        version="1.0.0",
    )

    def evolve_weather(candidate) -> None:
        candidate.register_tool(weather_tool)
        # update routing
        curr_routing = dict(harness.current_state.get_config("tool_routing", {}))
        curr_routing["forecast"] = "weather"
        candidate.set_config("tool_routing", curr_routing)

    def capability_evaluator(cand_state: HarnessState) -> CapabilityResult:
        # Check if the candidate state can resolve the forecast query
        has_tool = "weather" in cand_state.tools
        has_route = cand_state.get_config("tool_routing", {}).get("forecast") == "weather"
        if has_tool and has_route:
            return CapabilityResult(improved=True, score_before=0.66, score_after=1.0, delta=0.34)
        return CapabilityResult(improved=False, score_before=0.66, score_after=0.66, delta=0.0)

    decision = agent.self_evolve(
        description="Add weather tool and route 'forecast' action to weather",
        mutate_fn=evolve_weather,
        capability_evaluator=capability_evaluator,
    )

    print(f"\nEvolution Decision: {decision.status.value}")
    print(f"Decision Code: {decision.decision_code}")
    print(f"Admissible: {decision.admissible}")

    mutation_id = harness.history()[-1]["mutation_id"]
    print(f"Admitted Mutation ID: {mutation_id}")
    print(f"Current Harness Version: {harness.current_state.version}")
    print(f"Active Tools: {list(harness.current_state.tools.keys())}")
    print(f"Routing Config: {harness.current_state.get_config('tool_routing')}")
    print(f"Agent executing 'forecast' task: {agent.run('forecast', location='San Francisco')}")

    # 3. Simulate Independent Later Mutation (e.g. Updating max_retries config)
    print("\n--- Applying Unrelated Subsequent Mutation: Increase max_retries ---")
    with harness.mutation(description="Increase max_retries to 5") as next_candidate:
        next_candidate.set_config("max_retries", 5)
    harness.admit(next_candidate)
    print(f"Harness Version: {harness.current_state.version}")
    print(f"Config max_retries: {harness.current_state.get_config('max_retries')}")

    # 4. Simulate Revert of the Weather Tool Mutation
    print("\n--- Simulating Deprecation / Revert of Weather Tool ---")
    print(f"Reverting mutation: {mutation_id}...")
    harness.revert(mutation_id, reason="Weather API service deprecated and incompatible")

    print("\n--- Post-Revert State Verification ---")
    print(f"Current Harness Version: {harness.current_state.version}")
    print(f"Active Tools (weather removed): {list(harness.current_state.tools.keys())}")
    print(f"Routing Config (forecast route removed): {harness.current_state.get_config('tool_routing')}")
    print(f"Unrelated Config (max_retries preserved at 5): {harness.current_state.get_config('max_retries')}")

    # 5. Audit History
    print("\n--- Complete Audit History ---")
    print(json.dumps(harness.history(), indent=2))
    print("\n[SUCCESS] EvoUndo demo execution completed successfully.")


if __name__ == "__main__":
    run_demo()
