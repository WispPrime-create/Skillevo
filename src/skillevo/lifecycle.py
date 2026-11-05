"""Skill lifecycle: candidate -> evaluated -> active -> retired (or rejected), with policy gates and an audit trail.

Safety rules enforced here, not in callers:
  * Automation (`apply_policy`) only ever installs a brand-new skill name or amends a skill whose ownership marker is
    intact (managed and not edited by the user since). A user-owned skill is never changed without `allow_unmanaged`.
  * Every change to an existing skill is preceded by a snapshot, so it can be rolled back.
  * `review-only` never installs; `approval-required` queues; `autonomous` installs evaluated, promotable changes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import evalharness, paths, skills, versioning
from .candidates import evidence_terms, parse_evidence
from .config import Config
from .store import Store

TRANSITIONS = {
    "candidate": {"evaluated", "rejected"},
    "evaluated": {"active", "rejected", "candidate"},
    "active": {"retired", "evaluated"},
    "retired": {"active"},
    "rejected": {"candidate"},
}


class PolicyError(Exception):
    pass


class Lifecycle:
    def __init__(self, store: Store, cfg: Config, root: Path, project: str):
        self.store, self.cfg, self.root, self.project = store, cfg, root, project
        # reviewer(name, staged_text, pending_meta) -> {'verdict', 'issues', 'model', '_call_id'} | None; set when models are enabled
        self.reviewer = None

    # ---- locations ----------------------------------------------------------------------------------------
    def target_root(self) -> Path:
        p = Path(self.cfg.skills.managed_dir).expanduser()
        return p if p.is_absolute() else self.root / p

    def target_dir(self, name: str) -> Path:
        return self.target_root() / name

    def staged_dir(self, name: str) -> Path:
        return paths.staging_dir() / self.project / name

    # ---- state ----------------------------------------------------------------------------------------------
    def state(self, name: str) -> str | None:
        row = self.store.get_skill_state(name)
        return row["state"] if row else None

    def meta(self, name: str) -> dict:
        row = self.store.get_skill_state(name)
        return json.loads(row["meta"]) if row else {}

    def _move(self, name: str, new: str, actor: str, **meta_update) -> None:
        cur = self.state(name)
        if cur is not None and new != cur and new not in TRANSITIONS.get(cur, set()):
            raise PolicyError(f"illegal transition {cur} -> {new} for {name}")
        meta = {**self.meta(name), **meta_update}
        self.store.set_skill_state(name, new, meta=meta)
        self.store.audit(actor, f"state:{cur or 'none'}->{new}", name, meta_update or None)

    # ---- staging --------------------------------------------------------------------------------------------
    def stage(self, name: str, text: str, evidence: list[int], kind: str = "new", actor: str = "skillevo",
              sessions: int = 0, drafted_by: str = "template") -> Path:
        """Write a candidate (new skill or amendment) to the staging area. Never touches the live skills folder."""
        d = self.staged_dir(name)
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        row = self.store.get_skill_state(name)
        base = skills.dir_hash(self.target_dir(name)) if kind == "amendment" and self.target_dir(name).exists() else None
        state = row["state"] if row and row["state"] in ("active", "evaluated", "candidate") else "candidate"
        attempts = (row["attempts"] if row else 0) + (1 if kind == "amendment" else 0)
        old = json.loads(row["meta"]) if row else {}
        for k in ("eval", "verdict", "promotable"):
            old.pop(k, None)
        old["pending"] = {"kind": kind, "evidence": evidence, "base_hash": base, "sessions": sessions, "drafted_by": drafted_by}
        self.store.set_skill_state(name, state, attempts=attempts, meta=old)
        self.store.audit(actor, f"stage:{kind}", name, {"evidence": evidence})
        return d

    def pending(self) -> list[str]:
        return [r["name"] for r in self.store.list_skill_states() if "pending" in json.loads(r["meta"])]

    # ---- evaluation -----------------------------------------------------------------------------------------
    def _suite(self, name: str) -> dict | None:
        for p in (self.staged_dir(name) / "evals.json", self.target_dir(name) / "evals" / "evals.json"):
            if p.is_file():
                try:
                    return evalharness.load_suite(p)
                except (OSError, ValueError):
                    return None
        return None

    def evaluate(self, name: str, actor: str = "skillevo") -> evalharness.Verdict:
        pend = self.meta(name).get("pending")
        staged = self.staged_dir(name) / "SKILL.md"
        if not pend or not staged.is_file():
            raise PolicyError(f"nothing staged for {name}")
        suite = self._suite(name)
        terms = evidence_terms(self.store, pend.get("evidence", []))
        cand = evalharness.evaluate(staged.read_text(encoding="utf-8"), suite, terms, self.cfg.curator.max_skill_bytes, name)
        cur_file = self.target_dir(name) / "SKILL.md"
        cur = None
        if pend["kind"] == "amendment" and cur_file.is_file():
            cur = evalharness.evaluate(cur_file.read_text(encoding="utf-8"), suite, terms, self.cfg.curator.max_skill_bytes, name)
        verdict = evalharness.compare(cand, cur, self.cfg.evolution.min_improvement)
        cur_state = self.state(name)
        updates = {"eval": cand.to_json(), "verdict": verdict.reason, "promotable": verdict.promote}
        if cur_state == "candidate":
            self._move(name, "evaluated" if verdict.promote else "rejected", actor, **updates)
        else:
            self.store.set_skill_state(name, cur_state or "evaluated", meta={**self.meta(name), **updates})
        if not verdict.promote:
            self.store.audit(actor, "eval:not-promoted", name, verdict.reason)
        return verdict

    # ---- installation ---------------------------------------------------------------------------------------
    def can_auto_modify(self, name: str) -> tuple[bool, str]:
        own = skills.ownership(self.target_dir(name))
        if own in ("absent", "managed"):
            return True, own
        return False, ("a user-owned skill with this name exists" if own == "user"
                       else "the managed skill was edited by the user since SkillEvo last wrote it")

    def install(self, name: str, actor: str, allow_unmanaged: bool = False) -> Path:
        meta = self.meta(name)
        pend = meta.get("pending")
        staged = self.staged_dir(name)
        if not pend or not (staged / "SKILL.md").is_file():
            raise PolicyError(f"nothing staged for {name}")
        if not meta.get("promotable"):
            raise PolicyError(f"{name} has not passed evaluation: {meta.get('verdict', 'not evaluated')}")
        dest = self.target_dir(name)
        own = skills.ownership(dest)
        if own in ("user", "modified") and not allow_unmanaged:
            raise PolicyError(f"refusing to overwrite {name}: {self.can_auto_modify(name)[1]} (use --allow-unmanaged to override)")
        if own == "absent" and pend["kind"] == "amendment":
            raise PolicyError(f"amendment target {name} no longer exists")
        version = versioning.snapshot(dest, name, f"before {pend['kind']} install by {actor}") if dest.exists() else None
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged / "SKILL.md", dest / "SKILL.md")
        prev = skills.read_marker(dest) or {}
        evidence = sorted(set(prev.get("evidence", [])) | set(pend.get("evidence", [])))
        skills.write_marker(dest, evidence=evidence, project=self.project)
        cur = self.state(name)
        if cur != "active":
            self._move(name, "active", actor, installed=str(dest))
        meta2 = {k: v for k, v in self.meta(name).items() if k not in ("pending", "promotable")}
        self.store.set_skill_state(name, "active", attempts=0, meta={**meta2, "evidence": evidence})
        self.store.audit(actor, "install", name, {"snapshot": version, "kind": pend["kind"]})
        shutil.rmtree(staged, ignore_errors=True)
        return dest

    def reject(self, name: str, actor: str) -> None:
        cur = self.state(name)
        if cur in ("candidate", "evaluated"):
            self._move(name, "rejected", actor)
        full = self.meta(name)
        meta = {k: v for k, v in full.items() if k not in ("pending", "promotable")}
        meta["rejected_evidence"] = sorted(set(full.get("rejected_evidence", [])) | set((full.get("pending") or {}).get("evidence", [])))
        self.store.set_skill_state(name, self.state(name) or "rejected", meta=meta)
        shutil.rmtree(self.staged_dir(name), ignore_errors=True)
        self.store.audit(actor, "reject", name)

    def retire(self, name: str, actor: str, allow_unmanaged: bool = False) -> None:
        dest = self.target_dir(name)
        own = skills.ownership(dest)
        if own in ("user", "modified") and not allow_unmanaged:
            raise PolicyError(f"refusing to retire {name}: {self.can_auto_modify(name)[1]} (use --allow-unmanaged to override)")
        if not dest.exists():
            raise PolicyError(f"{name} is not installed")
        version = versioning.snapshot(dest, name, f"retired by {actor}")
        shutil.rmtree(dest)
        if self.state(name) in ("active",):
            self._move(name, "retired", actor)
        else:
            self.store.set_skill_state(name, "retired", managed=own == "managed")
        self.store.audit(actor, "retire", name, {"snapshot": version})

    def rollback(self, name: str, actor: str, version: int | None = None) -> int:
        dest = self.target_dir(name)
        restored = versioning.rollback(dest, name, version)
        own = skills.ownership(dest)
        self.store.set_skill_state(name, "active", managed=own == "managed", attempts=0, meta=self.meta(name))
        self.store.audit(actor, "rollback", name, {"to": restored})
        return restored

    # ---- policy ---------------------------------------------------------------------------------------------
    def apply_policy(self, name: str) -> str:
        """What automation may do with an evaluated candidate. Returns a short action label."""
        mode = self.cfg.policy.mode
        meta = self.meta(name)
        if not meta.get("promotable"):
            return "not-promotable"
        if mode == "review-only":
            return "review-only: recommendation recorded, nothing installed"
        if mode == "approval-required":
            self.store.audit("skillevo", "queued-for-approval", name)
            return "queued: run `skillevo approve " + name + "`"
        ok, why = self.can_auto_modify(name)
        if not ok:
            self.store.audit("skillevo", "autonomous-blocked", name, why)
            return f"blocked: {why}"
        pend = meta.get("pending") or {}
        need = self.cfg.detection.min_pattern_sessions
        if pend.get("kind") == "new" and pend.get("sessions", 0) < need:
            # A new skill from thin evidence (one session, one lesson) always waits for a human, whatever the mode.
            self.store.audit("skillevo", "autonomous-needs-approval", name, f"evidence from {pend.get('sessions', 0)} session(s), need {need}")
            return (f"queued: autonomous installs of new skills need evidence from {need}+ separate sessions "
                    f"(has {pend.get('sessions', 0)}); run `skillevo approve {name}`")
        if self.cfg.llm.enabled and self.cfg.orchestration.review_required:
            outcome = self._gate_with_review(name, pend)
            if outcome:
                return outcome
        self.install(name, "skillevo-autonomous")
        return "installed (autonomous)"

    def _review_ok(self, review: dict | None) -> bool:
        """An approval counts only if the reviewer call really is in the local log, with the review role and success."""
        if not review or review.get("verdict") != "approve":
            return False
        row = self.store.llm_call(int(review.get("call_id", -1)))
        return bool(row and row["role"] == "review" and row["ok"])

    def _gate_with_review(self, name: str, pend: dict) -> str | None:
        """Autonomous installs must be approved by the strong model when models are enabled. None = approved."""
        meta = self.meta(name)
        review = meta.get("review")
        if not self._review_ok(review):
            if self.reviewer is None:
                return f"queued: a model review is required before an autonomous install (run `skillevo evolve --smart`, or `skillevo approve {name}`)"
            text = (self.staged_dir(name) / "SKILL.md").read_text(encoding="utf-8")
            res = self.reviewer(name, text, pend)
            if not res:
                return f"queued: the reviewer was unavailable; run `skillevo approve {name}`"
            review = {"verdict": res.get("verdict"), "issues": res.get("issues", []), "model": res.get("model"),
                      "call_id": res.get("_call_id")}
            self.store.set_skill_state(name, self.state(name) or "evaluated", meta={**meta, "review": review})
            self.store.audit("skillevo", "review", name, review)
        if not self._review_ok(review):
            issues = "; ".join(str(i) for i in (review or {}).get("issues", []))[:200]
            return f"queued: reviewer said {review.get('verdict') if review else 'nothing'}" + (f" ({issues})" if issues else "") + f"; run `skillevo approve {name}`"
        return None
