"""Deterministic learning detectors: one-off high-impact lessons and cross-session repeated patterns.

No model calls. Each detector turns recorded events into experiences; the store de-duplicates by fingerprint."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from . import redact, tokenize
from .collect import error_signature
from .config import Config
from .store import Store

_IRREVERSIBLE = re.compile(
    r"(?i)\b(rm\s+-\w*r\w*f|rm\s+-rf|drop\s+(?:table|database)|truncate\s+table|git\s+push\s+(?:-f|--force)|"
    r"reset\s+--hard|clean\s+-\w*f|--no-verify|chmod\s+-R|mkfs|dd\s+if=|delete\s+from|force[- ]push|kubectl\s+delete)\b"
)
_READ_ONLY = {"Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch"}


def _tags(*texts: str, limit: int = 6) -> list[str]:
    seen: dict[str, None] = {}
    for t in tokenize.tokens(" ".join(texts)):
        if len(t) >= 3 and not t.isdigit():
            seen.setdefault(t, None)
        if len(seen) >= limit:
            break
    return list(seen)


def _status_for(text: str) -> str:
    return "quarantined" if redact.looks_like_injection(text) else "active"


def _sessions_of(store: Store, event_ids: list) -> list[str]:
    ids = [i for i in event_ids if isinstance(i, int)]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return [r[0] for r in store.db.execute(f"SELECT DISTINCT session FROM events WHERE id IN ({marks}) AND session != ''", ids)]


def _add(store: Store, project: str, kind: str, title: str, body: str, salience: float, confidence: float,
         evidence: list, tags: Iterable[str] | None = None, sessions: Iterable[str] | None = None) -> bool:
    body = redact.clean(body, 600)
    title = redact.clean(title, 120)
    _, created = store.add_experience(
        project, kind, title, body, tags or _tags(title, body), "auto", salience, confidence,
        evidence=evidence, status=_status_for(title + " " + body),
        sessions=sessions if sessions is not None else _sessions_of(store, evidence))
    return created


def salience(summary: str, failures_before_fix: int = 0, corrected: bool = False) -> float:
    """Impact score in [0, 1] from failure cost, user correction and irreversibility; independent of repetition."""
    s = 0.3 + 0.1 * min(failures_before_fix, 3)
    if corrected:
        s += 0.4
    if _IRREVERSIBLE.search(summary or ""):
        s += 0.3
    return round(min(s, 1.0), 2)


def corrections(store: Store, cfg: Config, project: str, events: list) -> int:
    n = 0
    prev_tool = None
    for ev in events:
        if ev["event"] == "tool" or ev["event"] == "tool_failure":
            prev_tool = ev
        elif ev["event"] == "correction":
            text = ev["summary"] or ""
            ctx = f" Context: the previous action was `{prev_tool['summary']}`." if prev_tool else ""
            sal = salience((prev_tool["summary"] if prev_tool else "") + " " + text, corrected=True)
            n += _add(store, project, "lesson", "User correction: " + text[:80],
                      f"The user corrected the assistant: \"{text}\".{ctx}", sal, 0.4, [ev["id"]])
    return n


def fix_pairs(store: Store, cfg: Config, project: str, events: list) -> int:
    n = 0
    by_session: dict[str, list] = defaultdict(list)
    for ev in events:
        by_session[ev["session"] or ""].append(ev)
    for evs in by_session.values():
        for i, ev in enumerate(evs):
            if ev["event"] != "tool_failure":
                continue
            fails = 1
            for later in evs[i + 1: i + 9]:
                if later["tool"] != ev["tool"]:
                    continue
                if later["event"] == "tool_failure":
                    fails += 1
                    continue
                if later["event"] == "tool" and later["summary"] != ev["summary"].split(" | error:")[0]:
                    err = ev["summary"].split(" | error:", 1)[-1].strip() if " | error:" in ev["summary"] else ev["sig"]
                    sal = salience(ev["summary"] + " " + later["summary"], failures_before_fix=fails)
                    n += _add(store, project, "lesson", f"Fix for {ev['tool']} failure: {ev['sig'][:60] or err[:60]}",
                              f"{ev['tool']} failed ({err}). What worked afterwards: {later['summary']}.",
                              sal, 0.4, [ev["id"], later["id"]])
                break
    return n


def one_off_irreversible(store: Store, cfg: Config, project: str, events: list) -> int:
    """A failed irreversible-looking command is worth remembering even if it happened once."""
    n = 0
    for ev in events:
        if ev["event"] == "tool_failure" and _IRREVERSIBLE.search(ev["summary"] or ""):
            n += _add(store, project, "lesson", "Risky command failed: " + ev["summary"][:70],
                      f"A risky command failed: {ev['summary']}. Re-check intent and safer alternatives before retrying.",
                      salience(ev["summary"], failures_before_fix=1), 0.4, [ev["id"]])
    return n


def recurring_failures(store: Store, cfg: Config, project: str, events: list) -> int:
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for ev in events:
        if ev["event"] == "tool_failure" and ev["sig"]:
            groups[(ev["tool"], ev["sig"])].append(ev)
    n = 0
    for (tool, sig), evs in groups.items():
        sessions = {e["session"] for e in evs}
        if len(sessions) >= cfg.detection.min_pattern_sessions:
            n += _add(store, project, "pattern", f"Recurring {tool} failure: {sig[:70]}",
                      f"{tool} failed with '{sig}' in {len(sessions)} separate sessions. Example: {evs[-1]['summary']}.",
                      0.5, min(0.9, 0.4 + 0.1 * len(sessions)), [e["id"] for e in evs[-5:]])
    return n


def _step(ev) -> str:
    if ev["tool"] == "Bash":
        words = (ev["summary"] or "")[5:].strip().split()
        return "Bash:" + (words[0] if words else "")
    return ev["tool"]


def workflows(store: Store, cfg: Config, project: str, events: list, max_new: int = 5) -> int:
    by_session: dict[str, list[str]] = defaultdict(list)
    for ev in events:
        if ev["event"] in ("tool", "tool_failure") and ev["tool"]:
            by_session[ev["session"] or ""].append(_step(ev))
    counts: dict[tuple[str, ...], set] = defaultdict(set)
    for sess, steps in by_session.items():
        for size in (3, 4):
            for i in range(len(steps) - size + 1):
                gram = tuple(steps[i: i + size])
                if len(set(gram)) == 1 or all(g in _READ_ONLY for g in gram):
                    continue
                counts[gram].add(sess)
    frequent = [(g, s) for g, s in counts.items() if len(s) >= cfg.detection.min_pattern_sessions]
    frequent.sort(key=lambda gs: (-len(gs[0]), -len(gs[1])))
    n = 0
    for gram, sessions in frequent[:max_new]:
        arrow = " -> ".join(gram)
        n += _add(store, project, "workflow", "Repeated workflow: " + arrow,
                  f"The sequence {arrow} occurred in {len(sessions)} separate sessions.", 0.4,
                  min(0.9, 0.3 + 0.1 * len(sessions)), [], tags=_tags(arrow), sessions=sessions)
    return n


def learn(store: Store, cfg: Config, project: str, since_id: int | None = None) -> dict[str, int]:
    """Run all detectors over events newer than the stored cursor. Returns counts of NEW experiences."""
    key = f"learn_cursor:{project}"
    if since_id is None:
        row = store.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        since_id = int(row["value"]) if row else 0
    new_events = [e for e in store.events(project, limit=20000) if e["id"] > since_id]
    window = store.events(project, limit=20000)  # cross-session patterns need history, not just new events
    out = {
        "corrections": corrections(store, cfg, project, new_events),
        "fixes": fix_pairs(store, cfg, project, new_events),
        "risky": one_off_irreversible(store, cfg, project, new_events),
        "recurring_failures": recurring_failures(store, cfg, project, window),
        "workflows": workflows(store, cfg, project, window),
    }
    if window:
        store.db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(window[-1]["id"])))
        store.db.commit()
    return out
