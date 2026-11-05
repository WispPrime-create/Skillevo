# Architecture

SkillEvo is a small, dependency-free Python package. There is no daemon: Claude Code starts a short-lived
process per hook event, and everything else is an explicit CLI command.

## Modules

| Module | Responsibility |
|---|---|
| `hook.py` | Hook entry point. Always exits 0, swallows and logs every error, hard 5 s watchdog, kill switch checked first |
| `collect.py` | Converts hook payloads into redacted, truncated events; detects typed `/skill` use and user corrections |
| `redact.py` | Secret redaction on write; injection detection; sanitising before re-injection |
| `store.py` | SQLite schema: events, experiences (+ FTS5 index), retrievals, skill telemetry, skill state, audit, model-call counter |
| `tokenize.py` | Latin words, snake_case parts, CJK character bigrams; builds safe FTS5 queries |
| `retrieve.py` | BM25 candidate pool → term-coverage filter → character-budgeted, data-framed output |
| `detect.py` | Deterministic detectors: corrections, failure→fix pairs, risky commands, recurring failures, repeated workflows; salience score |
| `candidates.py` | Clusters experiences and drafts `SKILL.md` candidates from a template |
| `evalharness.py` | Structural + assertion scoring, candidate-vs-current comparison with regression guard, opt-in live runner |
| `lifecycle.py` | State machine, policy gates, install/retire/rollback, ownership rules, audit |
| `versioning.py` | Snapshots outside the skill directory; rollback is itself snapshotted |
| `evolve.py` | One bounded evolution cycle (amend managed skills, then propose new ones) |
| `curator.py` | Evidence-tagged keep/improve/merge/retire analysis for any skill directory; merge drafts |
| `importers.py` | Read-only importers for ECC instinct files and skill-run logs |
| `install.py` | Merge/remove hook entries in a settings file with dry run, backup and exact uninstall |
| `orchestrate.py` | Tiered-model runner: isolation flags, caps, budget, strict JSON, audit, background spawn and lock |
| `smart.py` | Model-assisted stages: judge, distil, draft skill, draft merge (+ revision loop), review; each with a deterministic fallback |
| `skills.py`, `frontmatter.py` | Skill discovery, validation, ownership marker; a small YAML-subset parser |

## Data

All data lives under `data_home()`: `SKILLEVO_HOME`, else `~/Library/Application Support/skillevo` (macOS),
`$XDG_DATA_HOME/skillevo` or `~/.local/share/skillevo` (Linux), `%LOCALAPPDATA%\skillevo` (Windows).
Contents: `skillevo.db`, `versions/<skill>/vN/`, `candidates/<project>/<skill>/`, `skillevo.log`.
Project rows are keyed by a SHA-256 prefix of the project path; raw paths are not stored. Tool-call summaries
keep file paths relative to the project (or just the file name).

An experience has: kind (`lesson | pattern | workflow`), title, body, tags, salience (impact if forgotten),
confidence (how well supported), scope (`project | global`), status (`active | quarantined | retired`),
a fingerprint for de-duplication, and evidence (event ids).

## Retrieval

1. Tokenize the prompt (stop-words removed; CJK as bigrams).
2. Build an FTS5 expression with **every term quoted and OR-joined**. Raw prompt text is never passed to `MATCH`.
3. Take a pool of 20 by `bm25()` (lower is better), restricted to the current project plus global notes.
4. Keep rows that share at least `min_terms` words with the prompt (fixes BM25 weakness on tiny corpora and
   makes "no relevant note" produce no injection).
5. Order by coverage, BM25, salience; keep at most `max_items`; render whole items until `max_chars` is reached.
6. Output is `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ...}}`, framed as
   reference data, with markup characters replaced.

## Learning

* **One-off lessons** do not need repetition. Salience is `0.3 + 0.1 * failures_before_fix + 0.4 if the user
  corrected + 0.3 if the command looks irreversible` (capped at 1). A user correction, or a failure followed by a
  fix, becomes a lesson immediately.
* **Patterns** need `min_pattern_sessions` separate sessions: the same normalised failure signature, or the same
  3–4 step tool sequence (read-only sequences are ignored).
* No model is involved. Lessons are phrased mechanically from what was observed.

## Skill lifecycle

```
candidate ──evaluate──► evaluated ──install──► active ──retire──► retired
    │                       │                    │  ▲                │
    └──► rejected ◄─────────┘                    │  └── rollback ────┘
                                    amendment: stage → evaluate → (policy) → install (snapshot first)
```

Autonomy rules (all in `lifecycle.apply_policy`): nothing is installed in `review-only`; `approval-required` queues;
`autonomous` installs only managed, unedited skills, and a *new* skill only when its evidence spans at least
`min_pattern_sessions` distinct sessions (tracked per experience in `experiences.sessions`). Confidence and `seen`
on an experience grow only when a new session corroborates it; re-running a detector over the same events changes
nothing. An evolution cycle runs from the `SessionStart` hook at most every `auto_interval_hours`.

Policy gate (`lifecycle.apply_policy`) and ownership checks (`lifecycle.can_auto_modify`) are enforced inside
the lifecycle module, not in the CLI, so every caller gets the same protection.

## Smart mode

`orchestrate.Orchestrator` is the only code that calls a model. A call is: `claude -p --model M --tools "" --no-session-persistence
--disable-slash-commands --setting-sources "" --system-prompt ... --output-format json --max-turns 1`, prompt on stdin, run
from a neutral directory with `SKILLEVO_DISABLED=1`. `--setting-sources ""` matters: without it, a canary test showed the
user's own `CLAUDE.md` and global hook text reached the model (about 7,200 input tokens instead of about 1,000), which would
make every user's personal instructions shape SkillEvo's judgements. `--bare` is deliberately not used because it
disables OAuth/keychain login, which would break subscription users.

Independent jobs run at most `max_parallel` at a time; bookkeeping (caps, SQLite writes, audit) happens on the calling
thread. Replies go through `extract_json` (first JSON object in the text), a key/type check and an injection screen; one
retry with a stricter instruction, then `None` and the caller's deterministic fallback. Untrusted text is wrapped in
`<<<BEGIN ...>>>` markers after redaction, and the system prompt says to treat it as data.

Pipeline when `evolve --smart` runs: distil raw lessons (haiku, grounded against their sources, raw rows kept and marked
`superseded`) → cluster → draft each candidate (sonnet, validated like any skill, template fallback) → evaluate →
policy; under `autonomous`, a new skill additionally needs an `approve` verdict from the review model, recorded with the id of
the logged call. `curate --smart`: judge each similar pair (haiku; unsure → sonnet), let the judged verdict override the
TF-IDF tier and choose the survivor, and with `--draft-merges` draft and review the merges.

Automation: with `[orchestration] background = true`, the `SessionStart` hook (throttled to `auto_interval_hours`) only
spawns `skillevo evolve --smart --background` detached (stdio to devnull, new session) and returns. The child takes a lock
file (pid + timestamp, stale after 30 minutes), so runs cannot overlap. Windows detach flags are set but untested.

## Evaluation honesty

`evalharness.evaluate` scores validity, description quality (does it say *when* to use the skill), size budget,
evidence coverage (do the skill's terms include what the evidence says) and assertion pass-rate on skill text.
A candidate is promoted only if it is valid, does not regress any assertion that previously passed, and is better
than the current version by `min_improvement` (new skills: score ≥ 0.7 and no failing assertion).
This is a quality gate, not an outcome measurement. `run_live` grades *output* assertions with and without the
skill through a caller-supplied runner; it is library-only (no CLI path yet) and only unit-tested with a fake runner.

## Hook contract

Checked against the official documentation on 2026-11-02 (hooks reference and guide at code.claude.com/docs/en/):

* Payload on stdin as JSON with `session_id`, `cwd`, `hook_event_name`; `prompt` for `UserPromptSubmit`;
  `tool_name`, `tool_input` for tool events; `error` for `PostToolUseFailure`; `stop_hook_active` for `Stop`.
* Context injection: stdout JSON `hookSpecificOutput.additionalContext` (top-level placement is ignored).
* Exit 0 = proceed; **exit 2 blocks `UserPromptSubmit`**. SkillEvo always exits 0.
* `UserPromptSubmit` default timeout 30 s; SkillEvo writes `timeout: 10` and a 5 s internal watchdog.
* `PostToolUseFailure` is a separate event from `PostToolUse`.

### Verified against a real session

On 2026-11-02, with Claude Code 2.1.x and the Haiku model, in a throwaway project: a `UserPromptSubmit` hook returning
`additionalContext` did reach the model (it answered with a code word that existed only in the stored note);
`PostToolUseFailure` fired for a failing Bash command with `error` = `"Exit code 1\n<stderr line>"`; the `Skill` tool
call arrived as `PostToolUse` with `tool_input` `{"skill": "<name>"}`; `claude -p --output-format json` returns
`result`, `is_error`, `num_turns`, `total_cost_usd`. These shapes are covered by tests in `tests/test_hooks.py`.

### Known unknowns

Not confirmed, so handled defensively; re-check against your Claude Code version:

* Whether typed `/skill` fires `UserPromptSubmit` before or after `UserPromptExpansion` (SkillEvo reads the prompt text).
* The documented 10,000-character cap on injected context (SkillEvo stays far below it).
* Behaviour on Windows and Linux (never run there).
* When Claude issues parallel tool calls, hook ordering is not guaranteed, so failure→fix pairing can miss them.

## Design decisions

* **No vector search / no daemon / no network.** Keeps install trivial and behaviour auditable. BM25 over small,
  project-scoped notes is fast and predictable; the cost is no synonym matching.
* **Ownership marker + content hash** instead of a separate registry, so ownership travels with the skill folder.
* **Snapshots outside the skill dir** so retiring or replacing a skill cannot destroy its history.
* **Model calls never run inside a hook**: hooks have a few seconds; model work is an explicit command or a detached background process.
* **ECC is optional input, not a dependency**: its learning pipeline needs Node and a background observer that
  spends model quota and requires 3+ observations; SkillEvo needs neither.
