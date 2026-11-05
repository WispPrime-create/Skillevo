"""Model-assisted stages, each with a deterministic fallback: judge, distill, draft, merge, review.

Roles follow the orchestration pattern: cheap workers (Haiku) triage and extract, a mid model (Sonnet) writes drafts and
settles unsure cases, and a strong model (Opus) is consulted only to gate consequential changes. Every stage treats
model output as untrusted: strict JSON, validation, injection screening, and for text that becomes a skill, the same
structural checks every skill must pass. Nothing here installs anything."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import candidates, redact, skills, tokenize
from .config import Config
from .orchestrate import Job, Orchestrator, wrap_data
from .store import Store


def _brief(s: skills.Skill, body_chars: int = 800) -> str:
    heads = " | ".join(re.findall(r"(?m)^#{1,3}\s+(.+)$", s.body)[:12])
    return (f"name: {s.name}\ndescription: {s.description[:500]}\nheadings: {heads[:400]}\n"
            f"start of body:\n{s.body[:body_chars]}")


# ---- judge -------------------------------------------------------------------------------------------------------
JUDGE_RULES = (
    "Two Claude Code skills look similar. Decide whether one makes the other redundant.\n"
    "- duplicate: same purpose and scope, so keeping both adds nothing (for example one is deprecated or superseded by the "
    "other, one is a strict subset of the other, or they are near-copies).\n"
    "- complementary: they cover different things even if they look alike (different purpose, scope, technology or audience).\n"
    "- unsure: you cannot tell from what is shown.\n"
    'Reply {"verdict": "duplicate" | "complementary" | "unsure", "reason": "<= 25 words", "keep": "A" | "B" | "either"} '
    "where keep names the skill that should survive when the verdict is duplicate (otherwise \"either\").")


@dataclass
class Judgement:
    duplicate: bool | None      # None = undecided, caller keeps its deterministic verdict
    source: str                 # model name that decided, or "none"
    reason: str = ""
    keep: str | None = None     # name of the skill that should survive a merge, when the model said which


def _judge_job(role: str, a: skills.Skill, b: skills.Skill) -> Job:
    prompt = JUDGE_RULES + "\n\n" + wrap_data("SKILL A", _brief(a)) + "\n\n" + wrap_data("SKILL B", _brief(b))
    return Job(role, prompt, {"verdict": str, "reason": str}, f"judge:{a.name}|{b.name}")


def _keep(res: dict, a: skills.Skill, b: skills.Skill) -> str | None:
    return {"A": a.name, "B": b.name}.get(str(res.get("keep", "")).strip().upper())


def judge_pairs(orch: Orchestrator, pairs: list[tuple[skills.Skill, skills.Skill]]) -> dict[tuple[str, str], Judgement]:
    out: dict[tuple[str, str], Judgement] = {}
    first = orch.ask_many([_judge_job("judge", a, b) for a, b in pairs])
    if orch.dry_run:  # undecided pairs would escalate, but how many is unknowable without the replies
        return out
    redo: list[int] = []
    for i, ((a, b), res) in enumerate(zip(pairs, first)):
        verdict = res.get("verdict") if res else None
        if verdict in ("duplicate", "complementary"):
            out[(a.name, b.name)] = Judgement(verdict == "duplicate", orch.model_for("judge"), str(res.get("reason", ""))[:200],
                                              _keep(res, a, b) if verdict == "duplicate" else None)
        else:
            redo.append(i)
    second = orch.ask_many([_judge_job("escalate", *pairs[i]) for i in redo])
    for i, res in zip(redo, second):
        a, b = pairs[i]
        verdict = res.get("verdict") if res else None
        if verdict in ("duplicate", "complementary"):
            out[(a.name, b.name)] = Judgement(verdict == "duplicate", orch.model_for("escalate"), str(res.get("reason", ""))[:200],
                                              _keep(res, a, b) if verdict == "duplicate" else None)
        else:
            out[(a.name, b.name)] = Judgement(None, "none")
    return out


# ---- distill (learn) ---------------------------------------------------------------------------------------------
DISTILL_RULES = (
    "Below are raw lessons recorded automatically from coding sessions. Rewrite them as short, general, reusable lessons. "
    "Merge near-duplicates (list every source id you used). Use ONLY facts present in the raw lessons; do not add advice "
    "that is not there. Drop noise by leaving its id out. Keep each body under 300 characters.\n"
    'Reply {"lessons": [{"source_ids": [int], "title": "<=80 chars", "body": "...", "tags": ["..."]}]}.')


def _content_tokens(text: str) -> set[str]:
    return {t for t in tokenize.tokens(text) if len(t) >= 4 or not t.isascii()}


def grounded(new_text: str, source_texts: list[str], minimum: float = 0.6) -> bool:
    """A distilled lesson must be made of words that appear in its sources, so the model cannot invent rules."""
    want = _content_tokens(new_text)
    have: set[str] = set()
    for s in source_texts:
        have |= _content_tokens(s)
    return not want or len(want & have) / len(want) >= minimum


def distill(orch: Orchestrator, store: Store, cfg: Config, project: str) -> dict[str, int]:
    raw = [r for r in store.list_experiences(project, "active", "lesson", limit=500) if r["source"] == "auto"]
    size = max(1, cfg.orchestration.distill_batch)
    batches = [raw[i:i + size] for i in range(0, len(raw), size)]
    jobs = []
    for batch in batches:
        listing = "\n".join(f"id={r['id']} | {r['title']} | {r['body']}" for r in batch)
        jobs.append(Job("extract", DISTILL_RULES + "\n\n" + wrap_data("RAW LESSONS", listing, 4000), {"lessons": list},
                        f"distill:{len(batch)}"))
    counts = {"batches": len(batches), "distilled": 0, "rejected": 0, "superseded": 0}
    for batch, res in zip(batches, orch.ask_many(jobs)):
        if not res:
            continue
        by_id = {r["id"]: r for r in batch}
        for item in res["lessons"]:
            try:
                ids = [int(i) for i in item["source_ids"]]
                title, body = redact.clean(str(item["title"]), 120), redact.clean(str(item["body"]), 600)
                tags = [str(t)[:30] for t in item.get("tags", [])][:6]
            except (KeyError, TypeError, ValueError):
                counts["rejected"] += 1
                continue
            sources = [by_id[i] for i in ids if i in by_id]
            if not sources or len(sources) != len(ids) or not title or not body \
                    or not grounded(title + " " + body, [s["title"] + " " + s["body"] for s in sources]) \
                    or redact.looks_like_injection(title + " " + body):
                counts["rejected"] += 1
                continue
            sessions = {x for s in sources for x in s["sessions"].split(",") if x}
            new_id, _ = store.add_experience(
                project, "lesson", title, body, tags, "llm-distilled", salience=max(s["salience"] for s in sources),
                confidence=0.5, scope=sources[0]["scope"], evidence=ids, sessions=sessions)
            for s in sources:
                if s["id"] != new_id:
                    store.supersede(s["id"], new_id)
                    counts["superseded"] += 1
            counts["distilled"] += 1
    return counts


# ---- drafting ----------------------------------------------------------------------------------------------------
def assemble(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {candidates.yaml_quote(description.strip())}\n---\n\n{body.strip()}\n"


def check_draft(text: str, name: str, max_bytes: int) -> str | None:
    """Return a reason the draft must not be used, or None. Applied to every model-written skill text."""
    errors, _ = skills.validate(text, name)
    if errors:
        return "invalid skill: " + "; ".join(errors)
    meta, _ = skills.parse(text, name)
    if str(meta.get("name")) != name:
        return "name changed"
    if len(text.encode("utf-8")) > max_bytes:
        return "over the size budget"
    if redact.looks_like_injection(text):
        return "contains instruction-like text"
    if redact.redact(text) != text:
        return "contains something that looks like a secret"
    return None


SKILL_RULES = (
    "Write a Claude Code skill that captures the lessons below. Use ONLY what the lessons say; do not add new advice. "
    "description must say when to use the skill (start with 'Use when'), under 300 characters. body is Markdown with a short "
    "intro and an actionable checklist or rules; no frontmatter, no code fences around the whole body.\n"
    'Reply {"description": "...", "body": "..."}.')


def draft_skill(orch: Orchestrator, rows: list, name: str, max_bytes: int) -> str | None:
    listing = "\n".join(f"- {r['title']}: {r['body']}" for r in rows)
    res = orch.ask(Job("draft", SKILL_RULES + "\n\n" + wrap_data("LESSONS", listing, 5000), {"description": str, "body": str},
                       f"draft-skill:{name}"))
    if not res:
        return None
    body = res["body"].strip() + "\n\n## Evidence\n" + "\n".join(f"- experience #{r['id']}" for r in rows)
    text = assemble(name, res["description"], body)
    return None if check_draft(text, name, max_bytes) else text


MERGE_RULES = (
    "Merge the secondary skills into the primary skill so a single skill covers all of them. Keep every distinct rule, "
    "instruction, code example and compatibility note; remove only repetition. Keep the primary skill's structure where "
    "possible. Complete every section: never end a code block or section half-way. description must say when to use the "
    "merged skill and mention what was absorbed.\n"
    'Reply {"description": "...", "body": "..."} where body is the merged Markdown without frontmatter.')
MAX_MERGE_INPUT = 30000   # characters per skill; larger skills are not sent (a truncated input yields an incomplete merge)


def draft_merge(orch: Orchestrator, primary: skills.Skill, others: list[skills.Skill], max_bytes: int,
                feedback: list[str] | None = None, previous: str | None = None) -> str | None:
    if any(len(s.text) > MAX_MERGE_INPUT for s in [primary, *others]):
        return None
    parts = [wrap_data("PRIMARY " + primary.name, primary.text, MAX_MERGE_INPUT)]
    parts += [wrap_data("SECONDARY " + o.name, o.text, MAX_MERGE_INPUT) for o in others]
    prompt = MERGE_RULES + "\n\n" + "\n\n".join(parts)
    if feedback and previous:
        prompt += ("\n\nA reviewer rejected your previous draft. Fix every issue below and return the complete corrected "
                   "merge.\n" + wrap_data("REVIEW ISSUES", "\n".join(f"- {i}" for i in feedback), 3000)
                   + "\n\n" + wrap_data("PREVIOUS DRAFT", previous, MAX_MERGE_INPUT))
    res = orch.ask(Job("draft", prompt, {"description": str, "body": str},
                       f"{'revise' if feedback else 'draft'}-merge:{primary.name}"))
    if not res:
        return None
    text = assemble(primary.name, res["description"], res["body"])
    if check_draft(text, primary.name, max_bytes):
        return None
    kept = _content_tokens(primary.description + " " + " ".join(re.findall(r"(?m)^#{1,3}\s+(.+)$", primary.body)))
    if kept and len(kept & _content_tokens(text)) / len(kept) < 0.6:
        return None  # dropped most of what the primary was about
    return text


# ---- review (strong model, consequential changes only) ----------------------------------------------------------
REVIEW_RULES = (
    "You are the final reviewer of a change to a Claude Code skill. Compare the DRAFT with the ORIGINALS. approve only if the "
    "draft keeps every distinct instruction from the originals (for a merge), is fully supported by the originals (for a "
    "new skill), or keeps the existing skill intact and adds only what the new lessons support (for an amendment: the first "
    "original is the current skill, the rest are new lessons). It must be clear and add nothing unsupported or unsafe. "
    "Otherwise revise (fixable) or reject.\n"
    'Reply {"verdict": "approve" | "revise" | "reject", "issues": ["..."]}.')


def review(orch: Orchestrator, kind: str, originals: list[str], draft: str) -> dict | None:
    parts = [wrap_data(f"ORIGINAL {i + 1}", t, MAX_MERGE_INPUT) for i, t in enumerate(originals)] + [wrap_data("DRAFT", draft, MAX_MERGE_INPUT)]
    res = orch.ask(Job("review", f"Change type: {kind}\n" + REVIEW_RULES + "\n\n" + "\n\n".join(parts),
                       {"verdict": str, "issues": list}, f"review:{kind}"))
    if not res or res.get("verdict") not in ("approve", "revise", "reject"):
        return None
    res["model"] = orch.model_for("review")
    return res


def merge_with_review(orch: Orchestrator, primary: skills.Skill, others: list[skills.Skill], max_bytes: int,
                      max_revisions: int = 1) -> tuple[str | None, dict | None, int]:
    """Draft a merge, have the strong model review it, and give the drafter the reviewer's issues once.
    Returns (text, last_review, revisions_used). Text is the last draft; only a review verdict of 'approve' means it passed."""
    originals = [primary.text] + [o.text for o in others]
    text = draft_merge(orch, primary, others, max_bytes)
    if text is None:
        return None, None, 0
    rev = review(orch, "merge", originals, text)
    used = 0
    while rev and rev["verdict"] == "revise" and used < max_revisions:
        used += 1
        better = draft_merge(orch, primary, others, max_bytes, [str(i) for i in rev.get("issues", [])], text)
        if better is None:
            break
        text = better
        rev = review(orch, "merge", originals, text) or rev
    return text, rev, used
