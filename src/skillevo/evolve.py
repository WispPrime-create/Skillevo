"""One evolution cycle: amend managed skills with new related lessons, then propose new skills from the rest.
Bounded: amendment attempts per skill are capped, and the policy mode decides what is installed."""

from __future__ import annotations

import json
import math
from pathlib import Path

from . import candidates, orchestrate, redact, skills, smart, tokenize
from .config import Config
from .lifecycle import Lifecycle, PolicyError
from .store import Store


def _covered(store: Store) -> set[int]:
    out: set[int] = set()
    for r in store.list_skill_states():
        m = json.loads(r["meta"])
        out |= set(m.get("evidence", [])) | set(m.get("rejected_evidence", [])) | set((m.get("pending") or {}).get("evidence", []))
    # Distillation replaces raw rows with new ids. A distilled row is covered when any row it was built from is covered,
    # and a superseded row is covered when its replacement is, so encoded lessons are not proposed a second time.
    for row in store.db.execute("SELECT id, evidence, superseded_by FROM experiences "
                                "WHERE source='llm-distilled' OR superseded_by IS NOT NULL"):
        try:
            built_from = {int(i) for i in json.loads(row["evidence"])}
        except (ValueError, TypeError):
            built_from = set()
        if row["id"] in out or built_from & out:
            out.add(row["id"])
            out |= built_from
        if row["superseded_by"] in out:
            out.add(row["id"])
    return out


def amend_text(text: str, rows: list) -> str:
    bullets = [f"- {_clean(r['body'])} _(evidence #{r['id']})_" for r in rows]
    marker = "## Lessons"
    if marker not in text:
        return text.rstrip() + "\n\n" + marker + "\n" + "\n".join(bullets) + "\n"
    head, tail = text.split(marker, 1)
    nxt = tail.find("\n## ")
    section = tail if nxt == -1 else tail[:nxt]
    rest = "" if nxt == -1 else tail[nxt:]
    return head + marker + section.rstrip("\n") + "\n" + "\n".join(bullets) + "\n" + rest


def _clean(text: str) -> str:
    return redact.sanitize_for_injection(text)


def _unique_name(lc: Lifecycle, name: str) -> str:
    if skills.ownership(lc.target_dir(name)) in ("user", "modified"):
        return name + "-evo"
    return name


def cycle(store: Store, cfg: Config, root: Path, project: str, orch: orchestrate.Orchestrator | None = None) -> dict:
    """One evolution cycle. With `orch`, learning is distilled by a cheap model, new skills are drafted by a mid model and
    autonomous installs are gated by a strong reviewer; every model step falls back to the deterministic one."""
    lc = Lifecycle(store, cfg, root, project)
    report: dict = {"amendments": {}, "candidates": {}, "skipped": {}}
    smart_on = orch is not None and orch.enabled()
    if smart_on:
        report["distilled"] = smart.distill(orch, store, cfg, project)

        def reviewer(name: str, text: str, pend: dict):
            rows = [store.get_experience(i) for i in pend.get("evidence", [])]
            originals = [f"{r['title']}: {r['body']}" for r in rows if r]
            kind = "new-skill"
            installed = lc.target_dir(name) / "SKILL.md"
            if pend.get("kind") == "amendment" and installed.is_file():
                kind = "amendment"   # the draft contains the whole skill, so the reviewer must see the existing text too
                originals = [installed.read_text(encoding="utf-8")] + originals
            return smart.review(orch, kind, originals or [name], text)

        lc.reviewer = reviewer

    covered = _covered(store)
    pool = [r for kind in candidates.KINDS for r in store.list_experiences(project, "active", kind)]
    for row in store.list_skill_states():
        name = row["name"]
        if row["state"] != "active":
            continue
        if row["attempts"] >= cfg.evolution.max_attempts:
            report["skipped"][name] = f"attempt cap reached ({cfg.evolution.max_attempts})"
            continue
        ok, why = lc.can_auto_modify(name)
        if not ok or skills.ownership(lc.target_dir(name)) != "managed":
            report["skipped"][name] = why if not ok else "not installed"
            continue
        current = (lc.target_dir(name) / "SKILL.md").read_text(encoding="utf-8")
        vec = set(tokenize.tokens(current))
        related = [r for r in pool if r["id"] not in covered
                   and _sim(set(r["index_text"].split()), vec) >= 0.2][:5]
        if not related:
            continue
        lc.stage(name, amend_text(current, related), [r["id"] for r in related], "amendment")
        verdict = lc.evaluate(name)
        covered |= {r["id"] for r in related}
        report["amendments"][name] = lc.apply_policy(name) if verdict.promote else f"not promoted: {verdict.reason}"

    for spec in candidates.generate(store, cfg, project, covered):
        name = _unique_name(lc, spec.name)
        text = spec.text if name == spec.name else spec.text.replace(f"name: {spec.name}", f"name: {name}", 1)
        if lc.state(name) in ("active", "retired"):
            report["skipped"][name] = f"already {lc.state(name)}"
            continue
        drafted_by = "template"
        if smart_on:
            rows = [r for r in (store.get_experience(i) for i in spec.evidence) if r]
            drafted = smart.draft_skill(orch, rows, name, cfg.curator.max_skill_bytes)
            if drafted:
                text, drafted_by = drafted, "llm"
        lc.stage(name, text, spec.evidence, "new", sessions=spec.sessions, drafted_by=drafted_by)
        verdict = lc.evaluate(name)
        report["candidates"][name] = lc.apply_policy(name) if verdict.promote else f"rejected: {verdict.reason}"
    return report


def plan_smart(store: Store, cfg: Config, project: str, orch: orchestrate.Orchestrator) -> dict:
    """Dry run: report which model calls an evolution cycle would make. Stages and installs nothing."""
    smart.distill(orch, store, cfg, project)
    covered = _covered(store)
    specs = candidates.generate(store, cfg, project, covered)
    for spec in specs:
        rows = [r for r in (store.get_experience(i) for i in spec.evidence) if r]
        smart.draft_skill(orch, rows, spec.name, cfg.curator.max_skill_bytes)
    return {"candidate_drafts": len(specs)}


def maybe_cycle(store: Store, cfg: Config, root: Path, project: str, now: float | None = None,
                spawn=orchestrate.spawn_background) -> dict | None:
    """Run detectors and one evolution cycle at most once per `auto_interval_hours`. Follows the configured policy,
    so with the default (review-only) it only stages and evaluates candidates."""
    import time
    from . import detect
    hours = cfg.evolution.auto_interval_hours
    if hours <= 0:
        return None
    now = now if now is not None else time.time()
    key = f"last_evolve:{project}"
    row = store.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row and now - float(row["value"]) < hours * 3600:
        return None
    store.db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(now)))
    store.db.commit()
    detect.learn(store, cfg, project)
    if cfg.llm.enabled and cfg.orchestration.background:
        # Model work never runs inside a hook: hand it to a detached process and return immediately.
        spawn(root)
        return {"background": True}
    return cycle(store, cfg, root, project)


def _sim(a: set[str], b: set[str]) -> float:
    return len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0


__all__ = ["cycle", "maybe_cycle", "amend_text", "PolicyError"]
