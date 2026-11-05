# Privacy and security

* **Storage and processing are local.** SkillEvo itself makes no network connection and has no telemetry. The only
  external program it can start is your own `claude` CLI, and only when you enable `[llm]` and use a `--smart` command or the background cycle.
* **Retrieved notes are sent to your model provider.** Each injected note becomes part of the prompt Claude Code
  sends, exactly like any other context, and can contain redacted text from your earlier prompts and commands.
  Control it with `skillevo forget <id>`, `[privacy] store_prompts = false` (also disables learning from your
  corrections), `[retrieval] max_items`/`max_chars`, or `SKILLEVO_DISABLED=1`.
* **What is stored:** tool name plus a short summary (command text, file name relative to the project, search
  pattern, URL host), whether it failed and a short error line, your prompts truncated to 300 characters, and the notes
  derived from them. Not stored: file contents, tool outputs, model responses.
* **Redaction happens before storage**, then truncation (so a secret cannot be cut in half and leak a prefix).
  Patterns: cloud/API keys, GitHub/Slack tokens, JWTs, `Authorization`/`Bearer` values, `password=`/`token=`/
  `api_key=` style assignments, `--password x` flags, URL credentials, PEM private keys. Add your own regexes under
  `[privacy] extra_redact_patterns`. Redaction is pattern-based and cannot catch every secret; set
  `store_prompts = false` if you paste sensitive material into prompts.
* **Prompt-injection hygiene.** Stored text is re-injected into future prompts, so it is untrusted input:
  injection-like phrases are quarantined (status `quarantined`, never retrieved or put in a skill), the injected block
  is labelled as data, and markup characters are replaced.
* **Isolation.** Each user has their own database under their own data directory. Project notes are keyed by a hash
  of the project path; `scope = global` notes (created with `skillevo add --global`) apply everywhere.
* **Your skills are protected.** Automation cannot change a skill it did not create, or one you edited.
* **Config files are not touched silently.** `skillevo install` is a dry run unless `--apply`; user-scope settings
  need `--yes`; backups are timestamped; uninstall removes only SkillEvo's entries.
* **Smart mode sends text to your model provider.** Only if you set `[llm] enabled = true`: lessons, skill text and
  review prompts are sent through your own `claude -p`, redacted and quoted as data. Calls run without your `CLAUDE.md`,
  hooks or tools and leave no transcript. Nothing is sent to anyone else.
* **Cost.** Core operation uses no model calls. Opt-in calls use your Claude Code login and are limited by
  per-run, per-day and estimated-budget caps; every call is recorded locally (`skillevo audit`).
* **Delete everything:** `skillevo uninstall --apply --purge-data`.

Report security issues as described in [SECURITY.md](../SECURITY.md).
