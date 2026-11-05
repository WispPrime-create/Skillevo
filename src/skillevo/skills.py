"""Skill discovery, validation and ownership. Works on any directory of `<name>/SKILL.md` skills."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter

MARKER = ".skillevo.json"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class Skill:
    name: str
    path: Path
    text: str
    meta: dict = field(default_factory=dict)
    body: str = ""

    @property
    def description(self) -> str:
        d = self.meta.get("description", "")
        return d if isinstance(d, str) else str(d)

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))


def parse(text: str, fallback_name: str = "") -> tuple[dict, str]:
    meta, body = frontmatter.split(text)
    meta.setdefault("name", fallback_name)
    return meta, body


def load(skill_dir: Path) -> Skill | None:
    f = skill_dir / "SKILL.md"
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = parse(text, skill_dir.name)
    return Skill(name=str(meta.get("name") or skill_dir.name), path=skill_dir, text=text, meta=meta, body=body)


def discover(dirs: list[Path]) -> list[Skill]:
    out: list[Skill] = []
    seen: set[Path] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            real = child.resolve()
            if child.is_dir() and real not in seen and (child / "SKILL.md").is_file():
                seen.add(real)
                s = load(child)
                if s:
                    out.append(s)
    return out


def validate(text: str, dir_name: str = "") -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Rules are a deliberately lenient reimplementation: Claude Code accepts more
    frontmatter than stricter third-party validators, so only structural problems are errors."""
    errors: list[str] = []
    warnings: list[str] = []
    meta, body = frontmatter.split(text)
    if not meta:
        errors.append("missing YAML frontmatter")
    name = str(meta.get("name") or dir_name or "")
    desc = meta.get("description")
    if not name:
        errors.append("missing name")
    elif len(name) > 64:
        errors.append("name longer than 64 characters")
    elif not NAME_RE.match(name):
        warnings.append("name is not lowercase-hyphenated")
    if dir_name and name and name != dir_name:
        warnings.append("name differs from directory name")
    if not isinstance(desc, str) or not desc.strip():
        errors.append("missing description")
    else:
        if len(desc) > 1024:
            errors.append("description longer than 1024 characters")
        if len(desc) < 30:
            warnings.append("description is very short; Claude may not know when to use this skill")
    if not body.strip():
        errors.append("empty body")
    return errors, warnings


def dir_hash(skill_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and p.name != MARKER:
            h.update(str(p.relative_to(skill_dir)).replace("\\", "/").encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def read_marker(skill_dir: Path) -> dict | None:
    try:
        data = json.loads((skill_dir / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("managed") is True and data.get("created_by") == "skillevo" else None


def write_marker(skill_dir: Path, **extra) -> None:
    prev = read_marker(skill_dir) or {}
    data = {"managed": True, "created_by": "skillevo", "version": int(prev.get("version", 0)) + 1,
            "hash": dir_hash(skill_dir), **extra}
    (skill_dir / MARKER).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def ownership(skill_dir: Path) -> str:
    """'absent' | 'user' (no marker) | 'managed' (intact marker) | 'modified' (managed but edited since)."""
    if not skill_dir.exists():
        return "absent"
    marker = read_marker(skill_dir)
    if marker is None:
        return "user"
    return "managed" if marker.get("hash") == dir_hash(skill_dir) else "modified"


def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "skill"
