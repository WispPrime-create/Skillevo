"""SQLite persistence: raw events, distilled experiences (FTS5/BM25), skill telemetry, audit log."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from . import tokenize

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, session TEXT, project TEXT NOT NULL, event TEXT NOT NULL,
  tool TEXT, summary TEXT, ok INTEGER, sig TEXT);
CREATE INDEX IF NOT EXISTS ix_events_proj_ts ON events(project, ts);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session, ts);
CREATE TABLE IF NOT EXISTS experiences(
  id INTEGER PRIMARY KEY, project TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'project', kind TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'auto',
  salience REAL NOT NULL DEFAULT 0.3, confidence REAL NOT NULL DEFAULT 0.3, status TEXT NOT NULL DEFAULT 'active',
  fingerprint TEXT NOT NULL UNIQUE, seen INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, last_used REAL,
  uses INTEGER NOT NULL DEFAULT 0, evidence TEXT NOT NULL DEFAULT '[]', index_text TEXT NOT NULL DEFAULT '',
  sessions TEXT NOT NULL DEFAULT '', superseded_by INTEGER);
CREATE INDEX IF NOT EXISTS ix_exp_proj ON experiences(project, status);
CREATE TABLE IF NOT EXISTS retrievals(
  id INTEGER PRIMARY KEY, ts REAL, session TEXT, project TEXT, query_chars INTEGER, hits INTEGER,
  injected_chars INTEGER, latency_ms REAL);
CREATE TABLE IF NOT EXISTS skill_events(
  id INTEGER PRIMARY KEY, ts REAL, project TEXT, skill TEXT NOT NULL, source TEXT NOT NULL, session TEXT, ok INTEGER);
CREATE INDEX IF NOT EXISTS ix_skill_events ON skill_events(skill, ts);
CREATE TABLE IF NOT EXISTS skills_state(
  name TEXT PRIMARY KEY, state TEXT NOT NULL, managed INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
  updated REAL, meta TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS llm_calls(id INTEGER PRIMARY KEY, ts REAL NOT NULL, purpose TEXT, ok INTEGER,
  role TEXT, model TEXT, cost REAL);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS exp_fts USING fts5(
  index_text, content='experiences', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS exp_ai AFTER INSERT ON experiences BEGIN
  INSERT INTO exp_fts(rowid, index_text) VALUES (new.id, new.index_text); END;
CREATE TRIGGER IF NOT EXISTS exp_ad AFTER DELETE ON experiences BEGIN
  INSERT INTO exp_fts(exp_fts, rowid, index_text) VALUES('delete', old.id, old.index_text); END;
CREATE TRIGGER IF NOT EXISTS exp_au AFTER UPDATE OF index_text ON experiences BEGIN
  INSERT INTO exp_fts(exp_fts, rowid, index_text) VALUES('delete', old.id, old.index_text);
  INSERT INTO exp_fts(rowid, index_text) VALUES (new.id, new.index_text); END;
"""


def fingerprint(kind: str, title: str, body: str) -> str:
    norm = " ".join(sorted(set(tokenize.tokens(title + " " + body))))
    return hashlib.sha256((kind + "|" + norm).encode("utf-8")).hexdigest()[:24]


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=3.0)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
        self.db.executescript(SCHEMA)
        self._migrate()
        self.has_fts = self._init_fts()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created (CREATE IF NOT EXISTS never alters tables)."""
        wanted = {"experiences": {"sessions": "TEXT NOT NULL DEFAULT ''", "superseded_by": "INTEGER"},
                  "llm_calls": {"role": "TEXT", "model": "TEXT", "cost": "REAL"}}
        for table, cols in wanted.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.db.commit()

    def _init_fts(self) -> bool:
        try:
            self.db.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- events ---------------------------------------------------------------------------------------------
    def add_event(self, project: str, event: str, session: str = "", tool: str = "", summary: str = "",
                  ok: bool | None = None, sig: str = "", ts: float | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO events(ts, session, project, event, tool, summary, ok, sig) VALUES (?,?,?,?,?,?,?,?)",
            (ts or time.time(), session, project, event, tool, summary, None if ok is None else int(ok), sig))
        self.db.commit()
        return int(cur.lastrowid)

    def events(self, project: str | None = None, session: str | None = None, since: float = 0.0,
               limit: int = 5000) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM events WHERE ts >= ?", [since]
        if project:
            q += " AND project = ?"
            args.append(project)
        if session:
            q += " AND session = ?"
            args.append(session)
        q += " ORDER BY ts, id LIMIT ?"
        args.append(limit)
        return list(self.db.execute(q, args))

    # ---- experiences ----------------------------------------------------------------------------------------
    def add_experience(self, project: str, kind: str, title: str, body: str, tags: Iterable[str] = (),
                       source: str = "auto", salience: float = 0.3, confidence: float = 0.3,
                       scope: str = "project", evidence: list | None = None, status: str = "active",
                       sessions: Iterable[str] = ()) -> tuple[int, bool]:
        """Insert, or merge into the existing row with the same fingerprint. Confidence and `seen` only grow when the
        row is corroborated by a session it has not been seen in; re-detecting the same events changes nothing."""
        fp = fingerprint(kind, title, body)
        sess = {s for s in sessions if s}
        row = self.db.execute("SELECT id, seen, salience, confidence, sessions FROM experiences WHERE fingerprint=?", (fp,)).fetchone()
        if row:
            known = {s for s in row["sessions"].split(",") if s}
            fresh = sess - known
            if fresh:
                self.db.execute(
                    "UPDATE experiences SET seen=seen+1, salience=?, confidence=?, sessions=? WHERE id=?",
                    (max(row["salience"], salience), min(0.95, max(row["confidence"], confidence) + 0.05),
                     ",".join(sorted(known | sess)), row["id"]))
            else:
                self.db.execute("UPDATE experiences SET salience=? WHERE id=?", (max(row["salience"], salience), row["id"]))
            self.db.commit()
            return int(row["id"]), False
        cur = self.db.execute(
            "INSERT INTO experiences(project, scope, kind, title, body, tags, source, salience, confidence, status, "
            "fingerprint, created, evidence, index_text, sessions) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, scope, kind, title, body, ",".join(tags), source, salience, confidence, status, fp, time.time(),
             json.dumps(evidence or []), tokenize.index_text(title, body, " ".join(tags)), ",".join(sorted(sess))))
        self.db.commit()
        return int(cur.lastrowid), True

    def get_experience(self, exp_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM experiences WHERE id=?", (exp_id,)).fetchone()

    def list_experiences(self, project: str | None = None, status: str | None = "active",
                         kind: str | None = None, limit: int = 1000) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM experiences WHERE 1=1", []
        if project:
            q += " AND (project = ? OR scope = 'global')"
            args.append(project)
        if status:
            q += " AND status = ?"
            args.append(status)
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY created DESC LIMIT ?"
        args.append(limit)
        return list(self.db.execute(q, args))

    def supersede(self, old_id: int, new_id: int) -> None:
        """Keep the raw row for audit and rollback, but stop retrieving it once a distilled version exists."""
        self.db.execute("UPDATE experiences SET status='superseded', superseded_by=? WHERE id=?", (new_id, old_id))
        self.db.commit()

    def set_experience_status(self, exp_id: int, status: str) -> None:
        self.db.execute("UPDATE experiences SET status=? WHERE id=?", (status, exp_id))
        self.db.commit()

    def mark_used(self, ids: Iterable[int]) -> None:
        now = time.time()
        self.db.executemany("UPDATE experiences SET uses=uses+1, last_used=? WHERE id=?", [(now, i) for i in ids])
        self.db.commit()

    def search(self, project: str, query_text: str, pool: int = 20) -> list[dict]:
        """Return up to `pool` candidate experiences ordered best-first, each with a `rank` (lower is better)."""
        if not tokenize.unique_tokens(query_text):
            return []
        if self.has_fts:
            q = tokenize.fts_query(query_text)
            try:
                rows = self.db.execute(
                    "SELECT e.*, bm25(exp_fts) AS rank FROM exp_fts JOIN experiences e ON e.id = exp_fts.rowid "
                    "WHERE exp_fts MATCH ? AND e.status='active' AND (e.project=? OR e.scope='global') "
                    "ORDER BY rank LIMIT ?", (q, project, pool)).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass  # fall through to the portable path
        qset = set(tokenize.unique_tokens(query_text))
        scored = []
        for r in self.list_experiences(project, "active", limit=500):
            overlap = len(qset & set(r["index_text"].split()))
            if overlap:
                d = dict(r)
                d["rank"] = -float(overlap)
                scored.append(d)
        scored.sort(key=lambda d: d["rank"])
        return scored[:pool]

    def log_retrieval(self, session: str, project: str, query_chars: int, hits: int, injected: int, latency_ms: float) -> None:
        self.db.execute(
            "INSERT INTO retrievals(ts, session, project, query_chars, hits, injected_chars, latency_ms) VALUES (?,?,?,?,?,?,?)",
            (time.time(), session, project, query_chars, hits, injected, latency_ms))
        self.db.commit()

    # ---- skills ---------------------------------------------------------------------------------------------
    def record_skill_event(self, project: str, skill: str, source: str, session: str = "", ok: bool | None = None) -> None:
        self.db.execute(
            "INSERT INTO skill_events(ts, project, skill, source, session, ok) VALUES (?,?,?,?,?,?)",
            (time.time(), project, skill, source, session, None if ok is None else int(ok)))
        self.db.commit()

    def skill_usage(self, since: float = 0.0) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.db.execute(
                "SELECT skill, COUNT(*) n, MAX(ts) last, SUM(CASE WHEN source='typed' THEN 1 ELSE 0 END) typed "
                "FROM skill_events WHERE ts >= ? GROUP BY skill", (since,)):
            out[r["skill"]] = {"uses": r["n"], "last": r["last"], "typed": r["typed"]}
        return out

    def telemetry_span_days(self) -> float:
        """Days between the first and last recorded event; 0 means there is no observation window yet."""
        r = self.db.execute("SELECT MIN(ts) a, MAX(ts) b FROM events").fetchone()
        if not r or r["a"] is None:
            return 0.0
        return (r["b"] - r["a"]) / 86400.0

    def get_skill_state(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM skills_state WHERE name=?", (name,)).fetchone()

    def set_skill_state(self, name: str, state: str, managed: bool = True, attempts: int | None = None,
                        meta: dict | None = None) -> None:
        cur = self.get_skill_state(name)
        att = attempts if attempts is not None else (cur["attempts"] if cur else 0)
        m = json.dumps(meta) if meta is not None else (cur["meta"] if cur else "{}")
        self.db.execute(
            "INSERT INTO skills_state(name, state, managed, attempts, updated, meta) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET state=excluded.state, managed=excluded.managed, attempts=excluded.attempts, "
            "updated=excluded.updated, meta=excluded.meta", (name, state, int(managed), att, time.time(), m))
        self.db.commit()

    def list_skill_states(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM skills_state ORDER BY name"))

    # ---- audit / llm budget ---------------------------------------------------------------------------------
    def audit(self, actor: str, action: str, target: str = "", detail: Any = None) -> None:
        self.db.execute("INSERT INTO audit(ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                        (time.time(), actor, action, target, json.dumps(detail, default=str) if detail is not None else ""))
        self.db.commit()

    def audit_log(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)))

    def llm_calls_today(self) -> int:
        since = time.time() - 86400
        return int(self.db.execute("SELECT COUNT(*) FROM llm_calls WHERE ts >= ?", (since,)).fetchone()[0])

    def record_llm_call(self, purpose: str, ok: bool, role: str = "", model: str = "", cost: float = 0.0) -> int:
        cur = self.db.execute("INSERT INTO llm_calls(ts, purpose, ok, role, model, cost) VALUES (?,?,?,?,?,?)",
                              (time.time(), purpose, int(ok), role, model, cost))
        self.db.commit()
        return int(cur.lastrowid)

    def llm_call(self, call_id: int):
        return self.db.execute("SELECT * FROM llm_calls WHERE id=?", (call_id,)).fetchone()

    # ---- maintenance ----------------------------------------------------------------------------------------
    def prune(self, retention_days: int, max_events_per_project: int) -> dict[str, int]:
        cutoff = time.time() - retention_days * 86400
        removed = self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        for (proj,) in self.db.execute("SELECT DISTINCT project FROM events").fetchall():
            removed += self.db.execute(
                "DELETE FROM events WHERE project=? AND id NOT IN (SELECT id FROM events WHERE project=? "
                "ORDER BY ts DESC, id DESC LIMIT ?)", (proj, proj, max_events_per_project)).rowcount
        self.db.execute("DELETE FROM retrievals WHERE ts < ?", (cutoff,))
        self.db.commit()
        return {"events_removed": removed}

    def stats(self) -> dict[str, Any]:
        def n(sql: str) -> int:
            return int(self.db.execute(sql).fetchone()[0])
        return {
            "fts5": self.has_fts,
            "events": n("SELECT COUNT(*) FROM events"),
            "experiences_active": n("SELECT COUNT(*) FROM experiences WHERE status='active'"),
            "experiences_quarantined": n("SELECT COUNT(*) FROM experiences WHERE status='quarantined'"),
            "retrievals": n("SELECT COUNT(*) FROM retrievals"),
            "skills_tracked": n("SELECT COUNT(*) FROM skills_state"),
        }
