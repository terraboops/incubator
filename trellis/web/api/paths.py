"""Shared path constants for web routes."""
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = WEB_DIR / "frontend" / "templates"
