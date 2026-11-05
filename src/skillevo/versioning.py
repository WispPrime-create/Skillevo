"""Snapshot-based version history and rollback for skill directories. Snapshots live in the data directory,
never inside the skill itself, so deleting or replacing a skill cannot delete its history."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import paths, skills


def _root(name: str) -> Path:
    return paths.versions_dir() / name


def list_versions(name: str) -> list[dict]:
    out = []
    root = _root(name)
    if root.is_dir():
        for d in sorted(root.glob("v*"), key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else 0):
            try:
                out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def snapshot(skill_dir: Path, name: str, reason: str) -> int | None:
    if not skill_dir.is_dir():
        return None
    versions = list_versions(name)
    n = (versions[-1]["version"] + 1) if versions else 1
    dest = _root(name) / f"v{n}"
    shutil.copytree(skill_dir, dest / "content")
    meta = {"version": n, "ts": time.time(), "reason": reason, "hash": skills.dir_hash(dest / "content")}
    (dest / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return n


def rollback(skill_dir: Path, name: str, version: int | None = None) -> int:
    """Restore a snapshot (default: the latest). The current state is snapshotted first, so rollback is itself undoable."""
    versions = list_versions(name)
    if not versions:
        raise LookupError(f"no versions recorded for {name}")
    target = version if version is not None else versions[-1]["version"]
    src = _root(name) / f"v{target}" / "content"
    if not src.is_dir():
        raise LookupError(f"version v{target} of {name} not found")
    snapshot(skill_dir, name, f"pre-rollback to v{target}")
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    shutil.copytree(src, skill_dir)
    return target
