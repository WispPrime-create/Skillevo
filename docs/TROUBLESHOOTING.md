# Troubleshooting

Start with `skillevo doctor` and `skillevo status`.

| Symptom | Likely cause and fix |
|---|---|
| Nothing is ever injected | Hooks not installed (`doctor` warns), folder not trusted by Claude Code yet, no stored notes match (try `skillevo search <words>`), or `SKILLEVO_DISABLED` is set. Matching needs at least 2 shared words; very short prompts match less |
| `No module named skillevo` shown by a hook | The interpreter written into the hook no longer has SkillEvo (upgrade, venv removed). Re-run `skillevo install --apply` with the right Python, or `skillevo uninstall --apply` |
| Hook errors or slowness | Errors are logged to `skillevo.log` in the data directory (`skillevo config` shows the path). Hooks always exit 0 and give up after 5 s |
| `FTS5` shows FAIL/false in `doctor` | Your SQLite build lacks FTS5. SkillEvo falls back to a slower scan; fine for thousands of notes |
| Typed `/skill` usage not counted | It is detected from the prompt text; skills invoked in other ways are not visible to hooks |
| "refusing to overwrite" on approve | A user-owned skill, or one you edited, has that name. Use a different name, or `--allow-unmanaged` after reading the diff. A snapshot is taken first |
| Candidate "not promotable" | See `skillevo skills` and `skillevo audit` for the evaluation reason (validation errors, regression, or no improvement) |
| `--smart` refused or nothing happens | `[llm] enabled` is false, `claude` is not on PATH, or a cap was reached (`skillevo audit` shows each call; the summary line says how many were skipped by caps) |
| Smart cycle never seems to run in the background | Needs `[llm] enabled = true` and `[orchestration] background = true`, a trusted folder, and a due cycle (`auto_interval_hours`). Result: `last_smart_run.json` in the data directory; a stuck run clears after 30 minutes (`smart.lock`) |
| Merge draft marked `NOT approved` | The reviewer asked for changes; read `review.json` next to the draft. Drafts are starting points, not finished merges |
| Settings file reported as corrupt | `install` refuses to overwrite invalid JSON. Fix or move the file, then retry |
| Memory vanished after moving a project | Notes are keyed by the project path hash. Copy the database or keep the path |
| Windows or Linux problems | These platforms are not yet tested. Please open an issue with `skillevo doctor` output |
