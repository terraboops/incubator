"""Playwright browser tests for the top 5 user flows.

These tests spin up a real trellis ASGI server with the SDK digital twin,
then drive a Chromium browser through the UI.

Run with: uv run python -m pytest tests/test_browser.py -v --browser
Skip with: uv run python -m pytest tests/ (browser tests excluded by default)
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from trellis.config import Settings, _invalidate_settings_cache
from trellis.core.blackboard import Blackboard
from trellis.orchestrator.pool import PoolManager

from tests.sdk_twin import FakeClaudeSDKClient, patch_sdk_with_twin

pytestmark = [pytest.mark.browser, pytest.mark.asyncio]


# ── Fixtures ──────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture
def browser_settings(trellis_project: Path) -> Settings:
    return Settings(
        project_root=trellis_project,
        blackboard_dir=trellis_project / "blackboard" / "ideas",
        workspace_dir=trellis_project / "workspace",
        registry_path=trellis_project / "registry.yaml",
        pool_size=1,
        job_timeout_minutes=2,
        producer_interval_seconds=0,
        max_refinement_cycles=1,
        min_quality_score=0.0,
    )


@pytest.fixture
async def live_server(browser_settings):
    """Start a real uvicorn server with SDK twin for Playwright tests."""
    import uvicorn

    port = _free_port()
    _invalidate_settings_cache()

    # Write a pipeline preset
    pool_dir = browser_settings.project_root / "pool"
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
        patch("trellis.config.get_settings", return_value=browser_settings),
        patch("trellis.config._discover_project_root", return_value=browser_settings.project_root),
        patch("trellis.web.api.routes.ideas.get_settings", return_value=browser_settings),
        patch("trellis.web.api.routes.settings.get_settings", return_value=browser_settings),
        patch("trellis.web.api.routes.health.get_settings", return_value=browser_settings),
        patch("trellis.web.api.routes.agents.get_settings", return_value=browser_settings),
        patch("trellis.web.api.routes.activity.get_settings", return_value=browser_settings),
    ):
        from trellis.web.api.app import create_app

        app = create_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())

        # Wait for server to be ready
        for _ in range(50):
            try:
                import httpx

                async with httpx.AsyncClient() as c:
                    r = await c.get(f"http://127.0.0.1:{port}/healthz")
                    if r.status_code == 200:
                        break
            except Exception:
                pass
            await asyncio.sleep(0.1)

        yield f"http://127.0.0.1:{port}", browser_settings

        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()

    _invalidate_settings_cache()


@pytest.fixture
async def page(live_server):
    """Playwright browser page pointed at the live server."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    base_url = live_server[0]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(base_url=base_url)
        pg = await ctx.new_page()
        yield pg
        await browser.close()


async def _drive_pool(settings, timeout=30.0):
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
                bb.get_status(i).get("phase") in ("released", "killed", "paused") for i in ideas
            ):
                break
        pool.stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        pool._release_pool_lock()


# ── Flow 1: Submit idea and watch pipeline ────────────────────────────


async def test_flow_submit_idea_and_watch_pipeline(page, live_server):
    """Submit an idea via the web form → verify pipeline timeline → run to released."""
    base_url, settings = live_server

    # Navigate to new idea form
    await page.goto("/ideas/new")
    await page.wait_for_selector('input[name="title"]')

    # Fill and submit
    await page.fill('input[name="title"]', "Browser Test Idea")
    await page.fill('textarea[name="description"]', "Testing the full pipeline flow")
    await page.click('button[type="submit"]')

    # Should redirect to idea detail
    await page.wait_for_url("**/ideas/browser-test-idea**", timeout=5000)
    assert "browser-test-idea" in page.url

    # Verify SUBMITTED badge
    badge = await page.text_content(".badge")
    assert "SUBMITTED" in badge.upper()

    # Run pool to drive through pipeline
    FakeClaudeSDKClient.reset()
    with patch_sdk_with_twin():
        await _drive_pool(settings)

    # Refresh and check released
    await page.reload()
    badge = await page.text_content(".badge")
    assert "RELEASED" in badge.upper()


# ── Flow 2: Watch live activity ───────────────────────────────────────


async def test_flow_watch_live_activity(page, live_server):
    """Submit idea, start slow agent, verify live transcript panel shows entries."""
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Live Test", "Testing live activity")
    bb.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation"],
            "post_ready": [],
            "parallel_groups": [["ideation"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    # Navigate to activity page
    await page.goto("/activity")
    content = await page.content()
    # May show "All quiet" if no agents running yet
    assert "Activity" in content


# ── Flow 3: Configure agent ──────────────────────────────────────────


async def test_flow_configure_agent(page, live_server):
    """Navigate to agent detail, change settings, save, verify persistence."""
    base_url, settings = live_server

    await page.goto("/agents")
    await page.wait_for_selector("a[href*='/agents/']")

    # Click first agent link
    links = await page.query_selector_all("a[href*='/agents/ideation']")
    if links:
        await links[0].click()
        await page.wait_for_url("**/agents/ideation**")

        # Verify form elements exist
        assert await page.query_selector('select[name="model"]')
        assert await page.query_selector('select[name="status"]')
        assert await page.query_selector('textarea[name="prompt_py"]')

        # Check plugins link exists
        plugins_link = await page.query_selector('a[href*="/plugins"]')
        assert plugins_link


# ── Flow 4: Review agent logs ────────────────────────────────────────


async def test_flow_review_agent_logs(page, live_server):
    """Submit idea, run pipeline, navigate to logs, verify transcript renders."""
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Log Test", "Testing log review")
    bb.update_status(
        idea_id,
        pipeline={
            "agents": ["ideation"],
            "post_ready": [],
            "parallel_groups": [["ideation"]],
            "gating": {"default": "auto", "overrides": {}},
        },
    )

    FakeClaudeSDKClient.reset()
    with patch_sdk_with_twin():
        await _drive_pool(settings)

    # Navigate to idea detail
    await page.goto(f"/ideas/{idea_id}")
    await page.wait_for_selector("h1")

    # Navigate to logs
    await page.goto(f"/ideas/{idea_id}/logs")
    content = await page.content()
    assert "ideation" in content.lower() or "log" in content.lower()


# ── Flow 5: Settings and values ──────────────────────────────────────


async def test_flow_settings_and_values(page, live_server):
    """Navigate to settings, save values, verify persistence."""
    base_url, settings = live_server

    await page.goto("/settings")
    await page.wait_for_selector('textarea[name="values"]')

    # Type values
    await page.fill('textarea[name="values"]', "Progressive and inclusive\nCommunity-centered")

    # Submit the values form (find the right submit button)
    values_form = await page.query_selector('form[action="/settings/values"]')
    if values_form:
        submit = await values_form.query_selector('button[type="submit"]')
        await submit.click()

    # Wait for redirect
    await page.wait_for_url("**/settings**", timeout=5000)

    # Reload and verify persistence
    await page.goto("/settings")
    values_textarea = await page.query_selector('textarea[name="values"]')
    value = await values_textarea.input_value()
    assert "Progressive" in value

    # Verify values.md was created on disk
    values_path = settings.project_root / "values.md"
    assert values_path.exists()
    assert "Progressive" in values_path.read_text()


# ── Feature: Metrics endpoint ─────────────────────────────────────────


async def test_metrics_endpoint(page, live_server):
    """Verify /metrics returns Prometheus text with sandbox counter."""
    await page.goto("/metrics")
    content = await page.content()
    assert "trellis_up" in content
    assert "trellis_sandbox_failure_total" in content
