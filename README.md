# SkillEvo

A local-first learning loop and skill lifecycle manager for [Claude Code](https://claude.com/claude-code).

SkillEvo watches how your sessions go, remembers what mattered, hands the relevant notes back to Claude when a
similar task comes up, and proposes (and, if you allow it, installs) skills built from your own experience.
It can also audit any collection of skills and tell you which look redundant, broken or oversized, with the
evidence for each call.

> **Status: alpha (0.1.0).** It works end to end and is tested, but only on one environment so far.
> See [Tested environments](#tested-environments) before relying on it.

## What it does

```
 Claude Code session
   │  hooks (stdin JSON)                              everything below is local, stdlib-only Python
   ▼
 collect ──► events ──► detectors ──► experiences (SQLite + FTS5/BM25) ──► retrieval ──► prompt context
              │            │               ▲                                 (UserPromptSubmit,
              │            │               │                                  ≤1.8 KB, 3 notes)
              │            ▼               │
              │     one-off lessons    import (optional: ECC instincts)
              │     repeated patterns
              ▼            │
        skill telemetry    ▼
        (tool + typed)  skill candidates ─► evaluate ─► policy gate ─► install (snapshot first) ─► rollback
                                 ▲                         review-only | approval-required | autonomous
                                 └── curator: keep / improve / merge / retire recommendations
```

| Capability | How |
|---|---|
| Automatic experience collection | Claude Code hooks: tool calls, tool failures, your prompts (redacted, truncated) |
| Local persistent storage | One SQLite file in your user data directory. Nothing leaves your machine |
| Task-aware retrieval | On each prompt: BM25 search over your stored notes, project-scoped, hard cap on injected text |
| One-time lessons and repeated patterns | Deterministic detectors: user corrections, failure→fix pairs, risky commands, recurring failures, repeated tool workflows |
| Skill candidates | Related experiences are clustered into a draft `SKILL.md` |
| Evaluation | Structural score + your assertion suites; optional opt-in live runs through your own `claude -p` |
| Merge / retire recommendations | Near-duplicate detection, structural checks, usage telemetry, optional interest profile. Every verdict lists its evidence |
| Versions and rollback | Snapshot before every change to an existing skill; `skillevo rollback` |
| Bounded token use | Retrieval cap, per-skill attempt cap; optional model calls capped per run, per day and by estimated budget (off by default) |
| Smarter learn / judge / merge | Optional tiered models (haiku → sonnet → opus), see [Smart mode](#smart-mode-tiered-models-opt-in) |

## Install

Requires Python 3.11+ and Claude Code. No third-party Python packages, no Node, no daemon.

```bash
git clone <this repository>
cd skillevo
python -m pip install .
skillevo doctor
```

Hooks are **not** installed automatically. Preview, then apply, in your project directory:

```bash
skillevo install            # dry run: shows exactly what would be written
skillevo install --apply    # writes .claude/settings.local.json (git-ignored by convention), backs up the old file
# The hook command contains this machine's absolute Python path, so the committable `--scope project` file needs --yes.
```

Remove everything again (your own hook entries are left untouched). Do this before uninstalling the package, because
the hooks call `python -m skillevo`:

```bash
skillevo uninstall --apply                 # remove the hook entries
skillevo uninstall --apply --purge-data    # same, and also delete the local database
python -m pip uninstall skillevo
```

Details, platform paths and troubleshooting: [docs/USAGE.md](docs/USAGE.md), [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Quick tour

```bash
skillevo status                              # counts, policy, tracked skills
skillevo add "Run migrations first" "Run db migrations before the test suite or tests hit a stale schema"
skillevo search how do I run the test suite  # what would be injected for this prompt
skillevo evolve                              # detect patterns, propose skills under your policy
skillevo skills                              # candidates and installed skills
skillevo approve <name>                      # install a promotable candidate (approval-required mode)
skillevo rollback <name>                     # undo
skillevo curate path/to/skills --out report  # audit any skills folder; never modifies it
```

## Automation policies

| Mode (`[policy] mode`) | What `skillevo evolve` may do |
|---|---|
| `review-only` (default) | Generate and evaluate candidates, record recommendations. Installs nothing |
| `approval-required` | Same, and queue promotable candidates for `skillevo approve` |
| `autonomous` | Install evaluated, promotable changes, **only** for skills SkillEvo created and you have not edited. A *new* skill is installed automatically only if its evidence comes from at least `min_pattern_sessions` (default 3) separate sessions; thinner candidates wait for `skillevo approve` |

An evolution cycle also runs by itself at session start, at most once per `auto_interval_hours` (default 24;
0 turns it off), under the same policy. With the default `review-only` policy it only stages and evaluates candidates.

Safety rules that hold in every mode:

* A skill without SkillEvo's ownership marker is user-owned and is never modified by automation. A name clash
  creates `<name>-evo` instead.
* If you edit a SkillEvo-managed skill, SkillEvo notices (content hash) and stops auto-editing it.
* Every change to an existing skill is snapshotted first. Every action is written to an audit log (`skillevo audit`).
* Amendments are bounded (`max_attempts`) and only promoted when the evaluation does not regress.

## Smart mode: tiered models (opt-in)

By default everything is deterministic and free. Turn on `[llm] enabled = true` and SkillEvo can use your own
`claude -p` the way an orchestrator would: cheap models for routine judgement, a stronger one only where it matters.

| Role (model, configurable) | Does what | Used by |
|---|---|---|
| extract (haiku) | Rewrites raw recorded lessons into short general ones; merges near-duplicates | `evolve --smart` (learn) |
| judge (haiku) → escalate (sonnet) | Decides whether two similar skills are duplicates or complementary, and which should survive; unsure cases go up one tier | `curate --smart` (judge) |
| draft (sonnet) | Writes skill drafts and merge drafts | `evolve --smart`, `curate --smart --draft-merges` (merge) |
| review (opus) | Gates consequential changes: merge drafts, and every autonomous install while models are on | lifecycle gate |

```bash
skillevo curate DIR --smart --dry-run      # shows the model calls it would make, runs none
skillevo curate DIR --smart                # cheap pair judging
skillevo curate DIR --smart --draft-merges # also draft + review merges (costs more)
skillevo evolve --smart                    # distil lessons, draft skills, review autonomous installs
```

Set `[orchestration] background = true` to run the smart cycle automatically: the session-start hook only starts a
detached process (it returns in about 0.2 s) and that process does the model work under a lock, then writes
`last_smart_run.json` in the data directory.

With models enabled but no `--smart` run (plain `skillevo evolve`, or the inline session-start cycle), the reviewer is
unavailable, so `autonomous` mode queues new skills for `skillevo approve` instead of installing them.

Guard rails: off by default; per-run, per-day, review-call and estimated-budget caps; every call is written to the audit log with
its role and model, and a reviewer approval only counts if that call really is in the log; calls are isolated from
your own Claude Code setup (no `CLAUDE.md`, hooks, tools or transcript, and SkillEvo's own hooks are disabled inside
them); replies must be strict JSON, are screened for injection, and any failure falls back to the deterministic
result; a distilled lesson is rejected unless its words come from the lessons it summarises (no invented rules).

What was measured (one machine, 2026-11-02, Claude Code 2.1.312):

* **Judging duplicates vs complementary skills**, 20 similar ECC pairs hand-labelled by a single labeler (an AI, not
  reviewed by a human; only 2 are true duplicates and both say "deprecated" in their text): TF-IDF alone 17/20 correct
  (1 false merge, 0 of 2 real duplicates found). Haiku scored 20/20 with two earlier rubric wordings and again with
  the prompt that ships (which also names the skill to keep: right for 2 of 2 duplicates). The first wording
  contained examples I had written after reading these pairs, so the later runs use a generic rubric. 20 calls cost
  about $0.005-0.01. The sample is tiny; treat it as a sanity check, not a benchmark
  (`python scripts/eval_judge.py corpus/ECC/skills`).
* **Merge drafting is the expensive, unfinished part.** Two full runs on the same two ECC merges cost $0.28 and $0.56
  in total (20 judge calls are about $0.01 of that; the second run hit the $0.50 budget cap and skipped one step). Opus asked
  for changes on all 3 merge drafts it reviewed (dropped frontmatter metadata, edits to a command table, a truncated section,
  lost compatibility notes), and one automatic revision round did not satisfy it. Drafts are written for a human to finish and are
  marked `NOT approved` in `review.json` unless the reviewer approved. This is why drafting is a separate flag.
* **Distillation and skill drafting are covered by unit tests with a fake model, not yet by a real-model quality
  evaluation.** Do not assume they improve retrieval until you have compared them on your own data.

## Curating any set of skills

`skillevo curate DIR...` works on any directory of `<name>/SKILL.md` skills (your own, third-party, plugin
bundles) and writes `report.md`, `findings.json` and merge drafts to a separate output folder.

Verdicts are `keep | improve | merge | retire | review`, each tagged with the evidence behind it:
`structural`, `overlap`, `related`, `usage`, `profile`. Rules worth knowing:

* A skill with no usage data is **unmeasured**, never "waste".
* Retirement needs two independent evidence sources (for example no recorded use **and** no overlap with the
  interests you configured), or a structurally empty skill.
* Similar is not the same as redundant. Near-duplicates become merge candidates; merely related skills are
  flagged for review. `--smart` (opt in) asks a cheap model to separate real duplicates from siblings.

Example, from running it against a fresh clone of ECC v2.2.3 (314 skills, about 764k tokens of skill text, no
usage data): 1 near-duplicate pair flagged for merge, 22 skills flagged as related, 33 flagged improve
(mostly over the size budget), the rest keep. Without usage data or a profile it cannot call any of ECC's
domain packs unnecessary for you; that needs your telemetry or `interests`. These counts come from a
single run on one machine and were not manually verified pair by pair.

## Privacy, cost, requirements

* **Storage and processing are local** (except opt-in smart mode, see below). The database, snapshots and logs live under your user data directory
  (`SKILLEVO_HOME` overrides it). SkillEvo itself has no telemetry, makes no network connections and needs no account.
* **One thing does leave your machine, by design:** notes retrieved for a prompt (which can include text from your
  earlier prompts and commands, after redaction) are added to that prompt and so are sent to the model provider like
  any other context. Use `store_prompts = false`, `skillevo forget <id>`, or `SKILLEVO_DISABLED=1` to limit that.
* **Redaction at write time.** Keys, tokens, auth headers, `password=` style assignments and private-key
  blocks are scrubbed before storage; payloads are truncated. See [docs/PRIVACY.md](docs/PRIVACY.md).
* **Injected notes are framed as data**, stripped of markup, and suspicious instruction-like text is
  quarantined instead of injected.
* **Cost.** Collection, retrieval, detection and evaluation are deterministic local code and cost no model
  usage. The only model calls are the opt-in smart-mode ones (`[llm] enabled = true`), which run your own `claude -p`
  and count against your Claude Code plan, capped per run and per day. No API key is required. Skill text sent to a
  model goes to your model provider, like any prompt.
* **Overhead measured on the dev machine:** retrieval over 5,000 stored notes about 8 ms (p95 9 ms); a full
  hook process (Python start-up included) about 140 ms. Reproduce with `python scripts/bench.py`.
* **Kill switch:** `SKILLEVO_DISABLED=1` makes every hook do nothing.

## Tested environments

| Environment | Status |
|---|---|
| macOS, Python 3.14, Claude Code CLI present | Test suite passes locally (see CHANGELOG) |
| Linux / Windows, Python 3.11–3.13 | **Not tested yet.** [CI](.github/workflows/ci.yml) is defined for ubuntu/macos/windows × 3.11–3.13 but has not run, because this code has not been pushed anywhere |

The hook contract was checked against the official Claude Code documentation on 2026-11-02. A few behaviours
could not be confirmed from the docs (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#known-unknowns)).
Unsupported: Python < 3.11. SQLite without FTS5 works through a slower fallback.

## Relationship to ECC

[ECC](https://github.com/affaan-m/ECC) (MIT) was the starting point for research: its instinct format, skill-run
records and snapshot-based versioning informed this design. SkillEvo does not depend on ECC, does not vendor it,
and does not require its 300+ skills. If you already use ECC you can import its instinct files and skill-run
log (`skillevo import-ecc`) as low-trust data. `scripts/fetch_ecc_corpus.py` fetches ECC at a pinned commit as an
optional test corpus. See [NOTICE](NOTICE).

## Limitations (read these)

* Evaluation of **new** skills is structural: they are built from your own recorded notes, so passing means the file is
  well-formed, well-described and covers its evidence, not that it will help. That is why autonomous installs need
  multi-session evidence and everything else needs your approval.
* Deterministic evaluation measures structure, description quality, size, evidence coverage and your own
  assertions. It does **not** prove a skill improves task outcomes; that needs opt-in live runs and your own test cases.
* Experiences learned without a model are plain, mechanically phrased notes (for example, a correction is stored
  as what you said plus the preceding action). They are useful for retrieval, less polished as skills.
* Typed `/skill-name` invocations do not fire tool hooks, so SkillEvo detects them from your prompt. A skill that
  Claude loads without the `Skill` tool is not counted.
* Retrieval is lexical (BM25), not semantic. Synonyms will not match. Chinese/Japanese/Korean text is indexed with
  character bigrams.
* The project-scoped experiences are keyed by the project directory path hash; moving a project folder starts a
  fresh memory unless you set `SKILLEVO_HOME`/copy the database.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -v
python scripts/fetch_ecc_corpus.py   # optional corpus
SKILLEVO_TEST_CORPUS=corpus/ECC/skills PYTHONPATH=src python -m unittest tests.test_curator.CorpusTests
```

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
