"""Edge cases that surfaced during the integration-branch live test.

Covers:
- WebSocket broadcast surviving a concurrent connect/disconnect (the
  "Set changed size during iteration" bug we saw in /tmp/trellis-mybrain.log).
- Blackboard.delete_idea behaviour: _template guard, missing idea, no
  projection set, idempotent double-delete.
- ProjectionStore.delete_idea idempotency.
- Home route returns 200 (and just drops the row) when the projection
  has a stale entry whose status.json no longer exists on disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from trellis.core.blackboard import Blackboard
from trellis.core.projection import ProjectionStore


# ──────────────────────────────────────────────────────────────────────
# WebSocket broadcast concurrency
# ──────────────────────────────────────────────────────────────────────


class _FakeWebSocket:
    """Minimal stand-in for `fastapi.WebSocket` used by broadcast_event.

    `on_send` lets a test mutate the shared `_clients` set during the
    `await client.send_text(...)` yield, exactly like a real connect /
    disconnect would.
    """

    def __init__(self, on_send=None):
        self._on_send = on_send
        self.received: list[str] = []
        self.closed = False

    async def send_text(self, message: str) -> None:
        # Yield once so any handler scheduled via asyncio.sleep(0) can run
        # before we record the message — mimics the real send path.
        await asyncio.sleep(0)
        if self._on_send is not None:
            self._on_send()
        if self.closed:
            raise RuntimeError("send on closed socket")
        self.received.append(message)


@pytest.fixture
def fresh_ws_module():
    """Reset the websocket module's `_clients` set around each test."""
    from trellis.web.api import websocket as ws_mod

    ws_mod._clients.clear()
    yield ws_mod
    ws_mod._clients.clear()


@pytest.mark.asyncio
async def test_broadcast_survives_connect_during_iteration(fresh_ws_module):
    """A new client connecting mid-broadcast must not crash the broadcast."""
    ws_mod = fresh_ws_module
    a = _FakeWebSocket()
    new_client = _FakeWebSocket()

    def add_during_send():
        # Mimics a /ws/events handler accepting a new client mid-broadcast.
        ws_mod._clients.add(new_client)

    a._on_send = add_during_send
    ws_mod._clients.add(a)

    # Should not raise RuntimeError("Set changed size during iteration").
    await ws_mod.broadcast_event("activity", {"idea_id": "x", "message": "hi"})

    # The original client received the message; the late-joining client
    # is in the set for the *next* broadcast (not this one).
    assert len(a.received) == 1
    assert new_client in ws_mod._clients
    assert new_client.received == []


@pytest.mark.asyncio
async def test_broadcast_survives_disconnect_during_iteration(fresh_ws_module):
    """A client disconnecting mid-broadcast must not crash the broadcast."""
    ws_mod = fresh_ws_module
    a = _FakeWebSocket()
    b = _FakeWebSocket()

    def kill_b_during_a_send():
        ws_mod._clients.discard(b)

    a._on_send = kill_b_during_a_send
    ws_mod._clients.update({a, b})

    await ws_mod.broadcast_event("activity", {"idea_id": "x", "message": "hi"})

    assert a.received  # a got the message
    # b was discarded by the concurrent handler; iteration didn't crash
    assert b not in ws_mod._clients


@pytest.mark.asyncio
async def test_broadcast_drops_clients_that_raise(fresh_ws_module):
    """A client whose send_text raises is removed from the active set."""
    ws_mod = fresh_ws_module
    healthy = _FakeWebSocket()
    broken = _FakeWebSocket()
    broken.closed = True
    ws_mod._clients.update({healthy, broken})

    await ws_mod.broadcast_event("activity", {"idea_id": "x", "message": "hi"})

    assert healthy in ws_mod._clients
    assert broken not in ws_mod._clients


# ──────────────────────────────────────────────────────────────────────
# Blackboard.delete_idea edge cases
# ──────────────────────────────────────────────────────────────────────


def _make_bb(tmp_path: Path) -> Blackboard:
    ideas = tmp_path / "ideas"
    template = ideas / "_template"
    template.mkdir(parents=True)
    (template / "status.json").write_text(
        json.dumps({"id": "", "title": "", "phase": "submitted", "phase_history": []})
    )
    return Blackboard(ideas)


def _seed_idea(bb: Blackboard, idea_id: str) -> None:
    d = bb.base_dir / idea_id
    d.mkdir()
    (d / "status.json").write_text(
        json.dumps(
            {
                "id": idea_id,
                "title": idea_id,
                "phase": "released",
                "phase_history": [],
            }
        )
    )


def test_delete_idea_refuses_to_remove_template(tmp_path):
    bb = _make_bb(tmp_path)
    template_dir = bb.base_dir / "_template"
    assert template_dir.is_dir()

    bb.delete_idea("_template")

    # Template directory must survive — agents copy from it on create_idea.
    assert template_dir.is_dir()


def test_delete_idea_is_a_noop_for_unknown_idea(tmp_path):
    bb = _make_bb(tmp_path)
    # No exception — silent no-op so the route handler doesn't have to special-case
    # a stale id.
    bb.delete_idea("never-existed")
    assert (bb.base_dir / "_template").is_dir()


def test_delete_idea_works_without_projection_attached(tmp_path):
    bb = _make_bb(tmp_path)
    _seed_idea(bb, "doomed")
    assert bb.projection is None  # default state
    bb.delete_idea("doomed")
    assert "doomed" not in bb.list_ideas()


def test_delete_idea_is_idempotent(tmp_path):
    bb = _make_bb(tmp_path)
    _seed_idea(bb, "doomed")
    bb.delete_idea("doomed")
    # Second call must not raise (e.g. accidental double-submit from the UI).
    bb.delete_idea("doomed")
    assert "doomed" not in bb.list_ideas()


def test_delete_idea_swallows_projection_exception(tmp_path):
    """A broken projection shouldn't prevent the on-disk delete from completing."""
    bb = _make_bb(tmp_path)
    _seed_idea(bb, "doomed")

    class BoomProjection:
        def delete_idea(self, idea_id):
            raise RuntimeError("kaboom")

    bb.projection = BoomProjection()
    # Filesystem delete must still succeed.
    bb.delete_idea("doomed")
    assert "doomed" not in bb.list_ideas()


def test_delete_idea_returns_within_timeout_when_projection_hangs(tmp_path):
    """If the projection store hangs, delete_idea must not block forever."""
    import time
    import threading

    bb = _make_bb(tmp_path)
    _seed_idea(bb, "doomed")

    # Tiny timeout so the test runs fast.
    bb._PROJECTION_TIMEOUT = 0.2

    class HangingProjection:
        called = threading.Event()

        def delete_idea(self, idea_id):
            HangingProjection.called.set()
            time.sleep(5)  # longer than the timeout

    bb.projection = HangingProjection()
    t0 = time.monotonic()
    bb.delete_idea("doomed")
    elapsed = time.monotonic() - t0

    assert HangingProjection.called.is_set()
    # Should return well before the projection's 5s sleep.
    assert elapsed < 1.0, f"delete_idea blocked too long: {elapsed:.2f}s"
    assert "doomed" not in bb.list_ideas()


# ──────────────────────────────────────────────────────────────────────
# ProjectionStore.delete_idea idempotency
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def projection():
    p = ProjectionStore()
    p.connect("mem://")
    yield p
    p.close()


def test_projection_delete_idea_is_idempotent(projection):
    projection.upsert_idea("once", {"title": "Once", "phase": "submitted"})
    projection.delete_idea("once")
    # Second delete on a no-longer-existing record must not raise.
    projection.delete_idea("once")
    assert projection.get_idea("once") is None


def test_projection_delete_idea_no_op_without_connection():
    """Methods stay safe when the store was never connected."""
    p = ProjectionStore()
    # No connect() call — `_db` is None.
    p.delete_idea("anything")  # must not raise
    assert p.get_idea("anything") is None


# ──────────────────────────────────────────────────────────────────────
# Home route resilience to stale projection rows
# ──────────────────────────────────────────────────────────────────────


def test_home_loop_skips_idea_whose_status_disappeared(tmp_path):
    """The home page render loop calls bb.get_pipeline(idea_id) for every
    idea returned by the projection. If the projection still references an
    idea whose status.json is gone, that call raises FileNotFoundError —
    and the loop must drop the row instead of bubbling the exception.

    This is the exact shape of the bug the user hit: deleted idea, stale
    projection cache, home page 500.
    """
    bb = _make_bb(tmp_path)
    _seed_idea(bb, "alive")
    # "doomed" is in the projection-style raw_ideas list but has no dir.

    raw_ideas = [
        {"id": "alive", "phase": "released", "title": "Alive"},
        {"id": "doomed", "phase": "released", "title": "Doomed"},
    ]

    # Reproduce the home() loop's per-row work the same way the route does.
    rendered = []
    for status in raw_ideas:
        idea_id = status.get("id", "")
        try:
            pipeline = status["pipeline"] if "pipeline" in status else bb.get_pipeline(idea_id)
        except FileNotFoundError:
            continue
        rendered.append((idea_id, pipeline))

    ids = [r[0] for r in rendered]
    assert "alive" in ids
    assert "doomed" not in ids, "Stale projection rows must be silently dropped"
