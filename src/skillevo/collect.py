"""Turn hook payloads into stored events. Everything stored passes through redaction and truncation first."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from . import redact
from .config import Config
from .store import Store

_CORRECTION = re.compile(
    r"(?i)^\s*(?:no[,.! ]|nope\b|wrong\b|that'?s (?:not|wrong|incorrect)|don'?t\b|do not\b|stop\b|instead\b|"
    r"actually\b|not what i|you (?:should|shouldn'?t|forgot|missed|broke)|why did you|never\b|always\b|"
    r"不对|错了|不要|别再|别用|不是这样|应该是|你忘了|你漏了|不应该|重新来|撤销)"
)
_SKILL_FIELDS = ("skill", "name", "command", "skill_name")


def is_correction(prompt: str) -> bool:
    head = prompt.strip()[:200]
    return bool(_CORRECTION.search(head)) and not head.startswith("/")


def typed_skill(prompt: str) -> str | None:
    m = re.match(r"\s*/([A-Za-z0-9][A-Za-z0-9_.:-]*)", prompt)
    return m.group(1) if m else None


_EXIT_LINE = re.compile(r"(?i)^exit code \d+$")


def informative_line(text: str) -> str:
    """First useful line of an error. Claude Code reports Bash failures as 'Exit code N' followed by stderr."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in lines:
        if not _EXIT_LINE.match(ln):
            return ln
    return lines[0] if lines else ""


def error_signature(text: str) -> str:
    line = informative_line(text)
    line = re.sub(r"0x[0-9a-fA-F]+|\b\d+\b|(?:/[\w.@+-]+){2,}|[A-Za-z]:\\[\w\\.-]+", "#", line.lower())
    return re.sub(r"\s+", " ", line)[:100]


def summarize(tool: str, tin: dict[str, Any] | None, root: Path | None, limit: int) -> str:
    tin = tin or {}
    if tool == "Bash":
        return redact.clean("Bash: " + str(tin.get("command", "")).replace("\n", " ; "), limit)
    if tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        raw = str(tin.get("file_path") or tin.get("notebook_path") or "")
        try:
            rel = os.path.relpath(raw, root) if root and raw else raw
            name = rel if not rel.startswith("..") else os.path.basename(raw)
        except ValueError:
            name = os.path.basename(raw)
        return redact.clean(f"{tool} {name}", limit)
    if tool in ("Grep", "Glob"):
        return redact.clean(f"{tool} {tin.get('pattern', '')}", limit)
    if tool in ("WebFetch", "WebSearch"):
        raw = str(tin.get("url") or tin.get("query") or "")
        host = re.sub(r"^\w+://", "", raw).split("/")[0] if tin.get("url") else raw
        return redact.clean(f"{tool} {host}", limit)
    if tool == "Skill":
        return f"Skill {_skill_name(tin)}"
    if tool.startswith("mcp__"):
        return tool
    return redact.clean(f"{tool} {','.join(sorted(tin))}", limit)


def _skill_name(tin: dict[str, Any]) -> str:
    for f in _SKILL_FIELDS:
        v = tin.get(f)
        if isinstance(v, str) and v:
            return v.split()[0].lstrip("/")
    return "unknown"


def handle_tool(store: Store, cfg: Config, project: str, root: Path | None, payload: dict[str, Any], failed: bool) -> None:
    tool = str(payload.get("tool_name") or "")
    if not tool:
        return
    session = str(payload.get("session_id") or "")
    limit = cfg.collection.max_payload_chars
    summary = summarize(tool, payload.get("tool_input"), root, limit)
    sig = ""
    if failed:
        err = informative_line(redact.clean(str(payload.get("error") or ""), 400))
        sig = error_signature(err)
        if sig in ("", "exit code #") and tool == "Bash":  # nothing specific: keep unrelated commands apart
            head = str(( payload.get("tool_input") or {}).get("command", "")).split()[:1]
            sig = f"{head[0] if head else 'bash'}: {sig or 'failed'}"
        summary = redact.truncate(f"{summary} | error: {redact.truncate(err, 120)}", limit + 130)
    store.add_event(project, "tool_failure" if failed else "tool", session, tool, summary, ok=not failed, sig=sig)
    if tool == "Skill":
        store.record_skill_event(project, _skill_name(payload.get("tool_input") or {}), "tool", session, ok=not failed)


def handle_prompt(store: Store, cfg: Config, project: str, payload: dict[str, Any]) -> str:
    """Record the prompt-derived signals and return the prompt for retrieval."""
    prompt = str(payload.get("prompt") or "")
    session = str(payload.get("session_id") or "")
    skill = typed_skill(prompt)
    if skill:
        store.record_skill_event(project, skill, "typed", session)
    if cfg.privacy.store_prompts:
        kind = "correction" if is_correction(prompt) else "prompt"
        store.add_event(project, kind, session, "", redact.clean(prompt.replace("\n", " "), 300))
    return prompt


def maintenance(store: Store, cfg: Config) -> None:
    """Prune at most once a day."""
    row = store.db.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
    now = time.time()
    if row and now - float(row["value"]) < 86400:
        return
    store.prune(cfg.collection.retention_days, cfg.collection.max_events_per_project)
    store.db.execute("INSERT INTO meta(key, value) VALUES ('last_prune', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    store.db.commit()
