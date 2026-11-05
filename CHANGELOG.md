# Changelog

## 0.1.0 (unreleased)

Initial alpha.

* Hooks: collection (tool calls, failures, prompts), BM25 retrieval at `UserPromptSubmit`, detectors on `Stop`.
* SQLite/FTS5 experience store with write-time redaction, CJK bigram indexing, quarantine of injection-like text.
* Deterministic detectors for corrections, failure→fix pairs, risky commands, recurring failures, workflows.
* Skill candidates, evaluation harness, lifecycle state machine, policies (review-only / approval-required /
  autonomous), ownership markers, snapshots and rollback, audit log.
* Curator for arbitrary skill directories with evidence-tagged verdicts and merge drafts.
* ECC instinct / skill-run importers; pinned optional ECC corpus fetch script.
* Installer/uninstaller with dry run, backup and exact removal.
* Opt-in smart mode: tiered models (extract/judge/escalate/draft/review) through the user's own `claude -p`, with isolation
  from the user's Claude Code setup, per-run/per-day/budget caps, strict JSON, audit of every call, deterministic fallbacks,
  a reviewer gate for autonomous installs, a merge draft/review/revise loop, and a detached background cycle.
* Measured on one machine: judging 20 hand-labelled ECC pairs, TF-IDF 17/20 vs Haiku 20/20 (tiny sample, single labeler).
* Tested locally on macOS with Python 3.14 only. CI workflow defined but not yet run.
