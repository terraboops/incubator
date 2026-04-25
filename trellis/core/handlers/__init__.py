"""Handler abstraction for role execution.

Per the custom-stages spec (see `.claude-autonav/notes/custom-stages.md`),
every role execution — agent, script, webhook, human, k8s_job — satisfies
the same contract: JSON-in (HandlerContext) → JSON-out (HandlerResponse).

This module is pure scaffolding for now: the scheduler still invokes agents
directly. Later tasks wire the scheduler through `create_handler`, add
timeouts/retry, and ship the remaining handler types.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from trellis.core.agent import AgentResult, BaseAgent

logger = logging.getLogger(__name__)

State = Literal["proceed", "iterate", "needs_review", "failed"]


@dataclass
class HandlerContext:
    """Input payload (stdin shape) passed to every handler invocation."""

    idea_id: str
    stage: str
    role: str
    blackboard_dir: str
    workspace_dir: str
    iteration: int = 1
    previous_state: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    deadline: datetime | None = None
    event: str = "role.service"

    def to_json(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "idea_id": self.idea_id,
            "stage": self.stage,
            "role": self.role,
            "iteration": self.iteration,
            "blackboard_dir": self.blackboard_dir,
            "workspace_dir": self.workspace_dir,
            "previous_state": self.previous_state,
            "inputs": self.inputs,
        }


@dataclass
class HandlerResponse:
    """Output payload (stdout shape) returned by every handler."""

    state: State
    reason: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)

    # Agent-specific internal fields; not part of the JSON wire shape.
    session_id: str | None = None
    sandbox_failure: bool = False
    transcript: list[dict] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "artifacts": self.artifacts,
            "cost_usd": self.cost_usd,
            "metrics": self.metrics,
        }


class Handler(ABC):
    """Role handler interface. Every concrete handler speaks JSON-in/JSON-out."""

    @abstractmethod
    async def serve(self, context: HandlerContext) -> HandlerResponse:
        """Execute the role. Must not raise on expected failures — map them
        to `state="iterate"` or `state="failed"` instead."""


class AgentHandler(Handler):
    """Wraps a BaseAgent so it conforms to the Handler interface."""

    def __init__(self, agent: BaseAgent) -> None:
        self.agent = agent

    async def serve(self, context: HandlerContext) -> HandlerResponse:
        result = await self.agent.run(context.idea_id, deadline=context.deadline)
        return self._result_to_response(result)

    @staticmethod
    def _result_to_response(result: AgentResult) -> HandlerResponse:
        # Today the scheduler reads phase_recommendation from blackboard
        # status after the agent returns and bridges it into lifecycle
        # state. Here we only map the raw AgentResult shape — proceed on
        # success, iterate on any failure or sandbox breach.
        state: State = "proceed" if result.success and not result.sandbox_failure else "iterate"
        reason = result.error or result.stop_reason or ""
        return HandlerResponse(
            state=state,
            reason=reason,
            cost_usd=result.cost_usd,
            metrics={"stop_reason": result.stop_reason} if result.stop_reason else {},
            session_id=result.session_id,
            sandbox_failure=result.sandbox_failure,
            transcript=result.transcript,
        )


def create_handler(role_config: dict[str, Any], *, agent: BaseAgent | None = None) -> Handler:
    """Build a Handler from role config.

    Dispatches on `role_config["handler"]`. An omitted `handler` key
    defaults to `"agent"` for backwards compatibility with today's pipelines.
    Supported: `agent`, `script`, `webhook`. (`human`, `k8s_job` ship later.)

    If `role_config` includes a `retry` block or a `timeout`, the returned
    handler is wrapped with `with_retry` so every type (agent, script, ...)
    gets the same timeout + retry behaviour for free.
    """
    handler_type = role_config.get("handler", "agent")

    base: Handler
    if handler_type == "agent":
        if agent is None:
            raise ValueError("AgentHandler requires a BaseAgent via the `agent=` kwarg")
        base = AgentHandler(agent)
    elif handler_type == "script":
        base = ScriptHandler.from_role_config(role_config)
    elif handler_type == "webhook":
        base = WebhookHandler.from_role_config(role_config)
    elif handler_type == "human":
        base = HumanHandler.from_role_config(role_config)
    elif handler_type == "k8s_job":
        base = K8sJobHandler.from_role_config(role_config)
    else:
        raise NotImplementedError(
            f"Handler type {handler_type!r} is not a known handler type. "
            "Supported: agent, script, webhook, human, k8s_job."
        )

    policy = RetryPolicy.from_role_config(role_config)
    if policy.is_active():
        return with_retry(base, policy)
    return base


# ─── ScriptHandler ────────────────────────────────────────────────────────


class ScriptHandler(Handler):
    """Run a shell command; JSON on stdin, JSON on stdout.

    role_config::

        name: deploy-to-prod
        handler: script
        command: "./bin/deploy v1"      # string (shlex-split) or list
        env: {FOO: bar}                 # optional extra env vars
        cwd: "./workspace"              # optional; defaults to blackboard_dir

    Legacy exit-code mode per spec: if the subprocess exits cleanly without
    JSON on stdout, exit=0 → proceed; exit!=0 → iterate.
    """

    def __init__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        if not command:
            raise ValueError("ScriptHandler requires a non-empty command")
        self.command = command
        self.extra_env = env or {}
        self.cwd = cwd

    @classmethod
    def from_role_config(cls, role_config: dict[str, Any]) -> "ScriptHandler":
        cmd = role_config.get("command")
        if isinstance(cmd, str):
            cmd_list = shlex.split(cmd)
        elif isinstance(cmd, list):
            cmd_list = [str(s) for s in cmd]
        else:
            raise ValueError("ScriptHandler role config must have `command` (string or list)")
        return cls(
            cmd_list,
            env=role_config.get("env"),
            cwd=role_config.get("cwd"),
        )

    async def serve(self, context: HandlerContext) -> HandlerResponse:
        stdin_payload = json.dumps(context.to_json()).encode()
        cwd = self.cwd or context.workspace_dir or context.blackboard_dir

        env = {**os.environ, **self.extra_env}

        # Run in a worker thread to keep the event loop free.
        proc = await asyncio.to_thread(
            subprocess.run,
            self.command,
            input=stdin_payload,
            capture_output=True,
            cwd=cwd,
            env=env,
            check=False,
        )
        stdout_text = proc.stdout.decode(errors="replace").strip()
        stderr_text = proc.stderr.decode(errors="replace").strip()

        # Try JSON mode first.
        if stdout_text:
            try:
                parsed = json.loads(stdout_text)
                if isinstance(parsed, dict) and "state" in parsed:
                    return HandlerResponse(
                        state=parsed["state"],
                        reason=parsed.get("reason", ""),
                        artifacts=parsed.get("artifacts", {}) or {},
                        cost_usd=float(parsed.get("cost_usd") or 0.0),
                        metrics=parsed.get("metrics", {}) or {},
                    )
            except json.JSONDecodeError:
                pass

        # Legacy exit-code mode.
        if proc.returncode == 0:
            return HandlerResponse(state="proceed", reason=stdout_text[:500])
        last_stderr = stderr_text.splitlines()[-1] if stderr_text else ""
        return HandlerResponse(
            state="iterate",
            reason=last_stderr or f"exit {proc.returncode}",
        )


# ─── WebhookHandler ───────────────────────────────────────────────────────


class WebhookHandler(Handler):
    """POST context JSON to a URL; parse JSON response as HandlerResponse.

    role_config::

        name: notify
        handler: webhook
        url: "https://hooks.example.com/deploy"
        method: POST      # optional; default POST
        headers: {...}    # optional
    """

    def __init__(
        self,
        url: str,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
    ) -> None:
        if not url:
            raise ValueError("WebhookHandler requires a `url`")
        self.url = url
        self.method = method.upper()
        self.headers = headers or {}

    @classmethod
    def from_role_config(cls, role_config: dict[str, Any]) -> "WebhookHandler":
        return cls(
            role_config.get("url", ""),
            method=role_config.get("method", "POST"),
            headers=role_config.get("headers"),
        )

    async def serve(self, context: HandlerContext) -> HandlerResponse:
        import httpx

        headers = {"Content-Type": "application/json", **self.headers}
        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.request(
                self.method,
                self.url,
                json=context.to_json(),
                headers=headers,
            )

        if resp.status_code >= 500:
            return HandlerResponse(
                state="iterate",
                reason=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        if resp.status_code >= 400:
            return HandlerResponse(
                state="failed",
                reason=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )

        try:
            parsed = resp.json()
        except ValueError:
            parsed = {}

        if isinstance(parsed, dict) and parsed.get("state"):
            return HandlerResponse(
                state=parsed["state"],
                reason=parsed.get("reason", ""),
                artifacts=parsed.get("artifacts", {}) or {},
                cost_usd=float(parsed.get("cost_usd") or 0.0),
                metrics=parsed.get("metrics", {}) or {},
            )
        # 2xx without a parseable {state} body → assume proceed.
        return HandlerResponse(state="proceed", reason=resp.text[:200])


# ─── Timeouts + retry (per custom-stages spec §Timeouts) ──────────────────


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|h|hr)?\s*$", re.IGNORECASE)


def parse_duration(value: Any) -> float | None:
    """Parse ``"10m"`` / ``"30s"`` / ``"1h"`` / ``30`` / ``None`` → seconds.

    Returns None for missing/unparseable inputs rather than raising — callers
    treat missing durations as "no timeout".
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value)
    if not match:
        return None
    n = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    if unit in ("s", "sec", "secs"):
        return n
    if unit in ("m", "min"):
        return n * 60
    if unit in ("h", "hr"):
        return n * 3600
    return None


@dataclass
class RetryPolicy:
    """Per-role timeout + retry configuration.

    Maps to the spec's role config::

        - name: deploy-to-prod
          timeout: "10m"
          retry:
            max_attempts: 5
            backoff: exponential
            on_timeout: retry
            terminal_after: failed

    Passing an empty RetryPolicy() is a no-op — the handler runs once with
    no timeout and returns its result verbatim.
    """

    max_attempts: int = 1
    backoff: str = "exponential"  # "exponential" | "linear" | "fixed"
    initial_delay_sec: float = 30.0
    max_delay_sec: float = 300.0
    timeout_sec: float | None = None
    on_timeout: str = "retry"  # "retry" | "fail"
    terminal_state: State = "failed"

    def is_active(self) -> bool:
        """True when the policy changes behaviour vs. running the raw handler."""
        return self.max_attempts > 1 or self.timeout_sec is not None

    def backoff_for(self, attempt: int) -> float:
        """Delay before attempt `attempt` (0-indexed; returns 0 for attempt 0)."""
        if attempt <= 0:
            return 0.0
        if self.backoff == "fixed":
            delay = self.initial_delay_sec
        elif self.backoff == "linear":
            delay = self.initial_delay_sec * attempt
        else:  # exponential (default)
            delay = self.initial_delay_sec * (2 ** (attempt - 1))
        return min(delay, self.max_delay_sec)

    @classmethod
    def from_role_config(cls, role_config: dict[str, Any]) -> "RetryPolicy":
        retry_cfg = role_config.get("retry") or {}
        timeout = parse_duration(role_config.get("timeout"))

        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout_sec"] = timeout
        if "max_attempts" in retry_cfg:
            kwargs["max_attempts"] = int(retry_cfg["max_attempts"])
        if "backoff" in retry_cfg:
            kwargs["backoff"] = str(retry_cfg["backoff"])
        if "initial_delay" in retry_cfg:
            parsed = parse_duration(retry_cfg["initial_delay"])
            if parsed is not None:
                kwargs["initial_delay_sec"] = parsed
        if "max_delay" in retry_cfg:
            parsed = parse_duration(retry_cfg["max_delay"])
            if parsed is not None:
                kwargs["max_delay_sec"] = parsed
        if "on_timeout" in retry_cfg:
            kwargs["on_timeout"] = str(retry_cfg["on_timeout"])
        if "terminal_after" in retry_cfg:
            kwargs["terminal_state"] = retry_cfg["terminal_after"]
        return cls(**kwargs)


class _RetryingHandler(Handler):
    """Wraps a Handler with per-attempt timeout + retry with backoff."""

    def __init__(self, inner: Handler, policy: RetryPolicy) -> None:
        self.inner = inner
        self.policy = policy

    async def serve(self, context: HandlerContext) -> HandlerResponse:
        last: HandlerResponse | None = None
        for attempt in range(self.policy.max_attempts):
            delay = self.policy.backoff_for(attempt)
            if delay > 0:
                logger.info(
                    "Role %s/%s attempt %d: backing off %ds",
                    context.stage,
                    context.role,
                    attempt + 1,
                    int(delay),
                )
                await asyncio.sleep(delay)

            try:
                if self.policy.timeout_sec:
                    resp = await asyncio.wait_for(
                        self.inner.serve(context), timeout=self.policy.timeout_sec
                    )
                else:
                    resp = await self.inner.serve(context)
            except asyncio.TimeoutError:
                if self.policy.on_timeout == "fail":
                    return HandlerResponse(
                        state=self.policy.terminal_state,
                        reason=f"Timed out after {self.policy.timeout_sec}s (on_timeout=fail)",
                    )
                resp = HandlerResponse(
                    state="iterate",
                    reason=f"Timed out after {self.policy.timeout_sec}s",
                )
            except Exception as exc:  # handler raised — count as iterate
                logger.warning(
                    "Role %s/%s attempt %d raised: %s",
                    context.stage,
                    context.role,
                    attempt + 1,
                    exc,
                )
                resp = HandlerResponse(state="iterate", reason=str(exc))

            last = resp
            if resp.state in ("proceed", "needs_review"):
                return resp

        # Exhausted all attempts — terminal state.
        reason = last.reason if last else ""
        suffix = f"(exhausted {self.policy.max_attempts} attempts)"
        return HandlerResponse(
            state=self.policy.terminal_state,
            reason=f"{reason} {suffix}".strip() if reason else suffix,
            cost_usd=last.cost_usd if last else 0.0,
            artifacts=last.artifacts if last else {},
            metrics=last.metrics if last else {},
            session_id=last.session_id if last else None,
            sandbox_failure=last.sandbox_failure if last else False,
            transcript=last.transcript if last else [],
        )


def with_retry(handler: Handler, policy: RetryPolicy) -> Handler:
    """Wrap `handler` with the given retry/timeout policy."""
    if not policy.is_active():
        return handler
    return _RetryingHandler(handler, policy)


# ─── HumanHandler ─────────────────────────────────────────────────────────


class HumanHandler(Handler):
    """Surface the role to a human via a pending-request file on the blackboard.

    On invocation, writes a JSON request to
    ``<blackboard_dir>/human-requests/<role>.json`` so the UI (future work)
    can render it, and returns ``state=needs_review`` so the scheduler
    leaves the role alone until the UI submits a response.

    The UI submits via a future endpoint that writes a response file next
    to the request and calls `mark_role_state(..., resp.state)` directly.
    The handler itself is idempotent — re-running doesn't overwrite the
    user's in-progress answer.
    """

    def __init__(self, *, request_file_name: str = "request.json") -> None:
        self.request_file_name = request_file_name

    @classmethod
    def from_role_config(cls, role_config: dict[str, Any]) -> "HumanHandler":
        return cls(request_file_name=role_config.get("request_file", "request.json"))

    async def serve(self, context: HandlerContext) -> HandlerResponse:
        from pathlib import Path

        req_dir = Path(context.blackboard_dir) / "human-requests" / context.role
        req_dir.mkdir(parents=True, exist_ok=True)
        request_path = req_dir / self.request_file_name

        # Don't overwrite an in-progress request — re-emission should be a no-op.
        if not request_path.exists():
            request_path.write_text(json.dumps(context.to_json(), indent=2))

        return HandlerResponse(
            state="needs_review",
            reason=f"Awaiting human input at {request_path}",
            metrics={"request_file": str(request_path)},
        )


# ─── K8sJobHandler ────────────────────────────────────────────────────────


class K8sJobHandler(Handler):
    """Run a role inside a Kubernetes Job.

    The actual Kubernetes client is injected so this module doesn't force
    a `kubernetes` dependency on every install. Provide a client adapter
    that implements:

      - ``create_job(namespace: str, body: dict) -> str`` — returns job name
      - ``wait_for_completion(namespace: str, name: str) -> bool`` — True on success
      - ``read_result(namespace: str, name: str) -> dict | None`` — parsed JSON
      - ``cleanup(namespace: str, name: str) -> None``

    role_config::

        name: uptime-check
        handler: k8s_job
        image: "ghcr.io/example/uptime:latest"
        namespace: "trellis"
        resources: {cpu: "100m", memory: "128Mi"}
        result_key: "result.json"       # file in shared volume containing JSON
    """

    def __init__(
        self,
        image: str,
        *,
        client: Any | None = None,
        namespace: str = "default",
        resources: dict | None = None,
        result_key: str = "result.json",
        env: dict[str, str] | None = None,
    ) -> None:
        if not image:
            raise ValueError("K8sJobHandler requires `image`")
        self.image = image
        self.client = client
        self.namespace = namespace
        self.resources = resources or {}
        self.result_key = result_key
        self.env = env or {}

    @classmethod
    def from_role_config(cls, role_config: dict[str, Any]) -> "K8sJobHandler":
        return cls(
            image=role_config.get("image", ""),
            namespace=role_config.get("namespace", "default"),
            resources=role_config.get("resources"),
            result_key=role_config.get("result_key", "result.json"),
            env=role_config.get("env"),
        )

    def _build_job_body(self, context: HandlerContext) -> dict:
        # Minimal Job spec — the caller's adapter is expected to fill in
        # cluster-specific defaults (service account, volume mounts for
        # result retrieval, etc.). We just feed the context via a ConfigMap
        # reference that the adapter translates.
        resources = {
            "requests": self.resources,
            "limits": self.resources,
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "generateName": f"trellis-{context.role}-",
                "labels": {
                    "trellis/idea": context.idea_id,
                    "trellis/stage": context.stage,
                    "trellis/role": context.role,
                },
            },
            "spec": {
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "trellis-role",
                                "image": self.image,
                                "env": [{"name": k, "value": v} for k, v in self.env.items()],
                                "resources": resources,
                            }
                        ],
                    }
                },
                "backoffLimit": 0,
            },
        }

    async def serve(self, context: HandlerContext) -> HandlerResponse:
        if self.client is None:
            raise NotImplementedError(
                "K8sJobHandler requires a `client` adapter. "
                "Inject one via K8sJobHandler(image=..., client=my_adapter). "
                "The factory does not wire one today — this handler type is "
                "reserved for callers that have a k8s client configured."
            )

        body = self._build_job_body(context)
        job_name = await asyncio.to_thread(self.client.create_job, self.namespace, body)
        try:
            success = await asyncio.to_thread(
                self.client.wait_for_completion, self.namespace, job_name
            )
            if not success:
                return HandlerResponse(
                    state="iterate",
                    reason=f"Job {job_name} failed",
                )
            result = await asyncio.to_thread(self.client.read_result, self.namespace, job_name)
            if result is None:
                return HandlerResponse(
                    state="proceed",
                    reason=f"Job {job_name} completed (no result payload)",
                )
            return HandlerResponse(
                state=result.get("state", "proceed"),
                reason=result.get("reason", ""),
                artifacts=result.get("artifacts", {}) or {},
                cost_usd=float(result.get("cost_usd") or 0.0),
                metrics=result.get("metrics", {}) or {},
            )
        finally:
            try:
                await asyncio.to_thread(self.client.cleanup, self.namespace, job_name)
            except Exception as exc:
                logger.warning(
                    "Failed to clean up k8s job %s/%s: %s", self.namespace, job_name, exc
                )


__all__ = [
    "Handler",
    "HandlerContext",
    "HandlerResponse",
    "AgentHandler",
    "ScriptHandler",
    "WebhookHandler",
    "HumanHandler",
    "K8sJobHandler",
    "RetryPolicy",
    "State",
    "create_handler",
    "parse_duration",
    "with_retry",
]
