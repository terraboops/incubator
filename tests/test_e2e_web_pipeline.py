"""End-to-end test: idea submission via web → pipeline execution → released.

Tests the full user flow:
  1. POST /ideas/ to create an idea (web form)
  2. Idea appears in GET /api/ideas
  3. Pool (with FakeClaudeSDKClient) drives the idea through default pipeline
  4. Idea reaches "released" phase with artifacts on the blackboard
  5. GET /ideas/{idea_id} renders the detail page with artifact content

This test uses the ASGI transport (no real HTTP server needed) and the
SDK digital twin (no real Claude API calls).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from trellis.config import Settings, _invalidate_settings_cache
from trellis.core.blackboard import Blackboard
from trellis.orchestrator.pool import PoolManager

from tests.sdk_twin import FakeClaudeSDKClient, patch_sdk_with_twin

pytestmark = pytest.mark.e2e


# ── Helpers ────────────────────────────────────────────────────────────


def _settings_for_project(project: Path) -> Settings:
    return Settings(
        project_root=project,
        blackboard_dir=project / "blackboard" / "ideas",
        workspace_dir=project / "workspace",
        registry_path=project / "registry.yaml",
        pool_size=1,
        job_timeout_minutes=2,
        producer_interval_seconds=0,
        max_refinement_cycles=1,
        min_quality_score=0.0,
    )


async def _drive_pool_to_completion(settings: Settings, timeout: float = 30.0) -> None:
    """Run the pool until all ideas reach a terminal phase."""
    pool = PoolManager(settings)
    pool._running = True
    if not pool._acquire_pool_lock():
        pool._release_pool_lock()
        pool._acquire_pool_lock()

    try:
        task = asyncio.create_task(pool._run_loop())
        bb = pool.blackboard

        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(0.2)
            ideas = bb.list_ideas()
            if ideas and all(
                bb.get_status(iid).get("phase") in ("released", "killed", "paused") for iid in ideas
            ):
                break

        pool.stop()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        pool._release_pool_lock()


# ── Test ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idea_creation_through_pipeline(trellis_project):
    """Submit an idea via web form → pool drives it to 'released' → artifacts visible in UI."""
    settings = _settings_for_project(trellis_project)
    bb = Blackboard(settings.blackboard_dir)

    # Write a default pipeline preset the form handler will pick up
    pool_dir = trellis_project / "pool"
    pool_dir.mkdir(exist_ok=True)
    preset = {
        "full-pipeline": {
            "label": "Full Pipeline",
            "description": "All stages",
            "stages": ["ideation", "implementation", "validation", "release"],
            "post_ready": [],
            "gating": {"default": "auto", "overrides": {}},
        }
    }
    (pool_dir / "presets.json").write_text(json.dumps(preset))

    with (
        patch("trellis.config.get_settings", return_value=settings),
        patch("trellis.config._discover_project_root", return_value=trellis_project),
        patch("trellis.web.api.routes.ideas.get_settings", return_value=settings),
        patch("trellis.web.api.routes.settings.get_settings", return_value=settings),
        patch("trellis.web.api.routes.health.get_settings", return_value=settings),
    ):
        _invalidate_settings_cache()
        from trellis.web.api.app import create_app

        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # ── Step 1: Submit idea via web form ──
            resp = await client.post(
                "/ideas",
                data={
                    "title": "Test Startup Idea",
                    "description": "A compelling idea for an end-to-end test.",
                    "preset": "full-pipeline",
                },
                follow_redirects=False,
            )
            # Should redirect to /ideas/{idea_id}
            assert resp.status_code in (302, 303, 307), f"Expected redirect, got {resp.status_code}"
            idea_url = resp.headers["location"]
            idea_id = idea_url.split("/ideas/")[-1].rstrip("/")
            assert idea_id, "Should have created an idea with an ID"

            # ── Step 2: Idea is visible in API ──
            api_resp = await client.get("/api/ideas")
            assert api_resp.status_code == 200
            ideas = api_resp.json()
            idea_ids = [i["id"] for i in ideas]
            assert idea_id in idea_ids, f"Idea {idea_id!r} not found in {idea_ids}"

            # ── Step 3: Idea starts in 'submitted' phase ──
            status = bb.get_status(idea_id)
            assert status["phase"] == "submitted", f"Expected 'submitted', got {status['phase']}"

        # ── Step 4: Run pool to drive idea through pipeline ──
        FakeClaudeSDKClient.reset()
        with patch_sdk_with_twin():
            await _drive_pool_to_completion(settings, timeout=30.0)

        # ── Step 5: Idea reached 'released' ──
        status = bb.get_status(idea_id)
        assert status["phase"] == "released", (
            f"Expected 'released', got '{status['phase']}'. "
            f"History: {json.dumps(status.get('phase_history', []), indent=2)}"
        )

        # ── Step 6: All pipeline agents were invoked ──
        roles = [inv["role"] for inv in FakeClaudeSDKClient.invocations]
        for expected in ("ideation", "implementation", "validation", "release"):
            assert expected in roles, f"Agent '{expected}' was not invoked. Ran: {roles}"

        # ── Step 7: Idea directory has at least the core files ──
        idea_dir = bb.idea_dir(idea_id)
        assert (idea_dir / "idea.md").exists(), "idea.md should exist on the blackboard"
        assert (idea_dir / "status.json").exists(), "status.json should exist on the blackboard"

        # ── Step 8: Idea detail page renders (200 OK) with idea title ──
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            page_resp = await client.get(f"/ideas/{idea_id}")
            assert page_resp.status_code == 200
            body = page_resp.text
            assert "Test Startup Idea" in body or idea_id in body, (
                "Idea detail page should show the idea title or ID"
            )

        # ── Step 9: Health endpoints pass ──
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            healthz = await client.get("/healthz")
            assert healthz.status_code == 200
            assert healthz.json()["status"] == "ok"

            readyz = await client.get("/readyz")
            assert readyz.status_code == 200  # blackboard is accessible

    _invalidate_settings_cache()


@pytest.mark.asyncio
async def test_kill_then_delete_keeps_home_alive(trellis_project):
    """Killing then deleting an idea must not 500 the homepage.

    Regression for two distinct bugs that combined into one symptom:
    - The home loop's _compute_scheduling called bb.pending_post_ready,
      which read status.json and raised FileNotFoundError when the idea
      had been deleted.
    - The action handler created a Blackboard without a projection
      reference, so bb.delete_idea couldn't invalidate the projection
      cache. The next home() pulled the stale entry and tried to compute
      scheduling on an idea whose disk state was gone.
    """
    settings = _settings_for_project(trellis_project)

    pool_dir = trellis_project / "pool"
    pool_dir.mkdir(exist_ok=True)
    (pool_dir / "presets.json").write_text(
        json.dumps(
            {
                "minimal": {
                    "label": "Minimal",
                    "description": "Just ideation",
                    "stages": ["ideation"],
                    "post_ready": [],
                    "gating": {"default": "auto", "overrides": {}},
                }
            }
        )
    )

    with (
        patch("trellis.config.get_settings", return_value=settings),
        patch("trellis.config._discover_project_root", return_value=trellis_project),
        patch("trellis.web.api.routes.ideas.get_settings", return_value=settings),
        patch("trellis.web.api.routes.settings.get_settings", return_value=settings),
        patch("trellis.web.api.routes.health.get_settings", return_value=settings),
    ):
        _invalidate_settings_cache()
        from trellis.web.api.app import create_app

        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Drive the lifespan so app.state.projection is populated
            await client.get("/healthz")

            resp = await client.post(
                "/ideas",
                data={
                    "title": "Doomed Idea",
                    "description": "About to be killed and deleted.",
                    "preset": "minimal",
                },
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303, 307)
            idea_id = resp.headers["location"].split("/ideas/")[-1].rstrip("/")

            home1 = await client.get("/")
            assert home1.status_code == 200

            kill = await client.post(
                f"/ideas/{idea_id}/action",
                data={"action": "kill", "kill_reason": "regression test"},
                follow_redirects=False,
            )
            assert kill.status_code in (302, 303, 307)

            delete = await client.post(
                f"/ideas/{idea_id}/action",
                data={"action": "delete"},
                follow_redirects=False,
            )
            assert delete.status_code in (302, 303, 307)

            # The on-disk idea directory is gone
            assert not (settings.blackboard_dir / idea_id).exists()

            # Home must still render. Hit it twice to defeat any one-shot
            # cache-warm masking the bug.
            home2 = await client.get("/")
            assert home2.status_code == 200, (
                f"Home 500'd after kill+delete. Body: {home2.text[:500]}"
            )
            home3 = await client.get("/")
            assert home3.status_code == 200

    _invalidate_settings_cache()


@pytest.mark.asyncio
async def test_all_surfaces_render_without_500(trellis_project):
    """Smoke-sweep every public surface to lock in non-500 status codes.

    Catches regressions where a route handler crashes due to a stale
    template variable, missing import, broken projection lookup, etc.
    Doesn't validate content semantics — just that nothing 500s.
    """
    settings = _settings_for_project(trellis_project)

    pool_dir = trellis_project / "pool"
    pool_dir.mkdir(exist_ok=True)
    (pool_dir / "presets.json").write_text(
        json.dumps(
            {
                "minimal": {
                    "label": "Minimal",
                    "description": "Just ideation",
                    "stages": ["ideation"],
                    "post_ready": [],
                    "gating": {"default": "auto", "overrides": {}},
                }
            }
        )
    )

    with (
        patch("trellis.config.get_settings", return_value=settings),
        patch("trellis.config._discover_project_root", return_value=trellis_project),
        patch("trellis.web.api.routes.ideas.get_settings", return_value=settings),
        patch("trellis.web.api.routes.settings.get_settings", return_value=settings),
        patch("trellis.web.api.routes.health.get_settings", return_value=settings),
    ):
        _invalidate_settings_cache()
        from trellis.web.api.app import create_app

        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Force lifespan
            await client.get("/healthz")

            # Seed one idea so detail/logs/agent pages have something to render
            resp = await client.post(
                "/ideas",
                data={
                    "title": "Surface Sweep Idea",
                    "description": "Used by the all-surfaces smoke.",
                    "preset": "minimal",
                },
                follow_redirects=False,
            )
            idea_id = resp.headers["location"].split("/ideas/")[-1].rstrip("/")

            # Every URL that should always render (200 OK after redirects).
            # Ops, listings, forms, JSON APIs, idea + agent detail pages.
            urls = [
                # Ops
                "/healthz",
                "/readyz",
                "/metrics",
                # Listings
                "/",
                "/agents/",
                "/pipelines/",
                "/activity",
                "/pool/",
                "/evolution/",
                "/costs/",
                "/settings/",
                # Forms
                "/ideas/new",
                "/agents/new",
                "/agents/wizard",
                "/pipelines/new",
                # JSON APIs
                "/api/ideas",
                "/api/decisions/pending",
                "/api/migrations/check",
                "/pool/api/state",
                "/api/activity",
                # Idea-scoped
                f"/ideas/{idea_id}",
                f"/ideas/{idea_id}/logs",
                # Agent detail (registry roles)
                "/agents/ideation",
                "/agents/ideation/plugins",
            ]

            failures = []
            for u in urls:
                r = await client.get(u, follow_redirects=True)
                if r.status_code >= 500:
                    failures.append(f"{u} -> {r.status_code}: {r.text[:200]}")
                # 404 is also a regression for routes the plan says should exist
                elif r.status_code == 404 and not u.endswith("/migrations/check"):
                    failures.append(f"{u} -> 404 (route should exist)")

            assert not failures, "Surface sweep regressions:\n  " + "\n  ".join(failures)

    _invalidate_settings_cache()
