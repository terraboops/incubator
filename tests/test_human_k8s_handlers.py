"""Tests for HumanHandler + K8sJobHandler (deferrable handler types)."""

from __future__ import annotations

import json

import pytest

from trellis.core.handlers import (
    HandlerContext,
    HumanHandler,
    K8sJobHandler,
    create_handler,
)


def _ctx(blackboard_dir: str) -> HandlerContext:
    return HandlerContext(
        idea_id="idea-1",
        stage="validation",
        role="human-approval",
        blackboard_dir=blackboard_dir,
        workspace_dir="/tmp",
        inputs={"question": "Ship it?"},
    )


class TestHumanHandler:
    async def test_writes_request_file_and_returns_needs_review(self, tmp_path):
        handler = HumanHandler()
        ctx = _ctx(str(tmp_path))

        resp = await handler.serve(ctx)

        assert resp.state == "needs_review"
        request_path = tmp_path / "human-requests" / "human-approval" / "request.json"
        assert request_path.exists()
        written = json.loads(request_path.read_text())
        assert written["idea_id"] == "idea-1"
        assert written["role"] == "human-approval"
        assert written["inputs"]["question"] == "Ship it?"
        assert resp.metrics["request_file"] == str(request_path)

    async def test_second_call_does_not_overwrite_request(self, tmp_path):
        handler = HumanHandler()
        ctx = _ctx(str(tmp_path))

        await handler.serve(ctx)
        # Human edits the request file mid-flight
        req = tmp_path / "human-requests" / "human-approval" / "request.json"
        req.write_text('{"modified": true}')

        await handler.serve(ctx)  # second re-emission

        assert json.loads(req.read_text()) == {"modified": True}

    async def test_custom_request_file_name(self, tmp_path):
        handler = HumanHandler(request_file_name="pending.json")
        await handler.serve(_ctx(str(tmp_path)))
        assert (tmp_path / "human-requests" / "human-approval" / "pending.json").exists()

    def test_factory_builds_human_handler(self):
        h = create_handler({"handler": "human"})
        assert isinstance(h, HumanHandler)


class _FakeK8sClient:
    def __init__(self, *, result: dict | None = None, success: bool = True) -> None:
        self.result = result
        self.success = success
        self.created: list[tuple[str, dict]] = []
        self.cleaned: list[tuple[str, str]] = []

    def create_job(self, namespace: str, body: dict) -> str:
        self.created.append((namespace, body))
        return "trellis-role-abc123"

    def wait_for_completion(self, namespace: str, name: str) -> bool:
        return self.success

    def read_result(self, namespace: str, name: str) -> dict | None:
        return self.result

    def cleanup(self, namespace: str, name: str) -> None:
        self.cleaned.append((namespace, name))


class TestK8sJobHandler:
    async def test_requires_image(self):
        with pytest.raises(ValueError, match="image"):
            K8sJobHandler(image="")

    async def test_factory_builds_k8s_job_handler(self):
        h = create_handler(
            {
                "handler": "k8s_job",
                "image": "ghcr.io/example/uptime:latest",
                "namespace": "trellis",
            }
        )
        assert isinstance(h, K8sJobHandler)
        assert h.image == "ghcr.io/example/uptime:latest"
        assert h.namespace == "trellis"

    async def test_serve_without_client_raises_informative_error(self, tmp_path):
        handler = K8sJobHandler(image="example:1")
        with pytest.raises(NotImplementedError, match="client"):
            await handler.serve(_ctx(str(tmp_path)))

    async def test_serve_creates_job_and_parses_result(self, tmp_path):
        client = _FakeK8sClient(
            result={"state": "proceed", "reason": "healthy", "cost_usd": 0.1},
        )
        handler = K8sJobHandler(image="example:1", client=client, namespace="trellis")

        resp = await handler.serve(_ctx(str(tmp_path)))

        assert resp.state == "proceed"
        assert resp.reason == "healthy"
        assert resp.cost_usd == 0.1
        assert len(client.created) == 1
        ns, body = client.created[0]
        assert ns == "trellis"
        assert body["metadata"]["labels"]["trellis/idea"] == "idea-1"
        assert body["metadata"]["labels"]["trellis/role"] == "human-approval"
        assert client.cleaned == [("trellis", "trellis-role-abc123")]

    async def test_failed_job_returns_iterate(self, tmp_path):
        client = _FakeK8sClient(success=False)
        handler = K8sJobHandler(image="example:1", client=client)

        resp = await handler.serve(_ctx(str(tmp_path)))

        assert resp.state == "iterate"
        assert "failed" in resp.reason

    async def test_no_result_payload_is_proceed(self, tmp_path):
        client = _FakeK8sClient(result=None)
        handler = K8sJobHandler(image="example:1", client=client)

        resp = await handler.serve(_ctx(str(tmp_path)))

        assert resp.state == "proceed"
        assert "no result payload" in resp.reason

    async def test_cleanup_failure_does_not_mask_result(self, tmp_path):
        class _BrokenCleanup(_FakeK8sClient):
            def cleanup(self, namespace, name):
                raise RuntimeError("cleanup blew up")

        client = _BrokenCleanup(result={"state": "proceed"})
        handler = K8sJobHandler(image="example:1", client=client)

        resp = await handler.serve(_ctx(str(tmp_path)))

        assert resp.state == "proceed"  # cleanup failure doesn't mask success
