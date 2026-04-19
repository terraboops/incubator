"""Tests for ScriptHandler + WebhookHandler (JSON-in/JSON-out handler contract)."""

from __future__ import annotations

import httpx
import pytest

from trellis.core.handlers import (
    HandlerContext,
    ScriptHandler,
    WebhookHandler,
    create_handler,
)


def _ctx(workspace_dir: str = "/tmp") -> HandlerContext:
    return HandlerContext(
        idea_id="i",
        stage="s",
        role="r",
        blackboard_dir="/tmp/bb",
        workspace_dir=workspace_dir,
        inputs={"version": "v1"},
    )


# ─── ScriptHandler ────────────────────────────────────────────────────────


class TestScriptHandlerConfig:
    def test_string_command_is_shlex_split(self):
        h = ScriptHandler.from_role_config({"command": "echo hello world"})
        assert h.command == ["echo", "hello", "world"]

    def test_list_command_is_preserved(self):
        h = ScriptHandler.from_role_config({"command": ["python", "-c", "print(1)"]})
        assert h.command == ["python", "-c", "print(1)"]

    def test_missing_command_raises(self):
        with pytest.raises(ValueError, match="command"):
            ScriptHandler.from_role_config({})

    def test_empty_command_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            ScriptHandler([])


class TestScriptHandlerExecution:
    async def test_json_out_is_parsed(self, tmp_path):
        script = tmp_path / "hello.sh"
        script.write_text(
            '#!/bin/sh\ncat >/dev/null\necho \'{"state": "proceed", "reason": "ok", "cost_usd": 0.5, "metrics": {"duration_ms": 42}}\'\n'
        )
        script.chmod(0o755)
        handler = ScriptHandler([str(script)])

        resp = await handler.serve(_ctx(workspace_dir=str(tmp_path)))

        assert resp.state == "proceed"
        assert resp.reason == "ok"
        assert resp.cost_usd == 0.5
        assert resp.metrics == {"duration_ms": 42}

    async def test_stdin_is_passed(self, tmp_path):
        # Script echoes the idea_id from stdin, proves stdin wiring works.
        script = tmp_path / "echo-idea.sh"
        script.write_text(
            "#!/bin/sh\n"
            "INPUT=$(cat)\n"
            'printf \'{"state": "proceed", "reason": "got %s"}\' "$INPUT"\n'
        )
        script.chmod(0o755)
        handler = ScriptHandler([str(script)])

        resp = await handler.serve(_ctx(workspace_dir=str(tmp_path)))

        assert '"idea_id": "i"' in resp.reason

    async def test_legacy_exit_code_zero_is_proceed(self, tmp_path):
        script = tmp_path / "ok.sh"
        script.write_text("#!/bin/sh\ncat >/dev/null\necho plain text\nexit 0\n")
        script.chmod(0o755)
        handler = ScriptHandler([str(script)])

        resp = await handler.serve(_ctx(workspace_dir=str(tmp_path)))

        assert resp.state == "proceed"
        assert "plain text" in resp.reason

    async def test_legacy_exit_code_nonzero_is_iterate_with_stderr(self, tmp_path):
        script = tmp_path / "fail.sh"
        script.write_text("#!/bin/sh\ncat >/dev/null\necho 'something went wrong' 1>&2\nexit 3\n")
        script.chmod(0o755)
        handler = ScriptHandler([str(script)])

        resp = await handler.serve(_ctx(workspace_dir=str(tmp_path)))

        assert resp.state == "iterate"
        assert "something went wrong" in resp.reason

    async def test_cwd_defaults_to_workspace_dir(self, tmp_path):
        script = tmp_path / "pwd.sh"
        script.write_text("#!/bin/sh\ncat >/dev/null\npwd\n")
        script.chmod(0o755)
        handler = ScriptHandler([str(script)])

        resp = await handler.serve(_ctx(workspace_dir=str(tmp_path)))

        assert str(tmp_path) in resp.reason


# ─── WebhookHandler ───────────────────────────────────────────────────────


class TestWebhookHandlerConfig:
    def test_missing_url_raises(self):
        with pytest.raises(ValueError, match="url"):
            WebhookHandler.from_role_config({"handler": "webhook"})

    def test_method_defaults_to_post(self):
        h = WebhookHandler.from_role_config({"url": "http://x"})
        assert h.method == "POST"

    def test_method_is_normalized_upper(self):
        h = WebhookHandler.from_role_config({"url": "http://x", "method": "put"})
        assert h.method == "PUT"


class TestWebhookHandlerExecution:
    async def test_parses_state_from_json_response(self):
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"state": "proceed", "reason": "deployed", "cost_usd": 0.0}
            )

        transport = httpx.MockTransport(_handler)

        handler = WebhookHandler("https://hooks.example/deploy")
        # Patch the client creation to use the mock transport
        async with httpx.AsyncClient(transport=transport, timeout=None) as client:
            # Inline override — ScriptHandler/WebhookHandler build their own client;
            # for this test we assert the parsing logic via direct call.
            resp = await client.post("https://hooks.example/deploy", json=_ctx().to_json())
            assert resp.status_code == 200

        # Now test the real handler.serve path with a mock transport
        import unittest.mock

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def request(self, method, url, **kw):
                return httpx.Response(200, json={"state": "proceed", "reason": "ok"})

        with unittest.mock.patch("httpx.AsyncClient", _FakeClient):
            resp = await handler.serve(_ctx())
            assert resp.state == "proceed"
            assert resp.reason == "ok"

    async def test_5xx_is_iterate(self):
        import unittest.mock

        class _ErrClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def request(self, *a, **kw):
                return httpx.Response(503, text="service down")

        with unittest.mock.patch("httpx.AsyncClient", _ErrClient):
            resp = await WebhookHandler("https://x").serve(_ctx())
            assert resp.state == "iterate"
            assert "503" in resp.reason

    async def test_4xx_is_failed(self):
        import unittest.mock

        class _ClientErr:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def request(self, *a, **kw):
                return httpx.Response(404, text="not found")

        with unittest.mock.patch("httpx.AsyncClient", _ClientErr):
            resp = await WebhookHandler("https://x").serve(_ctx())
            assert resp.state == "failed"
            assert "404" in resp.reason

    async def test_2xx_without_state_field_is_proceed(self):
        import unittest.mock

        class _OkClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def request(self, *a, **kw):
                return httpx.Response(200, text="ok")

        with unittest.mock.patch("httpx.AsyncClient", _OkClient):
            resp = await WebhookHandler("https://x").serve(_ctx())
            assert resp.state == "proceed"


# ─── Factory integration ──────────────────────────────────────────────────


class TestFactoryDispatch:
    def test_script_type_returns_script_handler(self):
        h = create_handler({"handler": "script", "command": "echo hi"})
        assert isinstance(h, ScriptHandler)

    def test_webhook_type_returns_webhook_handler(self):
        h = create_handler({"handler": "webhook", "url": "https://x"})
        assert isinstance(h, WebhookHandler)

    def test_retry_wraps_script_handler(self):
        h = create_handler(
            {
                "handler": "script",
                "command": "echo hi",
                "retry": {"max_attempts": 3},
            }
        )
        # Wrapped by _RetryingHandler
        assert not isinstance(h, ScriptHandler)
        assert isinstance(h.inner, ScriptHandler)

    def test_unknown_handler_type_raises(self):
        with pytest.raises(NotImplementedError, match="bogus"):
            create_handler({"handler": "bogus"})
