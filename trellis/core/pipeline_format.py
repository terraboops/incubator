"""Format-aware pipeline template loading and saving.

Supports both ``.prose`` and ``.yaml``/``.yml`` pipeline templates.  The
internal representation is always the canonical pipeline dict used by the
rest of Trellis.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trellis.core.prose_parser import emit_pipeline_prose, parse_pipeline_prose


def detect_format(path: Path) -> str:
    """Return ``'prose'`` or ``'yaml'`` based on *path*'s extension."""
    if path.suffix == ".prose":
        return "prose"
    return "yaml"


def load_pipeline(path: Path) -> dict:
    """Load a pipeline template from a ``.prose`` or ``.yaml`` file."""
    text = path.read_text()
    if detect_format(path) == "prose":
        return parse_pipeline_prose(text)
    return yaml.safe_load(text) or {}


def save_pipeline(path: Path, data: dict, fmt: str = "yaml") -> None:
    """Write a pipeline template in the given format.

    Parameters
    ----------
    path:
        Destination file.  The extension is **not** checked against *fmt*;
        callers should ensure they match.
    data:
        Canonical pipeline dict.
    fmt:
        ``'prose'`` or ``'yaml'`` (default).
    """
    if fmt == "prose":
        path.write_text(emit_pipeline_prose(data))
    else:
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def find_template(templates_dir: Path, name: str) -> Path | None:
    """Find a template by *name*, checking ``.prose``, ``.yaml``, ``.yml``."""
    for ext in (".prose", ".yaml", ".yml"):
        candidate = templates_dir / f"{name}{ext}"
        if candidate.exists():
            return candidate
    return None
