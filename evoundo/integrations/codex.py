"""OpenAI Codex Coding Agent EvoUndo Integration.

Provides official OpenAI Codex App Server JSON-RPC 2.0 stdio protocol integration,
real-time lifecycle interception (turn/started, item/started, item/completed, turn/completed),
mutation journaling, deterministic identity derivation, and compensation triggers
for the OpenAI Codex agent harness.
"""

from __future__ import annotations
import concurrent.futures
import functools
import json
import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.core.harness import EvoUndoHarness
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType
from evoundo.identity import MutationIdentity
from evoundo.integrations.base import (
    AgentFrameworkAdapter,
    FrameworkContext,
    ToolClassification,
    _build_recovery_op,
)
from evoundo.observability.events import EventType
from evoundo.observability.logging import StructuredEventLogger, default_event_logger
from evoundo.reconciliation.reconciler import (
    JournalEntry,
    MutationReconciler,
    ReconciliationDecision,
    ReconciliationStatus,
)
from evoundo.recovery.operations import (
    BaseRecoveryOp,
    CustomRecoveryOp,
    DriverRecoveryOp,
    RecoveryProgram,
)
from evoundo.witness.stores import Witness

logger = logging.getLogger("evoundo.integrations.codex")

DEFAULT_CODEX_BIN_PATH = "/Applications/ChatGPT.app/Contents/Resources/codex"


class CodexAppServerError(RuntimeError):
    """Exception raised for errors returned by the Codex App Server JSON-RPC interface."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"Codex App Server RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class CodexAppServerClient:
    """Client for the official OpenAI Codex App Server running JSON-RPC 2.0 over stdio.

    Manages subprocess execution of `codex app-server --listen stdio://`,
    handling request/response correlation, notifications, and event streams.
    """

    def __init__(
        self,
        binary_path: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
    ):
        self.binary_path = binary_path or os.environ.get("CODEX_BIN_PATH", DEFAULT_CODEX_BIN_PATH)
        self.cwd = cwd or os.getcwd()
        self.env = env
        self.default_timeout = timeout

        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._req_id = 0
        self._lock = threading.Lock()
        self._pending_requests: Dict[int, concurrent.futures.Future] = {}
        self._notification_handlers: List[Callable[[str, Dict[str, Any]], None]] = []
        self._raw_event_log: List[Dict[str, Any]] = []
        self._is_running = False
        self._server_info: Dict[str, Any] = {}

    @property
    def is_running(self) -> bool:
        return self._is_running and self._process is not None and self._process.poll() is None

    @property
    def server_info(self) -> Dict[str, Any]:
        return self._server_info

    @property
    def raw_event_log(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._raw_event_log)

    def start(self) -> None:
        """Launch the Codex App Server process and start background stdio reader thread."""
        if self._is_running and self._process is not None and self._process.poll() is None:
            return

        if not os.path.isfile(self.binary_path):
            raise FileNotFoundError(
                f"OpenAI Codex binary not found at '{self.binary_path}'. "
                f"Set CODEX_BIN_PATH or ensure ChatGPT.app is installed."
            )

        cmd = [self.binary_path, "app-server", "--listen", "stdio://"]
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.cwd,
            env=self.env,
        )
        self._is_running = True

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="CodexAppServerStdioReader",
            daemon=True,
        )
        self._reader_thread.start()
        logger.info("Started Codex App Server process (PID: %d)", self._process.pid)

    def _reader_loop(self) -> None:
        """Continuously read newline-delimited JSON-RPC messages from app-server stdout."""
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning("Unparseable line from Codex App Server: %s", line_str)
                continue

            with self._lock:
                self._raw_event_log.append(msg)

            msg_id = msg.get("id")
            if msg_id is not None and ("result" in msg or "error" in msg):
                with self._lock:
                    fut = self._pending_requests.pop(msg_id, None)
                if fut and not fut.done():
                    if "error" in msg and msg["error"]:
                        err = msg["error"]
                        fut.set_exception(
                            CodexAppServerError(
                                code=err.get("code", -1),
                                message=err.get("message", "Unknown RPC error"),
                                data=err.get("data"),
                            )
                        )
                    else:
                        fut.set_result(msg.get("result"))
            elif "method" in msg:
                method = msg["method"]
                params = msg.get("params", {})
                for handler in list(self._notification_handlers):
                    try:
                        handler(method, params)
                    except Exception as e:
                        logger.exception("Error in Codex App Server notification handler: %s", e)

        self._is_running = False

    def send_request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send a JSON-RPC 2.0 request over stdio and wait for matching response."""
        if not self.is_running:
            self.start()

        with self._lock:
            self._req_id += 1
            req_id = self._req_id
            fut: concurrent.futures.Future = concurrent.futures.Future()
            self._pending_requests[req_id] = fut

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        data = json.dumps(payload) + "\n"

        assert self._process is not None and self._process.stdin is not None
        try:
            self._process.stdin.write(data)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            with self._lock:
                self._pending_requests.pop(req_id, None)
            raise RuntimeError(f"Failed writing to Codex App Server stdio pipe: {e}") from e

        eff_timeout = timeout if timeout is not None else self.default_timeout
        return fut.result(timeout=eff_timeout)

    def send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a JSON-RPC 2.0 notification over stdio without expecting a response."""
        if not self.is_running:
            self.start()

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        data = json.dumps(payload) + "\n"

        assert self._process is not None and self._process.stdin is not None
        try:
            self._process.stdin.write(data)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f"Failed sending notification to Codex App Server: {e}") from e

    def initialize(
        self,
        client_name: str = "evoundo_codex",
        client_version: str = "0.1.0",
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Perform the mandatory JSON-RPC `initialize` handshake."""
        params = {
            "clientInfo": {"name": client_name, "version": client_version},
            "capabilities": capabilities or {},
        }
        res = self.send_request("initialize", params)
        self._server_info = res if isinstance(res, dict) else {}
        return self._server_info

    def start_thread(
        self,
        ephemeral: bool = True,
        approval_policy: str = "never",
        sandbox: str = "danger-full-access",
        cwd: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Call `thread/start` on the Codex App Server to initiate an agent thread."""
        params: Dict[str, Any] = {
            "ephemeral": ephemeral,
            "approvalPolicy": approval_policy,
            "sandbox": sandbox,
            "cwd": cwd or self.cwd,
            **kwargs,
        }
        return self.send_request("thread/start", params)

    def start_turn(
        self,
        thread_id: str,
        prompt_or_input: Union[str, List[Dict[str, Any]]],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Call `turn/start` on the Codex App Server to begin executing a user prompt."""
        if isinstance(prompt_or_input, str):
            input_items = [{"type": "text", "text": prompt_or_input}]
        else:
            input_items = prompt_or_input

        params: Dict[str, Any] = {
            "threadId": thread_id,
            "input": input_items,
            **kwargs,
        }
        return self.send_request("turn/start", params)

    def exec_command(
        self,
        command: Union[str, List[str]],
        cwd: Optional[str] = None,
        sandbox_policy: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run a standalone command via `command/exec` on the Codex App Server."""
        if isinstance(command, str):
            cmd_list = shlex.split(command)
        else:
            cmd_list = list(command)

        params: Dict[str, Any] = {
            "command": cmd_list,
            "cwd": cwd or self.cwd,
            "sandboxPolicy": sandbox_policy or {"type": "dangerFullAccess"},
        }
        if env:
            params["env"] = env

        return self.send_request("command/exec", params, timeout=timeout)

    def add_notification_handler(self, handler: Callable[[str, Dict[str, Any]], None]) -> None:
        """Register a callback for incoming server notifications."""
        with self._lock:
            if handler not in self._notification_handlers:
                self._notification_handlers.append(handler)

    def remove_notification_handler(self, handler: Callable[[str, Dict[str, Any]], None]) -> None:
        """Unregister an incoming notification callback."""
        with self._lock:
            if handler in self._notification_handlers:
                self._notification_handlers.remove(handler)

    def close(self) -> None:
        """Shut down the app-server subprocess and clean up stdio resources."""
        if not self._is_running and self._process is None:
            return

        self._is_running = False
        if self._process is not None:
            try:
                if self._process.stdin and not self._process.stdin.closed:
                    self._process.stdin.close()
            except Exception:
                pass

            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2.0)

            self._process = None

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None

        logger.info("Closed Codex App Server client.")

    def __enter__(self) -> CodexAppServerClient:
        self.start()
        self.initialize()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class CodexJsonRpcInterceptor:
    """Intercepter for Codex App Server JSON-RPC lifecycle events.

    Intercepts official lifecycle notifications:
      - `turn/started`
      - `item/started` (commandExecution, mcpToolCall, dynamicToolCall, fileChange)
      - `item/completed`
      - `turn/completed`

    Binds EvoUndo mutation envelopes, deterministic identities, pre-witness capture,
    reconciliation duplicate suppression, journal commitment, and compensation triggers.
    """

    def __init__(
        self,
        harness: Optional[EvoUndoHarness] = None,
        event_logger: Optional[StructuredEventLogger] = None,
        reconciler: Optional[MutationReconciler] = None,
    ):
        self.harness = harness or EvoUndoHarness()
        self.event_logger = event_logger or (self.harness.event_logger if self.harness else default_event_logger)
        self.reconciler = reconciler or getattr(self.harness, "reconciler", None) or MutationReconciler(event_logger=self.event_logger)

        self._active_turns: Dict[str, Dict[str, Any]] = {}
        self._active_items: Dict[str, Dict[str, Any]] = {}
        self._surface_bindings: Dict[str, Dict[str, Any]] = {}
        self._journal_traces: List[Dict[str, Any]] = []
        self._intercepted_events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._client: Optional[CodexAppServerClient] = None

    @property
    def journal_traces(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._journal_traces)

    @property
    def intercepted_events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._intercepted_events)

    def bind_client(self, client: CodexAppServerClient) -> None:
        """Attach this interceptor to a CodexAppServerClient notification stream."""
        self._client = client
        client.add_notification_handler(self.handle_notification)

    def register_surface_binding(
        self,
        target: str,
        surface: str = "redis",
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        declared_effects: Optional[List[Effect]] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register an external datastore surface binding for a specific target or tool."""
        with self._lock:
            self._surface_bindings[target] = {
                "surface": surface,
                "capture_fn": capture_fn,
                "inverse_fn": inverse_fn,
                "post_condition_probe": post_condition_probe,
                "post_condition_validator": post_condition_validator,
                "declared_effects": declared_effects or [
                    Effect(category=EffectCategory.RESOURCES, target=target, op_type=EffectOpType.UPDATE)
                ],
                "parameters": parameters or {},
            }

    def handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Dispatch an incoming JSON-RPC server notification to the appropriate lifecycle hook."""
        with self._lock:
            self._intercepted_events.append({
                "method": method,
                "params": params,
                "timestamp": time.time(),
            })

        if method == "turn/started":
            self.on_turn_started(params)
        elif method == "item/started":
            self.on_item_started(params)
        elif method == "item/completed":
            self.on_item_completed(params)
        elif method == "turn/completed":
            self.on_turn_completed(params)

    def handle_event_json(self, json_str: str) -> None:
        """Parse and process a raw JSON-RPC event string."""
        msg = json.loads(json_str)
        method = msg.get("method")
        params = msg.get("params", {})
        if method:
            self.handle_notification(method, params)

    def on_turn_started(self, params: Dict[str, Any]) -> None:
        """Handle `turn/started` notification to bind the execution turn scope."""
        thread_id = params.get("threadId", "default_thread")
        turn = params.get("turn", {})
        turn_id = turn.get("id") or f"turn_{uuid.uuid4().hex[:8]}"

        with self._lock:
            self._active_turns[turn_id] = {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "started_at": time.time(),
                "items": [],
            }

        self.event_logger.emit(
            event_type=EventType.MUTATION_PROPOSED,
            mutation_id=turn_id,
            message=f"Codex turn started: thread={thread_id} turn={turn_id}",
        )

    def on_item_started(self, params: Dict[str, Any]) -> None:
        """Handle `item/started` notification for mutating tools (`commandExecution`, `mcpToolCall`)."""
        item = params.get("item", {})
        item_type = item.get("type", "")
        item_id = item.get("id") or f"item_{uuid.uuid4().hex[:8]}"
        thread_id = params.get("threadId", "default_thread")
        turn_id = params.get("turnId")
        if not turn_id and self._active_turns:
            turn_id = list(self._active_turns.keys())[-1]
        turn_id = turn_id or "default_turn"

        if item_type in ("commandExecution", "mcpToolCall", "dynamicToolCall", "fileChange"):
            tool_name = item_type
            target_name = "codex_tool"

            if item_type == "commandExecution":
                cmd = item.get("command", "")
                tool_name = "commandExecution"
                target_name = "command_exec"
                for target_key in self._surface_bindings:
                    if target_key in cmd:
                        target_name = target_key
                        break
            elif item_type == "mcpToolCall":
                tool_name = item.get("tool", "mcpToolCall")
                target_name = tool_name
                args_str = json.dumps(item.get("arguments", {}))
                for target_key in self._surface_bindings:
                    if target_key == tool_name or target_key in args_str:
                        target_name = target_key
                        break
            elif item_type == "dynamicToolCall":
                tool_name = item.get("tool", "dynamicToolCall")
                target_name = tool_name
            elif item_type == "fileChange":
                tool_name = "fileChange"
                target_name = "fileChange"

            binding = self._surface_bindings.get(target_name, {})
            surface = binding.get("surface", "custom")
            capture_fn = binding.get("capture_fn")
            probe = binding.get("post_condition_probe")
            validator = binding.get("post_condition_validator")
            declared_effects = binding.get("declared_effects") or [
                Effect(category=EffectCategory.RESOURCES, target=target_name, op_type=EffectOpType.UPDATE)
            ]

            ctx = FrameworkContext(
                framework_name="codex",
                session_id=thread_id,
                run_id=turn_id,
                agent_id="codex_app_server",
                tool_name=tool_name,
                tool_call_id=item_id,
                metadata={
                    "item_type": item_type,
                    "command": item.get("command"),
                    "cwd": item.get("cwd"),
                    "process_id": item.get("processId"),
                    "tool": item.get("tool"),
                    "server": item.get("server"),
                    "arguments": item.get("arguments"),
                    "target": target_name,
                    "surface": surface,
                },
            )
            logical_id = ctx.derive_logical_id()

            identity = MutationIdentity(
                logical_mutation_id=logical_id,
                framework="codex",
                framework_run_id=turn_id,
                tool_name=tool_name,
                tool_call_id=item_id,
                retry_attempt=0,
                metadata=ctx.metadata,
            )

            # Check duplicate / evaluate reconciliation
            rec_decision = self.reconciler.evaluate_request(
                identity=identity,
                current_state_probe=probe,
                expected_post_condition=validator,
            )

            # Capture pre-state witness
            witness_val = None
            if capture_fn:
                try:
                    witness_val = capture_fn()
                except Exception as e:
                    logger.warning("Error capturing witness for %s: %s", target_name, e)
            elif surface == "redis":
                try:
                    import redis
                    r_url = os.environ.get("EVOUNDO_REDIS_URL", "redis://localhost:6379/0")
                    rc = redis.Redis.from_url(r_url, decode_responses=False)
                    if rc.exists(target_name):
                        witness_val = rc.get(target_name)
                except Exception:
                    pass

            self.reconciler.record_witness(logical_id, {target_name: witness_val})

            with self._lock:
                self._active_items[item_id] = {
                    "item_id": item_id,
                    "item_type": item_type,
                    "tool_name": tool_name,
                    "target_name": target_name,
                    "surface": surface,
                    "logical_id": logical_id,
                    "identity": identity,
                    "context": ctx,
                    "witness_val": witness_val,
                    "binding": binding,
                    "rec_decision": rec_decision,
                    "started_at": time.time(),
                }
                if turn_id in self._active_turns:
                    self._active_turns[turn_id]["items"].append(item_id)

    def on_item_completed(self, params: Dict[str, Any]) -> None:
        """Handle `item/completed` notification: record journal entry, bind recovery op, or trigger compensation."""
        item = params.get("item", {})
        item_id = item.get("id")

        with self._lock:
            tracked = self._active_items.get(item_id)
        if not tracked:
            return

        logical_id = tracked["logical_id"]
        identity = tracked["identity"]
        target_name = tracked["target_name"]
        surface = tracked["surface"]
        witness_val = tracked["witness_val"]
        binding = tracked["binding"]
        inverse_fn = binding.get("inverse_fn")
        declared_effects = binding.get("declared_effects") or [
            Effect(category=EffectCategory.RESOURCES, target=target_name, op_type=EffectOpType.UPDATE)
        ]

        item_type = tracked["item_type"]
        is_success = True
        err_msg = None

        if item_type == "commandExecution":
            exit_code = item.get("exitCode")
            if exit_code is not None and exit_code != 0:
                is_success = False
                err_msg = f"Command execution failed with exit code {exit_code}: {item.get('aggregatedOutput', '')}"
        elif item_type in ("mcpToolCall", "dynamicToolCall"):
            status = item.get("status")
            error = item.get("error")
            if status == "failed" or error:
                is_success = False
                err_msg = str(error) if error else f"{item_type} completed with status '{status}'"

        if not is_success:
            self.reconciler.record_failed(logical_id, err_msg or "Failed item execution")
            self.trigger_compensation(logical_id)
            trace_record = {
                "logical_mutation_id": logical_id,
                "item_id": item_id,
                "item_type": item_type,
                "target": target_name,
                "surface": surface,
                "status": "FAILED",
                "error": err_msg,
                "timestamp": time.time(),
            }
            with self._lock:
                self._journal_traces.append(trace_record)
            return

        result_val = item.get("aggregatedOutput") or item.get("result") or "COMPLETED"
        self.reconciler.record_mutation_executed(
            logical_mutation_id=logical_id,
            result=result_val,
            effects=[e.to_dict() for e in declared_effects],
        )

        # Build recovery operation and bind RecoveryProgram
        op = _build_recovery_op(surface, target_name, logical_id, witness_val, inverse_fn, result_val)
        prog = RecoveryProgram(operations=[op])

        if self.harness and hasattr(self.harness, "record_external_protected_mutation"):
            witness = Witness(mutation_id=logical_id, data={target_name: witness_val})
            self.harness.record_external_protected_mutation(
                mutation_id=logical_id,
                description=f"Protected [codex] {item_type} '{target_name}'",
                witness=witness,
                recovery_program=prog,
                declared_effects=declared_effects,
                identity=identity,
            )

        self.reconciler.record_committed(logical_id)

        trace_record = {
            "logical_mutation_id": logical_id,
            "item_id": item_id,
            "item_type": item_type,
            "target": target_name,
            "surface": surface,
            "status": "COMMITTED",
            "result": str(result_val)[:200],
            "timestamp": time.time(),
        }
        with self._lock:
            self._journal_traces.append(trace_record)

    def on_turn_completed(self, params: Dict[str, Any]) -> None:
        """Handle `turn/completed` notification to finalize turn records."""
        turn = params.get("turn", {})
        turn_id = turn.get("id")
        if turn_id and turn_id in self._active_turns:
            with self._lock:
                self._active_turns[turn_id]["completed_at"] = time.time()
                self._active_turns[turn_id]["status"] = turn.get("status", "completed")

        self.event_logger.emit(
            event_type=EventType.EXECUTION_COMMITTED,
            mutation_id=turn_id or "turn_unknown",
            message=f"Codex turn completed: {turn_id}",
        )

    def trigger_compensation(self, target_id: str) -> bool:
        """Execute compensating recovery for a given mutation ID or item ID."""
        logical_id = target_id
        with self._lock:
            if target_id in self._active_items:
                logical_id = self._active_items[target_id]["logical_id"]

        if self.harness and hasattr(self.harness, "mutation_registry"):
            rec = self.harness.mutation_registry.inspect_mutation(logical_id)
            if rec and rec.status == "ACTIVE":
                try:
                    self.harness.revert(logical_id, reason="Codex compensation trigger")
                    return True
                except Exception as e:
                    logger.error("Failed to compensate mutation %s: %s", logical_id, e)
                    return False
        return False


class CodexAdapter(AgentFrameworkAdapter):
    """Adapter for official OpenAI Codex App Server JSON-RPC integration and tool hook lifecycle."""

    @property
    def framework_name(self) -> str:
        return "codex"

    def classify_tool(
        self,
        tool_name: str,
        tool_callable: Optional[Callable[..., Any]] = None,
    ) -> ToolClassification:
        """Classify tool actions: read-only actions bypass mutation tracking."""
        read_only_prefixes = ("view_", "read_", "search_", "list_", "inspect_", "cat", "ls", "grep")
        if tool_name.startswith(read_only_prefixes):
            return ToolClassification.READ_ONLY
        return ToolClassification.MUTATING

    def create_client(
        self,
        binary_path: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> CodexAppServerClient:
        """Factory creating a configured CodexAppServerClient."""
        return CodexAppServerClient(
            binary_path=binary_path,
            cwd=cwd,
            timeout=timeout,
        )

    def create_interceptor(self) -> CodexJsonRpcInterceptor:
        """Factory creating a CodexJsonRpcInterceptor bound to this adapter's harness."""
        return CodexJsonRpcInterceptor(
            harness=self.harness,
            event_logger=self.event_logger,
            reconciler=self.reconciler,
        )

    def exec_protected_command(
        self,
        command: Union[str, List[str]],
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
        client: Optional[CodexAppServerClient] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        sandbox_policy: Optional[Dict[str, Any]] = None,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute a standalone command via Codex App Server `command/exec` protected by EvoUndo."""
        cmd_list = [command] if isinstance(command, str) else list(command)
        cmd_str = " ".join(cmd_list)
        target_name = target or cmd_list[0]
        tool_call_id = tool_call_id or f"exec_{uuid.uuid4().hex[:8]}"

        chosen_session_id = session_id
        chosen_run_id = run_id
        if not chosen_session_id and not chosen_run_id:
            chosen_run_id = f"codex_run_{uuid.uuid4().hex[:8]}"

        ctx = FrameworkContext(
            framework_name=self.framework_name,
            session_id=chosen_session_id,
            run_id=chosen_run_id,
            agent_id="codex_agent",
            tool_name=target_name,
            tool_call_id=tool_call_id,
            metadata={"command": cmd_str, **(metadata or {})},
        )
        logical_id = ctx.derive_logical_id()

        identity = MutationIdentity(
            logical_mutation_id=logical_id,
            framework=self.framework_name,
            framework_run_id=ctx.run_id,
            tool_name=target_name,
            tool_call_id=tool_call_id,
            retry_attempt=0,
            metadata=ctx.metadata,
        )

        rec_decision = self.reconciler.evaluate_request(
            identity=identity,
            current_state_probe=post_condition_probe,
            expected_post_condition=post_condition_validator,
        )

        if not rec_decision.should_execute_fn:
            return rec_decision.cached_result or {
                "exitCode": 0,
                "stdout": "",
                "stderr": "",
                "suppressed": True,
            }

        # Capture Pre-State Witness
        witness_val = None
        if capture_fn:
            try:
                witness_val = capture_fn()
            except Exception as e:
                self.event_logger.emit(
                    event_type=EventType.ERROR,
                    mutation_id=logical_id,
                    message=f"Failed to capture witness for {target_name}: {e}",
                )
        elif surface == "redis":
            try:
                import redis
                r_url = os.environ.get("EVOUNDO_REDIS_URL", "redis://localhost:6379/0")
                rc = redis.Redis.from_url(r_url, decode_responses=False)
                if rc.exists(target_name):
                    witness_val = rc.get(target_name)
            except Exception:
                pass

        self.reconciler.record_witness(logical_id, {target_name: witness_val})

        # Execute command through Codex App Server
        owns_client = False
        if client is None:
            client = self.create_client(cwd=cwd)
            client.start()
            client.initialize()
            owns_client = True

        try:
            res = client.exec_command(
                command=cmd_list,
                cwd=cwd,
                sandbox_policy=sandbox_policy or {"type": "dangerFullAccess"},
                timeout=timeout,
            )
        finally:
            if owns_client:
                client.close()

        exit_code = res.get("exitCode", 1)
        if exit_code != 0:
            err_msg = res.get("stderr") or f"Command failed with exitCode={exit_code}"
            self.reconciler.record_failed(logical_id, err_msg)
            return res

        # Record executed
        eff_list = declared_effects or [
            Effect(category=EffectCategory.RESOURCES, target=target_name, op_type=EffectOpType.UPDATE)
        ]
        self.reconciler.record_mutation_executed(
            logical_mutation_id=logical_id,
            result=res,
            effects=[e.to_dict() for e in eff_list],
        )

        # Register recovery operation with harness
        if self.harness:
            witness = Witness(mutation_id=logical_id, data={target_name: witness_val})
            op = _build_recovery_op(surface, target_name, logical_id, witness_val, inverse_fn, res)
            prog = RecoveryProgram(operations=[op])
            if hasattr(self.harness, "record_external_protected_mutation"):
                self.harness.record_external_protected_mutation(
                    mutation_id=logical_id,
                    description=f"Protected [codex] command '{target_name}'",
                    witness=witness,
                    recovery_program=prog,
                    declared_effects=eff_list,
                    identity=identity,
                )

        self.reconciler.record_committed(logical_id)
        return res

    def run_protected_turn(
        self,
        prompt: str,
        thread_id: Optional[str] = None,
        client: Optional[CodexAppServerClient] = None,
        surface_bindings: Optional[Dict[str, Dict[str, Any]]] = None,
        timeout: float = 45.0,
    ) -> Dict[str, Any]:
        """Execute a full interactive turn on the Codex App Server with live event interception."""
        owns_client = False
        if client is None:
            client = self.create_client()
            client.start()
            client.initialize()
            owns_client = True

        interceptor = self.create_interceptor()
        interceptor.bind_client(client)
        if surface_bindings:
            for tgt, cfg in surface_bindings.items():
                interceptor.register_surface_binding(target=tgt, **cfg)

        try:
            if not thread_id:
                t_res = client.start_thread(
                    ephemeral=True,
                    approval_policy="never",
                    sandbox="danger-full-access",
                )
                thread_id = t_res["thread"]["id"]

            turn_completed_event = threading.Event()

            def on_turn_done(method: str, params: Dict[str, Any]) -> None:
                if method == "turn/completed":
                    turn_completed_event.set()

            client.add_notification_handler(on_turn_done)
            turn_res = client.start_turn(thread_id=thread_id, prompt_or_input=prompt)
            turn_id = turn_res.get("turn", {}).get("id")

            # Wait for turn completion
            turn_completed_event.wait(timeout=timeout)

            return {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "interceptor": interceptor,
                "journal_traces": interceptor.journal_traces,
                "intercepted_events": interceptor.intercepted_events,
            }
        finally:
            if owns_client:
                client.close()

    def create_tool_hook(
        self,
        surface: str = "custom",
        target: Optional[str] = None,
        declared_effects: Optional[List[Effect]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        inverse_fn: Optional[Callable[[Any, Any], Any]] = None,
        post_condition_probe: Optional[Callable[[], Any]] = None,
        post_condition_validator: Optional[Callable[[Any], bool]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Wrap a Codex agent tool with EvoUndo lifecycle and recovery (backward-compatible)."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = target or getattr(fn, "__name__", "codex_tool")

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                codex_meta: Dict[str, Any] = kwargs.pop("__codex_context", {}) or {}
                ctx = FrameworkContext(
                    framework_name=self.framework_name,
                    session_id=codex_meta.get("thread_id", kwargs.pop("thread_id", None)),
                    run_id=codex_meta.get("run_id", kwargs.pop("run_id", None)),
                    agent_id="codex_agent",
                    tool_name=tool_name,
                    tool_call_id=codex_meta.get("step_id", kwargs.pop("tool_call_id", None)),
                    logical_mutation_id=codex_meta.get("logical_mutation_id", kwargs.pop("__logical_mutation_id", None)),
                    retry_attempt=int(codex_meta.get("retry_attempt", kwargs.pop("retry_attempt", 0))),
                    metadata={"runtime": "codex_cli", **codex_meta.get("metadata", {})},
                )
                return self.execute_protected_tool(
                    tool_fn=fn,
                    context=ctx,
                    tool_args=args,
                    tool_kwargs=kwargs,
                    surface=surface,
                    target=tool_name,
                    declared_effects=declared_effects or [
                        Effect(category=EffectCategory.RESOURCES, target=tool_name, op_type=EffectOpType.UPDATE)
                    ],
                    capture_fn=capture_fn,
                    inverse_fn=inverse_fn,
                    post_condition_probe=post_condition_probe,
                    post_condition_validator=post_condition_validator,
                )

            setattr(wrapper, "_is_evoundo_protected", True)
            setattr(wrapper, "_evoundo_adapter", self)
            return wrapper

        return decorator


__all__ = [
    "CodexAdapter",
    "CodexAppServerClient",
    "CodexJsonRpcInterceptor",
    "CodexAppServerError",
    "DEFAULT_CODEX_BIN_PATH",
]
