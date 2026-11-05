"""Claude Code hook entry point.

Contract (verified against the official hooks docs, 2026-11-02): payload arrives as JSON on stdin; context is
returned as {"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}} on stdout with exit code 0.
Exit code 2 would BLOCK a UserPromptSubmit, so this module always exits 0, swallows every error (logging it to the
data directory) and enforces a hard time budget."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

from . import collect, config, detect, evolve, paths, redact, retrieve
from .store import Store

EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop")
HARD_BUDGET_S = 5.0


def _log(msg: str) -> None:
    try:
        p = paths.log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 256_000:
            p.write_text("", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def process(event: str, raw: str) -> str:
    """Pure-ish handler: returns the stdout text to print (possibly empty). May raise; `run` guards it."""
    if config.disabled():
        return ""
    payload = json.loads(raw) if raw.strip() else {}
    root = paths.project_root(payload.get("cwd"))
    cfg = config.load(root)
    if not cfg.collection.enabled:
        return ""
    redact.set_extra_patterns(cfg.privacy.extra_redact_patterns)
    project = paths.project_id(root)
    with Store(paths.db_path()) as store:
        if event == "SessionStart":
            collect.maintenance(store, cfg)
            evolve.maybe_cycle(store, cfg, root, project)
        elif event == "PostToolUse":
            collect.handle_tool(store, cfg, project, root, payload, failed=False)
        elif event == "PostToolUseFailure":
            collect.handle_tool(store, cfg, project, root, payload, failed=True)
        elif event == "UserPromptSubmit":
            prompt = collect.handle_prompt(store, cfg, project, payload)
            text = retrieve.retrieve(store, cfg, project, prompt, str(payload.get("session_id") or ""))
            if text:
                return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}},
                                  ensure_ascii=False)
        elif event == "Stop":
            if not payload.get("stop_hook_active"):
                detect.learn(store, cfg, project)
    return ""


def run(event: str, raw: str) -> str:
    try:
        return process(event, raw)
    except Exception:  # noqa: BLE001 - a learning hook must never break the user's session
        _log(f"{event}: {traceback.format_exc(limit=4)}")
        return ""


def main(event: str) -> int:
    watchdog = threading.Timer(HARD_BUDGET_S, lambda: os._exit(0))
    watchdog.daemon = True
    watchdog.start()
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except (OSError, ValueError):
        raw = ""
    out = run(event, raw)
    if out:
        sys.stdout.write(out)
        sys.stdout.flush()
    watchdog.cancel()
    return 0
