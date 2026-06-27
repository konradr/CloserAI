"""Environment loading for the CRM component."""

from __future__ import annotations

import os
from pathlib import Path

_loaded = False


def load_env() -> None:
    """Load the repo .env once. Safe to call repeatedly."""
    global _loaded
    if _loaded:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        _loaded = True
        return
    # crm/config.py -> repo root is one parent up.
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path)
    _loaded = True


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
