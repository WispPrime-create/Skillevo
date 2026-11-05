"""Filesystem locations. Everything is overridable; nothing is hardcoded to a user."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def data_home() -> Path:
    """Directory holding the experience database, logs and version snapshots."""
    override = os.environ.get("SKILLEVO_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "skillevo"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "skillevo"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "skillevo"


def user_config_path() -> Path:
    override = os.environ.get("SKILLEVO_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "skillevo" / "config.toml"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "skillevo" / "config.toml"


def project_root(cwd: str | None = None) -> Path:
    """Project directory: CLAUDE_PROJECT_DIR if set, else the given cwd, else os.getcwd()."""
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or cwd or os.getcwd()
    return Path(raw).expanduser().resolve()


def project_config_path(root: Path) -> Path:
    return root / ".skillevo" / "config.toml"


def project_id(root: Path) -> str:
    """Stable, non-reversible identifier so database rows do not store raw paths."""
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]


def db_path() -> Path:
    return data_home() / "skillevo.db"


def versions_dir() -> Path:
    return data_home() / "versions"


def staging_dir() -> Path:
    return data_home() / "candidates"


def log_path() -> Path:
    return data_home() / "skillevo.log"
