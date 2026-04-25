"""Playwright browser tests for the custom-stages UI surfaces.

Covers: retry button, stage progress bar, handler badges, custom stage
add/rename flow. Run with:

    uv run python -m pytest tests/test_browser_custom_stages.py --browser
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from trellis.config import Settings, _invalidate_settings_cache
from trellis.core.blackboard import Blackboard
from trellis.core.pipeline import DEFAULT_STAGE_NAME

pytestmark = [pytest.mark.browser, pytest.mark.asyncio]


SCREENSHOT_DIR = Path(os.environ.get("TRELLIS_SCREENSHOT_DIR", "/tmp/trellis-screenshots"))


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
    import uvicorn

    port = _free_port()
    _invalidate_settings_cache()

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

        import httpx

        for _ in range(50):
            try:
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
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    base_url = live_server[0]
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(base_url=base_url, viewport={"width": 1280, "height": 900})
        pg = await ctx.new_page()
        yield pg
        await browser.close()


def _seed_failed_role(bb: Blackboard, idea_id: str, role: str = "implementation") -> None:
    """Force a role into state=failed so the retry affordance shows up."""
    status = bb.get_status(idea_id)
    role_state = dict(status.get("role_state") or {})
    stage_state = dict(role_state.get(DEFAULT_STAGE_NAME) or {})
    stage_state[role] = {"state": "failed", "iterations": 5}
    role_state[DEFAULT_STAGE_NAME] = stage_state
    bb.update_status(idea_id, role_state=role_state)


# ── Retry button ──────────────────────────────────────────────────────


async def test_retry_button_appears_for_failed_role(page, live_server):
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Retry Test", "An idea with a failed role")
    _seed_failed_role(bb, idea_id, "implementation")

    await page.goto(f"/ideas/{idea_id}")
    await page.wait_for_selector("h1")

    # Failed-role panel should be visible
    panel_text = await page.text_content("body")
    assert "Failed roles" in panel_text, "Failed-role panel should render"
    assert "implementation" in panel_text

    # Retry button present
    btn = await page.query_selector("form[data-retry-form] button[type=submit]")
    assert btn, "Retry button should exist"
    text = await btn.text_content()
    assert "Retry" in text

    await page.screenshot(path=str(SCREENSHOT_DIR / "retry-button.png"), full_page=True)


async def test_retry_button_clears_failed_state(page, live_server):
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Retry Click Test", "desc")
    _seed_failed_role(bb, idea_id, "validation")

    await page.goto(f"/ideas/{idea_id}")
    await page.wait_for_selector("form[data-retry-form]")

    # Click the retry button — JS fetches, reloads on success
    async with page.expect_navigation():
        await page.click("form[data-retry-form] button[type=submit]")

    # After reload, role is back to pending (no longer in the failed panel)
    body = await page.text_content("body")
    assert "Failed roles" not in body, f"Failed panel should be gone, got: {body[:500]}"

    # Verify disk state
    status = bb.get_status(idea_id)
    assert status["role_state"][DEFAULT_STAGE_NAME]["validation"]["state"] == "pending"


# ── Stage badge + progress bar ────────────────────────────────────────


async def test_stage_badge_shown_for_custom_stage(page, live_server):
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Stage Badge Test", "desc")

    # Override pipeline with a multi-stage setup
    pipeline = {
        "name": "app-idea",
        "stages": [
            {
                "name": "prototype",
                "role_groups": [[{"name": "ideation", "handler": "agent"}]],
                "watchers": [],
            },
            {
                "name": "v1",
                "role_groups": [[{"name": "implementation", "handler": "agent"}]],
                "watchers": [],
            },
        ],
    }
    bb.set_pipeline(idea_id, pipeline)
    bb.update_status(
        idea_id,
        current_stage="prototype",
        role_state={"prototype": {}},
        stage_history=[{"stage": "prototype", "entered_at": "x", "exited_at": None}],
    )

    await page.goto(f"/ideas/{idea_id}")
    await page.wait_for_selector("h1")

    body = await page.text_content("body")
    assert "stage: prototype" in body, "Custom stage badge should render in the header"
    assert "Progress" in body, "Progress section should render"

    await page.screenshot(path=str(SCREENSHOT_DIR / "stage-progress-bar.png"), full_page=True)


async def test_progress_bar_highlights_current_stage(page, live_server):
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Progress Test", "desc")
    pipeline = {
        "stages": [
            {"name": "prototype", "role_groups": [[{"name": "ideation", "handler": "agent"}]]},
            {"name": "v1", "role_groups": [[{"name": "implementation", "handler": "agent"}]]},
            {"name": "v2", "role_groups": [[{"name": "validation", "handler": "agent"}]]},
        ],
    }
    bb.set_pipeline(idea_id, pipeline)
    bb.update_status(
        idea_id,
        current_stage="v1",
        role_state={
            "prototype": {"ideation": {"state": "proceed"}},
            "v1": {},
        },
        stage_history=[
            {"stage": "prototype", "entered_at": "x", "exited_at": "y"},
            {"stage": "v1", "entered_at": "y", "exited_at": None},
        ],
    )

    await page.goto(f"/ideas/{idea_id}")
    await page.wait_for_selector("h1")

    body = await page.text_content("body")
    assert "prototype" in body and "v1" in body and "v2" in body
    # Counter shows "stage 2/3"
    assert "stage 2/3" in body


# ── Handler badges in pipeline editor ─────────────────────────────────


async def test_handler_badges_appear_in_pipeline_editor(page, live_server):
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Badge Test", "desc")

    pipeline = {
        "stages": [
            {
                "name": "default",
                "role_groups": [
                    [{"name": "ideation", "handler": "agent"}],
                    [{"name": "deploy-webhook", "handler": "webhook"}],
                    [{"name": "deploy-script", "handler": "script"}],
                ],
                "watchers": [],
            }
        ],
    }
    bb.set_pipeline(idea_id, pipeline)

    await page.goto(f"/ideas/{idea_id}")
    await page.wait_for_selector("h1")
    # Expand the pipeline editor
    await page.click("#pipeline-editor summary")

    body = await page.content()
    # Each handler type renders as its own badge with the type name
    assert "webhook" in body
    assert "script" in body

    await page.screenshot(path=str(SCREENSHOT_DIR / "handler-badges.png"), full_page=True)


# ── Custom stage add + rename ─────────────────────────────────────────


async def test_add_stage_endpoint_appends_and_ui_renders(page, live_server):
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Add Stage Test", "desc")

    # POST directly to the endpoint (the UI trigger uses a confirm dialog)
    import httpx

    async with httpx.AsyncClient(base_url=base_url) as c:
        r = await c.post(
            f"/ideas/{idea_id}/stages",
            json={"name": "hotfix", "role_groups": []},
        )
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "hotfix"

    # Re-read pipeline and verify
    pipeline = bb.get_pipeline(idea_id)
    names = [s["name"] for s in pipeline["stages"]]
    assert "hotfix" in names

    # UI renders it
    await page.goto(f"/ideas/{idea_id}")
    await page.wait_for_selector("h1")
    body = await page.text_content("body")
    assert "hotfix" in body

    await page.screenshot(path=str(SCREENSHOT_DIR / "custom-stages.png"), full_page=True)


async def test_add_stage_rejects_invalid_name(live_server):
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Invalid Name Test", "desc")

    import httpx

    async with httpx.AsyncClient(base_url=base_url) as c:
        r = await c.post(f"/ideas/{idea_id}/stages", json={"name": "BAD NAME!"})
    assert r.status_code == 400

    async with httpx.AsyncClient(base_url=base_url) as c:
        r = await c.post(f"/ideas/{idea_id}/stages", json={"name": "done"})
    assert r.status_code == 400


async def test_add_stage_rejects_duplicate(live_server):
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Dup Test", "desc")

    import httpx

    async with httpx.AsyncClient(base_url=base_url) as c:
        r1 = await c.post(f"/ideas/{idea_id}/stages", json={"name": "v1"})
        assert r1.status_code == 200
        r2 = await c.post(f"/ideas/{idea_id}/stages", json={"name": "v1"})
        assert r2.status_code == 409


async def test_rename_stage_updates_current_stage(live_server):
    base_url, settings = live_server
    bb = Blackboard(settings.blackboard_dir)
    idea_id = bb.create_idea("Rename Test", "desc")

    # current_stage starts at "default"; rename it
    import httpx

    async with httpx.AsyncClient(base_url=base_url) as c:
        r = await c.post(f"/ideas/{idea_id}/stages/default/rename", json={"name": "prototype"})
    assert r.status_code == 200
    status = bb.get_status(idea_id)
    assert status["current_stage"] == "prototype"
    assert any(s["name"] == "prototype" for s in status["pipeline"]["stages"])
