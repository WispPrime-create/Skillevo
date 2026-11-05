"""Optional importers for data produced by other tools. They read files only; no foreign code is executed.

ECC (affaan-m/ECC, MIT): instinct files (`---` frontmatter blocks followed by a body) and `skill-runs.jsonl`.
The instinct parsing approach follows ECC's `instinct-cli.py` file format; see NOTICE."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import detect, frontmatter, redact
from .store import Store

_EXT = (".yaml", ".yml", ".md")


def parse_instinct_file(content: str) -> list[dict]:
    out: list[dict] = []
    lines = content.removeprefix("﻿").split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip() != "---":
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() != "---":
            j += 1
        if j >= len(lines):
            break
        meta = frontmatter.parse("\n".join(lines[i + 1: j]))
        k = j + 1
        while k < len(lines) and lines[k].strip() != "---":
            k += 1
        meta["content"] = "\n".join(lines[j + 1: k]).strip()
        if meta.get("id"):
            out.append(meta)
        i = k
    return out


def import_instincts(store: Store, project: str, path: Path) -> dict[str, int]:
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.suffix.lower() in _EXT) if path.is_dir() else []
    seen = added = quarantined = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ins in parse_instinct_file(text):
            seen += 1
            trigger = str(ins.get("trigger") or ins.get("id"))
            body = str(ins.get("content") or trigger)
            try:
                conf = max(0.1, min(0.6, float(ins.get("confidence", 0.3))))  # foreign data is capped as low-trust
            except (TypeError, ValueError):
                conf = 0.3
            raw = f"{trigger} {body}"
            status = "quarantined" if redact.looks_like_injection(raw) else "active"
            domain = str(ins.get("domain") or "")
            _, created = store.add_experience(
                project, "lesson", redact.clean(trigger, 120), redact.clean(body, 600), [domain] if domain else [],
                "ecc-instinct", salience=0.3, confidence=conf,
                scope="global" if str(ins.get("scope")) == "global" else "project", status=status)
            added += created
            quarantined += created and status == "quarantined"
    store.audit("import", "ecc-instincts", str(path.name), {"seen": seen, "added": added})
    return {"seen": seen, "added": added, "quarantined": quarantined}


def import_skill_runs(store: Store, project: str, path: Path) -> dict[str, int]:
    n = bad = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"imported": 0, "skipped": 0}
    for line in lines:
        try:
            rec = json.loads(line)
            skill = str(rec["skill_id"]).strip()
            if not skill:
                raise KeyError
        except (ValueError, KeyError, TypeError):
            bad += 1
            continue
        try:
            ts = datetime.fromisoformat(str(rec.get("recorded_at")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            ts = time.time()
        store.db.execute("INSERT INTO skill_events(ts, project, skill, source, session, ok) VALUES (?,?,?,?,?,?)",
                         (ts, project, skill, "import", "", 0 if rec.get("outcome") == "failure" else 1))
        n += 1
    store.db.commit()
    return {"imported": n, "skipped": bad}


__all__ = ["parse_instinct_file", "import_instincts", "import_skill_runs", "detect"]
