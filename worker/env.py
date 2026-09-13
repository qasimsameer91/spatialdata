"""Minimal .env loader for local secrets such as AI33_API_KEY.

Dependency-free rather than python-dotenv: this is the only place the project
reads an environment file, and the rules that matter fit in a few lines.

Real environment variables always win over the file, so a CI secret or a
Railway variable is never silently replaced by a stray local .env.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

_loaded = False


def load_env(path: Optional[Path] = None) -> None:
    """Read KEY=value lines from the project's .env into os.environ, once."""
    global _loaded
    if path is None:
        if _loaded:
            return
        _loaded = True
        path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
