"""Shared Jinja2 template filters for the trellis web dashboard."""

from __future__ import annotations

import json

import markdown
from fastapi.templating import Jinja2Templates

# ── Cadence label ────────────────────────────────────────────────────

_CADENCE_PATTERNS = {
    "0 */6 * * *": "every 6h",
    "0 */4 * * *": "every 4h",
    "0 */12 * * *": "every 12h",
    "0 8 * * *": "daily at 8am",
    "0 0 * * *": "daily at midnight",
    "*/30 * * * *": "every 30min",
    "*/5 * * * *": "every 5min",
}


def cadence_label(cron: str) -> str:
    """Turn common cron expressions into readable labels."""
    return _CADENCE_PATTERNS.get(cron, cron)


# ── Markdown ─────────────────────────────────────────────────────────

_md = markdown.Markdown(extensions=["tables", "fenced_code", "nl2br", "toc"])


def render_markdown(text: str) -> str:
    _md.reset()
    return _md.convert(text)


# ── Phase label ──────────────────────────────────────────────────────

_PHASE_LABELS = {
    "released": "ready",
    "killed": "shelved",
    "release": "releasing",
    "ideation_review": "reviewing ideation",
    "implementation_review": "reviewing build",
    "validation_review": "reviewing tests",
}


def phase_label(phase: str) -> str:
    """Map internal phase names to display labels."""
    label = _PHASE_LABELS.get(phase, phase)
    return label.replace("_", " ")


# ── Role label ───────────────────────────────────────────────────────

_ROLE_LABELS = {
    "ideation": "ideation",
    "implementation": "build",
    "validation": "validate",
    "release": "release",
    "competitive-watcher": "competitive",
    "research-watcher": "research",
    "prioritizer": "prioritize",
}


def role_label(r: str) -> str:
    return _ROLE_LABELS.get(r, r.replace("-watcher", "").replace("-", " "))


# ── Registration ─────────────────────────────────────────────────────


def setup_filters(templates: Jinja2Templates) -> None:
    """Register all shared Jinja2 filters on a Templates instance."""
    templates.env.filters["cadence_label"] = cadence_label
    templates.env.filters["markdown"] = render_markdown
    templates.env.filters["phase_label"] = phase_label
    templates.env.filters["role_label"] = role_label
    templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2)
