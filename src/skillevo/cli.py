"""Command line interface. Run `skillevo --help`."""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from . import (__version__, candidates, config, curator, detect, evalharness, evolve, importers, install, orchestrate, paths,
               redact, retrieve, skills, smart, versioning)
from .lifecycle import Lifecycle, PolicyError
from .store import Store

EXAMPLE_CONFIG = """# SkillEvo configuration. Every key is optional; defaults are shown.
[policy]
mode = "review-only"        # review-only | approval-required | autonomous

[retrieval]
max_chars = 1800            # hard cap on text injected per prompt
max_items = 3

[llm]
enabled = false             # opt in to `claude -p` calls (spends your Claude Code usage)
max_calls_per_day = 5

[skills]
managed_dir = ".claude/skills"   # where SkillEvo-created skills are installed (relative to the project)

[curator]
interests = []              # e.g. ["python", "docker"]: enables profile-based evidence
"""


def _ctx():
    root = paths.project_root()
    cfg = config.load(root)
    redact.set_extra_patterns(cfg.privacy.extra_redact_patterns)
    return root, cfg, paths.project_id(root)


def _open() -> Store:
    return Store(paths.db_path())


def cmd_hook(a) -> int:
    from . import hook
    return hook.main(a.event)


def cmd_install(a) -> int:
    root, _, _ = _ctx()
    if a.apply and not a.yes and a.scope == "user":
        print("Refusing: user scope changes your global Claude Code settings. Re-run with --yes to confirm.", file=sys.stderr)
        return 2
    if a.apply and not a.yes and a.scope == "project":
        print("Refusing: .claude/settings.json is meant to be committed, but the hook command contains this machine's absolute "
              "interpreter path, which will not work for teammates. Prefer the default project-local scope, or re-run with --yes.",
              file=sys.stderr)
        return 2
    path, diff, backup = install.install(root, a.scope, a.apply, a.python)
    print(f"{'Wrote' if a.apply else 'Dry run. Would write'} hooks to {path}\n{diff}")
    if backup:
        print(f"Backup of the previous file: {backup}")
    if not a.apply:
        print("\nNothing was changed. Re-run with --apply to install.")
    return 0


def cmd_uninstall(a) -> int:
    root, _, _ = _ctx()
    path, count, backup = install.uninstall(root, a.scope, a.apply)
    print(f"{path}: {count} SkillEvo hook entr{'y' if count == 1 else 'ies'} {'removed' if a.apply else 'found (dry run)'}")
    if backup:
        print(f"Backup: {backup}")
    if a.purge_data and a.apply:
        shutil.rmtree(paths.data_home(), ignore_errors=True)
        print(f"Deleted data directory {paths.data_home()}")
    elif a.purge_data:
        print(f"Dry run: would delete data directory {paths.data_home()}")
    return 0


def cmd_init(a) -> int:
    root, _, _ = _ctx()
    target = paths.project_config_path(root) if a.scope == "project" else paths.user_config_path()
    if target.exists():
        print(f"{target} already exists; not overwriting.")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"Wrote example config to {target}")
    return 0


def cmd_status(a) -> int:
    root, cfg, project = _ctx()
    with _open() as s:
        st = s.stats()
        print(f"SkillEvo {__version__}  project {project}  policy {cfg.policy.mode}")
        print(f"data: {paths.data_home()}")
        print(f"hooks installed (project-local): {install.installed(root)}")
        for k, v in st.items():
            print(f"  {k}: {v}")
        r = s.db.execute("SELECT COUNT(*) n, AVG(latency_ms) l, AVG(injected_chars) c FROM retrievals").fetchone()
        if r["n"]:
            print(f"  retrieval: {r['n']} runs, avg {r['l']:.1f} ms, avg {r['c']:.0f} chars injected")
        for row in s.list_skill_states():
            print(f"  skill {row['name']}: {row['state']} (attempts {row['attempts']})")
    return 0


def cmd_doctor(a) -> int:
    root, cfg, _ = _ctx()
    ok = True

    def line(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"[{'ok' if good else 'FAIL'}] {label} {detail}".rstrip())

    line("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    line("sqlite", True, sqlite3.sqlite_version)
    try:
        with _open() as s:
            print(f"[{'ok' if s.has_fts else 'warn'}] FTS5 full-text search" + ("" if s.has_fts else " unavailable (using the slower portable fallback)"))
    except Exception as exc:  # noqa: BLE001
        line("database writable", False, str(exc))
    hooked = install.installed(root)
    print(f"[{'ok' if hooked else 'warn'}] hooks installed (project-local)" + ("" if hooked else "  -> run: skillevo install --apply"))
    print(f"[info] claude on PATH: {bool(shutil.which(cfg.llm.claude_bin))}; model calls enabled: {cfg.llm.enabled}")
    print(f"[info] kill switch SKILLEVO_DISABLED: {config.disabled()}")
    return 0 if ok else 1


def cmd_search(a) -> int:
    _, cfg, project = _ctx()
    with _open() as s:
        rows = retrieve.select(s, cfg, project, " ".join(a.query))
        if not rows:
            print("no relevant experiences (nothing would be injected)")
        for r in rows:
            print(f"#{r['id']} [{r['kind']}] {r['title']}\n    {r['body'][:200]}")
    return 0


def cmd_add(a) -> int:
    _, _, project = _ctx()
    with _open() as s:
        i, created = s.add_experience(project, "lesson", redact.clean(a.title, 120), redact.clean(a.body, 600),
                                      source="manual", salience=a.salience, confidence=0.8,
                                      scope="global" if a.is_global else "project",
                                      status="quarantined" if redact.looks_like_injection(a.title + a.body) else "active")
        print(f"{'added' if created else 'already known'}: #{i}")
    return 0


def cmd_list(a) -> int:
    _, _, project = _ctx()
    with _open() as s:
        for r in s.list_experiences(project, None if a.all else "active", a.kind, limit=a.limit):
            print(f"#{r['id']} {r['status']:<11} {r['kind']:<8} sal {r['salience']:.2f} conf {r['confidence']:.2f} seen {r['seen']}  {r['title'][:80]}")
    return 0


def cmd_forget(a) -> int:
    with _open() as s:
        s.set_experience_status(a.id, "retired")
        s.audit("user", "forget", f"#{a.id}")
    print(f"experience #{a.id} will no longer be retrieved")
    return 0


def cmd_learn(a) -> int:
    _, cfg, project = _ctx()
    with _open() as s:
        print(json.dumps(detect.learn(s, cfg, project, since_id=0 if a.rescan else None)))
    return 0


def cmd_evolve(a) -> int:
    root, cfg, project = _ctx()
    if a.policy:
        cfg.policy.mode = a.policy
    if a.smart and not cfg.llm.enabled:
        print("--smart uses your own `claude -p` and spends Claude Code usage. Opt in with [llm] enabled = true in the config.",
              file=sys.stderr)
        return 1
    if a.background and not orchestrate.acquire_lock():
        return 0  # another background run is active
    try:
        with _open() as s:
            detect.learn(s, cfg, project)
            orch = orchestrate.Orchestrator(s, cfg, dry_run=a.dry_run) if a.smart else None
            if a.dry_run:
                if not orch:
                    print("--dry-run needs --smart (a deterministic cycle makes no model calls)")
                    return 1
                print(evolve.plan_smart(s, cfg, project, orch))
                print("model calls this cycle would start with:", orch.describe(),
                      "\n(a minimum: reviews and retries depend on earlier replies; nothing was staged or installed)")
                return 0
            rep = evolve.cycle(s, cfg, root, project, orch)
            if a.background:
                (paths.data_home() / "last_smart_run.json").write_text(json.dumps({"ts": time.time(), "report": rep,
                                                                                    "calls": orch.describe() if orch else ""}, default=str))
    finally:
        if a.background:
            orchestrate.release_lock()
    print(f"policy: {cfg.policy.mode}")
    if rep.get("distilled"):
        print("  distilled:", rep["distilled"])
    for section in ("candidates", "amendments", "skipped"):
        for name, res in rep[section].items():
            print(f"  {section[:-1] if section != 'skipped' else 'skipped'} {name}: {res}")
    if not any(rep[k] for k in ("candidates", "amendments", "skipped")):
        print("  nothing to do")
    if orch:
        print("model calls:", orch.describe())
    return 0


def _lc(s: Store):
    root, cfg, project = _ctx()
    return Lifecycle(s, cfg, root, project)


def cmd_approve(a) -> int:
    with _open() as s:
        lc = _lc(s)
        try:
            if lc.meta(a.name).get("promotable") is None:
                lc.evaluate(a.name, "user")
            dest = lc.install(a.name, "user", allow_unmanaged=a.allow_unmanaged)
        except PolicyError as exc:
            print(f"cannot approve: {exc}", file=sys.stderr)
            return 1
    print(f"installed {dest}")
    return 0


def cmd_reject(a) -> int:
    with _open() as s:
        _lc(s).reject(a.name, "user")
    print(f"rejected {a.name}")
    return 0


def cmd_retire(a) -> int:
    with _open() as s:
        try:
            _lc(s).retire(a.name, "user", a.allow_unmanaged)
        except PolicyError as exc:
            print(f"cannot retire: {exc}", file=sys.stderr)
            return 1
    print(f"retired {a.name} (snapshot kept; `skillevo rollback {a.name}` restores it)")
    return 0


def cmd_rollback(a) -> int:
    with _open() as s:
        try:
            v = _lc(s).rollback(a.name, "user", a.to)
        except (LookupError, PolicyError) as exc:
            print(f"cannot roll back: {exc}", file=sys.stderr)
            return 1
    print(f"restored {a.name} to v{v}")
    return 0


def cmd_versions(a) -> int:
    for v in versioning.list_versions(a.name):
        print(f"v{v['version']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(v['ts']))}  {v['reason']}")
    return 0


def cmd_skills(a) -> int:
    with _open() as s:
        lc = _lc(s)
        for row in s.list_skill_states():
            meta = json.loads(row["meta"])
            pend = " [pending: " + meta["pending"]["kind"] + (", promotable]" if meta.get("promotable") else ", not promotable]") if "pending" in meta else ""
            print(f"{row['name']:<40} {row['state']:<10} {skills.ownership(lc.target_dir(row['name'])):<9}{pend}")
    return 0


def cmd_audit(a) -> int:
    with _open() as s:
        for r in s.audit_log(a.limit):
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts']))}  {r['actor']:<18} {r['action']:<24} {r['target'] or ''}")
    return 0


def cmd_eval(a) -> int:
    _, cfg, _ = _ctx()
    path = Path(a.path)
    f = path / "SKILL.md" if path.is_dir() else path
    suite = evalharness.load_suite(a.suite) if a.suite else None
    res = evalharness.evaluate(f.read_text(encoding="utf-8"), suite, None, cfg.curator.max_skill_bytes, path.name if path.is_dir() else "")
    print(json.dumps(res.to_json(), indent=2))
    print("Note: this is a structural/assertion score, not proof of improved task outcomes.")
    return 0 if not res.errors else 1


def cmd_curate(a) -> int:
    root, cfg, _ = _ctx()
    dirs = [Path(d) for d in a.dirs] or curator.scan_dirs(cfg.skills.scan_dirs, root)
    if a.include_user:
        dirs.append(Path.home() / ".claude" / "skills")
    items = skills.discover(dirs)
    if not items:
        print("no skills found in: " + ", ".join(str(d) for d in dirs))
        return 1
    if a.threshold:
        cfg.curator.overlap_threshold = a.threshold
    usage, span = None, 0.0
    if not a.no_telemetry:
        with _open() as s:
            usage, span = s.skill_usage(), s.telemetry_span_days()
    orch = None
    judge = None
    if a.smart:
        if not cfg.llm.enabled:
            print("--smart uses your own `claude -p` and spends Claude Code usage. Opt in with [llm] enabled = true in the config.",
                  file=sys.stderr)
            return 1
        with _open() as s:
            orch = orchestrate.Orchestrator(s, cfg, dry_run=a.dry_run)
            pair_idx = curator.overlap_pairs(items, cfg.curator.related_threshold)
            table = smart.judge_pairs(orch, [(items[i], items[j]) for i, j, _ in pair_idx])
            if a.dry_run:
                print(f"{len(pair_idx)} similar pairs to judge; model calls planned:", orch.describe(),
                      "\n(judging only; --draft-merges adds a draft, a review and possibly a revision per confirmed duplicate pair. Nothing was written)")
                return 0

            def judge(x, y):
                j = table.get((x.name, y.name))
                return curator.PairVerdict(j.duplicate, j.reason, j.keep, j.source) if j else None
            findings = curator.analyze(items, cfg, usage, span, a.interests, judge)
            by_name = {s_.name: s_ for s_ in items}
            return _write_curation(a, cfg, items, findings, span, dirs, orch, by_name)
    findings = curator.analyze(items, cfg, usage, span, a.interests)
    return _write_curation(a, cfg, items, findings, span, dirs, None, {s_.name: s_ for s_ in items})


def _write_curation(a, cfg, items, findings, span, dirs, orch, by_name) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    label = ", ".join(d.name or str(d) for d in dirs)
    report = curator.report_markdown(findings, label, span)
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "findings.json").write_text(json.dumps([dataclasses.asdict(f) for f in findings], indent=2), encoding="utf-8")
    merged = 0
    targets: dict[str, list[skills.Skill]] = {}
    for f in findings:
        if f.merge_into:
            targets.setdefault(f.merge_into, []).append(by_name[f.name])
    for primary, others in targets.items():
        d = out / "merge-drafts" / primary
        d.mkdir(parents=True, exist_ok=True)
        text, source, review = None, "deterministic", None
        if orch is not None and a.draft_merges:
            text, review, revisions = smart.merge_with_review(orch, by_name[primary], others, cfg.curator.max_skill_bytes)
            if text:
                source = f"model:{orch.model_for('draft')} ({revisions} revision(s))"
        if text is None:
            text = curator.draft_merge(by_name[primary], others, cfg.curator.max_skill_bytes)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        (d / "review.json").write_text(json.dumps({"draft_source": source, "absorbs": [o.name for o in others],
                                                   "review": review,
                                                   "status": "approved by reviewer" if review and review.get("verdict") == "approve"
                                                   else "NOT approved: read review issues before using"}, indent=2, default=str), encoding="utf-8")
        merged += 1
    counts = {}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    print(f"{len(items)} skills analysed: {counts}; {merged} merge draft(s). Report: {out / 'report.md'}")
    if orch:
        print("model calls:", orch.describe())
    print("Nothing in the analysed skill folders was modified.")
    return 0


def cmd_import_ecc(a) -> int:
    _, _, project = _ctx()
    with _open() as s:
        if a.instincts:
            print("instincts:", importers.import_instincts(s, project, Path(a.instincts)))
        if a.skill_runs:
            print("skill runs:", importers.import_skill_runs(s, project, Path(a.skill_runs)))
    return 0


def cmd_prune(a) -> int:
    _, cfg, _ = _ctx()
    with _open() as s:
        print(s.prune(cfg.collection.retention_days, cfg.collection.max_events_per_project))
    return 0


def cmd_config(a) -> int:
    root, cfg, _ = _ctx()
    print(json.dumps({"effective": dataclasses.asdict(cfg), "user_config": str(paths.user_config_path()),
                      "project_config": str(paths.project_config_path(root)), "data_home": str(paths.data_home())}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skillevo", description="Local-first learning and skill lifecycle manager for Claude Code.")
    p.add_argument("--version", action="version", version=f"skillevo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("hook", cmd_hook, "internal: Claude Code hook entry point")
    sp.add_argument("event")
    for name, fn in (("install", cmd_install), ("uninstall", cmd_uninstall)):
        sp = add(name, fn, f"{name} hooks (dry run unless --apply)")
        sp.add_argument("--scope", choices=["project-local", "project", "user"], default="project-local")
        sp.add_argument("--apply", action="store_true")
        if name == "install":
            sp.add_argument("--python", help="interpreter to write into the hook command (default: this one)")
            sp.add_argument("--yes", action="store_true", help="confirm user-scope changes")
        else:
            sp.add_argument("--purge-data", action="store_true", help="also delete the local experience database")
    sp = add("init", cmd_init, "write an example config file")
    sp.add_argument("--scope", choices=["user", "project"], default="project")
    add("status", cmd_status, "show counts, policy and tracked skills")
    add("doctor", cmd_doctor, "check the environment")
    sp = add("search", cmd_search, "show what would be retrieved for a prompt")
    sp.add_argument("query", nargs="+")
    sp = add("add", cmd_add, "record a lesson manually")
    sp.add_argument("title")
    sp.add_argument("body")
    sp.add_argument("--salience", type=float, default=0.7)
    sp.add_argument("--global", dest="is_global", action="store_true")
    sp = add("list", cmd_list, "list stored experiences")
    sp.add_argument("--kind")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--limit", type=int, default=50)
    sp = add("forget", cmd_forget, "stop retrieving an experience")
    sp.add_argument("id", type=int)
    sp = add("learn", cmd_learn, "run the pattern/lesson detectors now")
    sp.add_argument("--rescan", action="store_true", help="reprocess all recorded events")
    sp = add("evolve", cmd_evolve, "run one evolution cycle under the configured policy")
    sp.add_argument("--policy", choices=list(config.POLICY_MODES))
    sp.add_argument("--smart", action="store_true", help="opt in: tiered models distil lessons, draft skills and review installs (needs [llm] enabled)")
    sp.add_argument("--dry-run", action="store_true", help="with --smart: show the model calls that would be made, then stop")
    sp.add_argument("--background", action="store_true", help="internal: run as the detached process started by the session-start hook")
    add("skills", cmd_skills, "list SkillEvo-tracked skills")
    for name, fn in (("approve", cmd_approve), ("reject", cmd_reject), ("retire", cmd_retire)):
        sp = add(name, fn, f"{name} a skill")
        sp.add_argument("name")
        if name != "reject":
            sp.add_argument("--allow-unmanaged", action="store_true", help="allow acting on a skill SkillEvo does not own")
    sp = add("rollback", cmd_rollback, "restore a previous version of a skill")
    sp.add_argument("name")
    sp.add_argument("--to", type=int)
    sp = add("versions", cmd_versions, "list saved versions of a skill")
    sp.add_argument("name")
    sp = add("audit", cmd_audit, "show the audit log")
    sp.add_argument("--limit", type=int, default=30)
    sp = add("eval", cmd_eval, "score a skill file or directory")
    sp.add_argument("path")
    sp.add_argument("--suite")
    sp = add("curate", cmd_curate, "recommend keep/improve/merge/retire for any set of skills")
    sp.add_argument("dirs", nargs="*", help="skill directories (default: configured scan_dirs)")
    sp.add_argument("--out", default="skillevo-report")
    sp.add_argument("--interests", nargs="*")
    sp.add_argument("--include-user", action="store_true", help="also scan ~/.claude/skills (read-only)")
    sp.add_argument("--threshold", type=float)
    sp.add_argument("--no-telemetry", action="store_true")
    sp.add_argument("--smart", action="store_true", help="opt in: a cheap model judges near-duplicate pairs (needs [llm] enabled)")
    sp.add_argument("--draft-merges", action="store_true", help="with --smart: also have a mid model draft each merge and a strong model review it (costs roughly $0.1-0.3 per merge; a deterministic draft is written otherwise)")
    sp.add_argument("--dry-run", action="store_true", help="with --smart: show the model calls that would be made, then stop")
    sp = add("import-ecc", cmd_import_ecc, "import ECC instinct files / skill-runs.jsonl")
    sp.add_argument("--instincts")
    sp.add_argument("--skill-runs")
    add("prune", cmd_prune, "apply retention limits")
    add("config", cmd_config, "print effective configuration and paths")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "hook":
        # Hooks bypass argparse and can never fail: exit code 2 would block the user's prompt.
        try:
            from . import hook
            return hook.main(argv[1] if len(argv) > 1 else "")
        except BaseException:  # noqa: BLE001
            return 0
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"skillevo: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
