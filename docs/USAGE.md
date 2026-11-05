# Usage

## Requirements

* Python 3.11 or newer (standard library only).
* Claude Code (for hooks and, optionally, `claude -p`).
* SQLite with FTS5 is preferred; `skillevo doctor` reports whether it is available and falls back if not.

## Install and enable

```bash
python -m pip install .
skillevo doctor
cd /path/to/your/project
skillevo install            # dry run
skillevo install --apply
```

`install` writes absolute interpreter paths into the hook commands (`python` is frequently absent or the wrong
one), merges with existing hooks, creates a timestamped backup, and removes any older SkillEvo entries first, so it
is idempotent. Scopes: `project-local` (default, `.claude/settings.local.json`), `project` (`.claude/settings.json`,
committable, but the hook contains this machine's absolute interpreter path, so it needs `--apply --yes`), `user` (`~/.claude/settings.json`; needs `--apply --yes`).

If you move or upgrade Python, or install into a virtualenv, re-run `skillevo install --apply`.
Claude Code holds project hooks back until you trust the folder.

## Configuration

Layers (later wins): defaults, user config, project config, environment.

| Platform | User config | Data directory |
|---|---|---|
| macOS | `~/.config/skillevo/config.toml` | `~/Library/Application Support/skillevo` |
| Linux | `$XDG_CONFIG_HOME/skillevo/config.toml` | `$XDG_DATA_HOME/skillevo` |
| Windows | `%APPDATA%\skillevo\config.toml` | `%LOCALAPPDATA%\skillevo` |

Project config: `<project>/.skillevo/config.toml`. Environment: `SKILLEVO_HOME`, `SKILLEVO_CONFIG`,
`SKILLEVO_POLICY`, `SKILLEVO_DISABLED`. `skillevo init` writes an example; [examples/config.toml](../examples/config.toml)
lists every key. `skillevo config` prints the effective values and paths.

## Day to day

1. Work normally. Hooks collect events; on `Stop` the detectors run.
2. Before a prompt, relevant notes are injected automatically (`skillevo search <words>` previews this).
3. `skillevo list` shows what was learned; `skillevo forget <id>` removes a note from retrieval;
   `skillevo add TITLE BODY [--global]` records one by hand.
4. `skillevo evolve` clusters experiences into skill candidates and evaluates them under your policy.
   `skillevo skills` lists them; `skillevo approve NAME` installs a promotable one; `skillevo reject NAME` discards it.
5. `skillevo versions NAME`, `skillevo rollback NAME [--to N]`, `skillevo retire NAME`, `skillevo audit`.

## Evaluating a skill

```bash
skillevo eval path/to/skill-or-SKILL.md --suite examples/evals.json
```

Suite format: `cases[].assertions[]` with `type` in `contains | not_contains | regex | max_chars` and
`target` `skill` (checked on the skill text, deterministic) or `output` (checked on model output, only used by live
runs). For managed skills, place a suite at `<skill>/evals/evals.json` and it is used when amendments are evaluated.

## Curating skills

```bash
skillevo curate ~/some/skills ./.claude/skills --out report --interests python docker
skillevo curate DIR --smart         # needs [llm] enabled = true; a cheap model judges near-duplicate pairs
```

Outputs `report.md`, `findings.json` and `merge-drafts/<primary>/SKILL.md`. Input folders are never modified.
Add `--include-user` to also read `~/.claude/skills`.

## Smart mode (optional, spends Claude Code usage)

```toml
[llm]
enabled = true            # master opt-in; nothing below runs without it
[orchestration]
background = false        # true: the session-start hook launches the smart cycle in a detached process
```

```bash
skillevo evolve --smart --dry-run     # list the model calls a cycle would start with; changes nothing
skillevo evolve --smart               # distil lessons, draft candidates, gate autonomous installs with a reviewer
skillevo curate DIR --smart [--draft-merges] [--dry-run]
```

Tune models and limits under `[orchestration]` (see [examples/config.toml](../examples/config.toml)). Every model call is
visible in `skillevo audit` (role, model, success, reported cost). Dry-run counts are a minimum: escalations, revisions
and reviews depend on earlier replies. Costs shown are estimates unless Claude Code reported them.

## Importing from ECC

```bash
skillevo import-ecc --instincts /path/to/instincts --skill-runs /path/to/skill-runs.jsonl
```

Instinct confidence is capped at 0.6 (foreign data is low trust) and injection-like text is quarantined.

## Uninstall

```bash
skillevo uninstall --apply [--scope user] [--purge-data]
python -m pip uninstall skillevo
```

Skills SkillEvo installed stay in your skills folder (they are normal skills); delete them by hand or with
`skillevo retire NAME`.
