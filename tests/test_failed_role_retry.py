"""Tests for the `clear_failed_role` primitive + HTTP + CLI integrations."""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from trellis.core.blackboard import Blackboard
from trellis.core.pipeline import DEFAULT_STAGE_NAME


def _set_failed(bb: Blackboard, idea_id: str, role: str) -> None:
    """Drop the given role into state=failed for tests."""
    status = bb.get_status(idea_id)
    role_state = dict(status["role_state"])
    stage_state = dict(role_state[DEFAULT_STAGE_NAME])
    stage_state[role] = {"state": "failed", "iterations": 5}
    role_state[DEFAULT_STAGE_NAME] = stage_state
    bb.update_status(idea_id, role_state=role_state)


class TestClearFailedRole:
    def test_resets_state_to_pending(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        _set_failed(blackboard, idea_id, "ideation")

        blackboard.clear_failed_role(idea_id, "ideation", note="investigated")

        role_entry = blackboard.get_status(idea_id)["role_state"][DEFAULT_STAGE_NAME]["ideation"]
        assert role_entry["state"] == "pending"
        assert role_entry["iterations"] == 0
        assert "retried_at" in role_entry
        assert role_entry["retry_note"] == "investigated"

    def test_logs_history_entry(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        _set_failed(blackboard, idea_id, "ideation")

        blackboard.clear_failed_role(idea_id, "ideation", actor="human:alice", note="ok")

        history = blackboard.get_status(idea_id)["phase_history"]
        assert history[-1]["role"] == "ideation"
        assert history[-1]["from"] == "failed"
        assert history[-1]["to"] == "pending"
        assert history[-1]["actor"] == "human:alice"
        assert history[-1]["note"] == "ok"

    def test_raises_when_role_not_failed(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        # Ideation has no state at all yet.
        with pytest.raises(ValueError, match="not in 'failed'"):
            blackboard.clear_failed_role(idea_id, "ideation")

    def test_raises_when_role_in_proceed(self, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        blackboard.mark_role_state(idea_id, "ideation", "proceed")
        with pytest.raises(ValueError, match="not in 'failed'"):
            blackboard.clear_failed_role(idea_id, "ideation")


class TestRetryRoute:
    @pytest.fixture
    def client(self, trellis_settings):
        from unittest.mock import patch

        with (
            patch("trellis.config.get_settings", return_value=trellis_settings),
            patch(
                "trellis.web.api.routes.ideas.get_settings",
                return_value=trellis_settings,
            ),
        ):
            from trellis.web.api.app import create_app

            with TestClient(create_app()) as client:
                yield client

    def test_retry_returns_404_for_missing_idea(self, client):
        resp = client.post("/ideas/nonexistent/roles/ideation/retry")
        assert resp.status_code == 404

    def test_retry_returns_409_when_role_not_failed(self, client, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        resp = client.post(f"/ideas/{idea_id}/roles/ideation/retry")
        assert resp.status_code == 409
        assert "not in 'failed'" in resp.json()["error"]

    def test_retry_success_clears_failed(self, client, blackboard):
        idea_id = blackboard.create_idea("Test", "body")
        _set_failed(blackboard, idea_id, "implementation")

        resp = client.post(
            f"/ideas/{idea_id}/roles/implementation/retry",
            json={"note": "manual fix applied"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["role"] == "implementation"

        role_entry = blackboard.get_status(idea_id)["role_state"][DEFAULT_STAGE_NAME][
            "implementation"
        ]
        assert role_entry["state"] == "pending"
        assert role_entry["retry_note"] == "manual fix applied"


class TestRetryCLI:
    def test_cli_retry_success(self, trellis_settings, blackboard):
        from unittest.mock import patch

        idea_id = blackboard.create_idea("Test", "body")
        _set_failed(blackboard, idea_id, "validation")

        from trellis.cli import app

        runner = CliRunner()
        with patch("trellis.cli.get_settings", return_value=trellis_settings):
            result = runner.invoke(app, ["retry", idea_id, "validation", "--note", "fix"])

        assert result.exit_code == 0, result.stdout
        assert "Reset 'validation'" in result.stdout
        assert (
            blackboard.get_status(idea_id)["role_state"][DEFAULT_STAGE_NAME]["validation"]["state"]
            == "pending"
        )

    def test_cli_retry_missing_role_returns_error(self, trellis_settings, blackboard):
        from unittest.mock import patch

        idea_id = blackboard.create_idea("Test", "body")
        from trellis.cli import app

        runner = CliRunner()
        with patch("trellis.cli.get_settings", return_value=trellis_settings):
            result = runner.invoke(app, ["retry", idea_id, "ideation"])
        assert result.exit_code == 1
        assert "not in 'failed'" in result.stdout
