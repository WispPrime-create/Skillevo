"""Register / remove SkillEvo's hooks in a Claude Code settings file.

Defaults are conservative: dry run, project-local scope (`.claude/settings.local.json`, normally git-ignored), an
absolute interpreter path (a bare `python` is often missing or the wrong one), a timestamped backup before any write,
and removal that touches only entries whose command runs `-m skillevo hook`. User scope needs explicit confirmation."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOOK_TIMEOUT_S = 10
EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop")
SENTINEL = "-m skillevo hook"


def settings_path(root: Path, scope: str, home: Path | None = None) -> Path:
    if scope == "project-local":
        return root / ".claude" / "settings.local.json"
    if scope == "project":
        return root / ".claude" / "settings.json"
    if scope == "user":
        return (home or Path.home()) / ".claude" / "settings.json"
    raise ValueError(f"unknown scope: {scope}")


def _quote(path: str) -> str:
    return subprocess.list2cmdline([path]) if sys.platform == "win32" else shlex.quote(path)


def command_for(event: str, python: str | None = None) -> str:
    return f"{_quote(python or sys.executable)} {SENTINEL} {event}"


def _is_ours(entry: dict) -> bool:
    return any(SENTINEL in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _strip_ours(data: dict) -> dict:
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for ev in list(hooks):
            kept = [e for e in hooks[ev] if not (isinstance(e, dict) and _is_ours(e))]
            if kept:
                hooks[ev] = kept
            else:
                del hooks[ev]
        if not hooks:
            del data["hooks"]
    return data


def plan_install(data: dict, python: str | None = None) -> dict:
    data = _strip_ours(json.loads(json.dumps(data)))
    hooks = data.setdefault("hooks", {})
    for ev in EVENTS:
        hooks.setdefault(ev, []).append(
            {"hooks": [{"type": "command", "command": command_for(ev, python), "timeout": HOOK_TIMEOUT_S}]})
    return data


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skillevo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    b = path.with_name(f"{path.name}.skillevo-bak-{time.strftime('%Y%m%d-%H%M%S')}")
    b.write_bytes(path.read_bytes())
    return b


def install(root: Path, scope: str = "project-local", apply: bool = False, python: str | None = None,
            home: Path | None = None) -> tuple[Path, str, Path | None]:
    path = settings_path(root, scope, home)
    before = _load(path)
    after = plan_install(before, python)
    diff = json.dumps(after.get("hooks", {}), indent=2)
    backup = None
    if apply:
        backup = _backup(path)
        _atomic_write(path, after)
    return path, diff, backup


def uninstall(root: Path, scope: str = "project-local", apply: bool = False, home: Path | None = None) -> tuple[Path, int, Path | None]:
    path = settings_path(root, scope, home)
    if not path.exists():
        return path, 0, None
    data = _load(path)
    count = sum(1 for ev in (data.get("hooks") or {}).values() for e in ev if isinstance(e, dict) and _is_ours(e))
    backup = None
    if apply and count:
        backup = _backup(path)
        data = _strip_ours(data)
        if data:
            _atomic_write(path, data)
        else:
            path.unlink()
    return path, count, backup


def installed(root: Path, scope: str = "project-local", home: Path | None = None) -> bool:
    path = settings_path(root, scope, home)
    try:
        data = _load(path)
    except (OSError, ValueError):
        return False
    return any(isinstance(e, dict) and _is_ours(e) for ev in (data.get("hooks") or {}).values() for e in ev)
