"""Persistent overlay store for the reports plugin (its own SQLite db).

Three concerns:
  - summaries: AI/heuristic task summaries, cached by (task_id, last_event_id)
  - decisions: stateful "needs your hand" items that persist across days until
    you resolve them or their veto window expires
  - digests:   cached daily digest JSON, so weekly/monthly are pure roll-ups
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .config import Config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    task_id        TEXT PRIMARY KEY,
    last_event_id  INTEGER NOT NULL,
    outcome        TEXT,
    why            TEXT,
    waiting_on     TEXT,
    bullets        TEXT,
    updated_at     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id           TEXT PRIMARY KEY,
    task_id      TEXT,
    kind         TEXT NOT NULL,           -- approval | blocked | failed | instability
    title        TEXT NOT NULL,
    detail       TEXT,
    status       TEXT NOT NULL DEFAULT 'open',  -- open | resolved | expired
    created_at   INTEGER NOT NULL,
    deadline     INTEGER,
    resolved_at  INTEGER,
    resolution   TEXT
);
CREATE TABLE IF NOT EXISTS digests (
    date         TEXT PRIMARY KEY,        -- YYYY-MM-DD (local)
    json         TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
CREATE TABLE IF NOT EXISTS build_status (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    state        TEXT NOT NULL DEFAULT 'idle',   -- idle | running
    label        TEXT,
    current      TEXT,
    done         INTEGER DEFAULT 0,
    total        INTEGER DEFAULT 0,
    started_at   INTEGER,
    updated_at   INTEGER,
    finished_at  INTEGER,
    error        TEXT
);
"""


class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = sqlite3.connect(cfg.reports_db(), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO build_status(id,state) VALUES(1,'idle')")
        self.conn.commit()

    # -- summaries -------------------------------------------------------

    def get_summary(self, task_id: str, last_event_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM summaries WHERE task_id=? AND last_event_id>=?",
            (task_id, last_event_id),
        ).fetchone()
        return dict(row) if row else None

    def put_summary(self, task_id: str, last_event_id: int, data: dict) -> None:
        self.conn.execute(
            """INSERT INTO summaries(task_id,last_event_id,outcome,why,waiting_on,bullets,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(task_id) DO UPDATE SET
                 last_event_id=excluded.last_event_id, outcome=excluded.outcome,
                 why=excluded.why, waiting_on=excluded.waiting_on,
                 bullets=excluded.bullets, updated_at=excluded.updated_at""",
            (task_id, last_event_id, data.get("outcome", ""), data.get("why", ""),
             data.get("waiting_on", ""), json.dumps(data.get("bullets", []), ensure_ascii=False),
             int(time.time())),
        )
        self.conn.commit()

    # -- decisions -------------------------------------------------------

    def upsert_decision(self, d: dict) -> None:
        existing = self.conn.execute(
            "SELECT id,status FROM decisions WHERE id=?", (d["id"],)
        ).fetchone()
        if existing:
            if existing["status"] == "open":   # don't clobber a resolved item
                self.conn.execute(
                    "UPDATE decisions SET title=?, detail=?, deadline=? WHERE id=?",
                    (d["title"], d.get("detail", ""), d.get("deadline"), d["id"]),
                )
        else:
            self.conn.execute(
                """INSERT INTO decisions(id,task_id,kind,title,detail,status,created_at,deadline)
                   VALUES(?,?,?,?,?, 'open', ?, ?)""",
                (d["id"], d.get("task_id"), d["kind"], d["title"], d.get("detail", ""),
                 int(time.time()), d.get("deadline")),
            )
        self.conn.commit()

    def open_decisions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE status='open' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def expire_due(self) -> int:
        now = int(time.time())
        cur = self.conn.execute(
            "UPDATE decisions SET status='expired', resolved_at=? "
            "WHERE status='open' AND deadline IS NOT NULL AND deadline < ?",
            (now, now),
        )
        self.conn.commit()
        return cur.rowcount

    def resolve_decision(self, decision_id: str, resolution: str) -> bool:
        cur = self.conn.execute(
            "UPDATE decisions SET status='resolved', resolution=?, resolved_at=? "
            "WHERE id=? AND status='open'",
            (resolution, int(time.time()), decision_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def decision_stats(self, since_ts: int) -> dict:
        rows = self.conn.execute(
            "SELECT status, resolution, COUNT(*) n FROM decisions "
            "WHERE created_at>=? GROUP BY status, resolution", (since_ts,)
        ).fetchall()
        out = {"total": 0, "resolved": 0, "vetoed": 0, "expired": 0, "open": 0}
        for r in rows:
            out["total"] += r["n"]
            if r["status"] == "expired":
                out["expired"] += r["n"]
            elif r["status"] == "open":
                out["open"] += r["n"]
            elif r["status"] == "resolved":
                out["resolved"] += r["n"]
                if (r["resolution"] or "").lower() in ("veto", "stop", "vetoed", "reject"):
                    out["vetoed"] += r["n"]
        return out

    # -- digests ---------------------------------------------------------

    def put_digest(self, date: str, digest: dict) -> None:
        self.conn.execute(
            "INSERT INTO digests(date,json,created_at) VALUES(?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET json=excluded.json, created_at=excluded.created_at",
            (date, json.dumps(digest, ensure_ascii=False), int(time.time())),
        )
        self.conn.commit()

    def get_digest(self, date: str) -> dict | None:
        row = self.conn.execute("SELECT json FROM digests WHERE date=?", (date,)).fetchone()
        return json.loads(row["json"]) if row else None

    def list_digests(self, limit: int = 60) -> list[dict]:
        rows = self.conn.execute(
            "SELECT date, json FROM digests ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["json"])
            out.append({
                "date": r["date"],
                "open": len(d.get("hand", [])),
                "cost_eur": d.get("cost", {}).get("today_eur", 0.0),
                "done": len(d.get("done", [])),
            })
        return out

    def close(self) -> None:
        self.conn.close()

    # -- build status (shared across processes: UI, CLI, timer) ----------

    def build_begin(self, label: str, total: int) -> None:
        now = int(time.time())
        self.conn.execute(
            "UPDATE build_status SET state='running', label=?, current=?, done=0, "
            "total=?, started_at=?, updated_at=?, finished_at=NULL, error=NULL WHERE id=1",
            (label, label, total, now, now),
        )
        self.conn.commit()

    def build_step(self, current: str, done: int) -> None:
        self.conn.execute(
            "UPDATE build_status SET current=?, done=?, updated_at=? WHERE id=1 AND state='running'",
            (current, done, int(time.time())),
        )
        self.conn.commit()

    def build_finish(self, error: str | None = None) -> None:
        now = int(time.time())
        self.conn.execute(
            "UPDATE build_status SET state='idle', updated_at=?, finished_at=?, error=? WHERE id=1",
            (now, now, error),
        )
        self.conn.commit()

    def build_status(self, stale_after: int = 600) -> dict:
        row = self.conn.execute("SELECT * FROM build_status WHERE id=1").fetchone()
        d = dict(row) if row else {"state": "idle"}
        now = int(time.time())
        # a 'running' row that hasn't ticked in stale_after sec => the worker died
        if d.get("state") == "running" and d.get("updated_at") and now - d["updated_at"] > stale_after:
            d["state"] = "idle"
            d["error"] = "stale (build process ended without finishing)"
        d["running"] = d.get("state") == "running"
        return d

    def reconcile_decisions(self, tasks: dict) -> int:
        """Auto-close open decisions whose task has clearly moved past blocked.

        Prevents stale 'needs your hand' items on first-install bootstrap, when a
        task was blocked days ago but has since been unblocked / completed.
        """
        closed = 0
        for d in self.open_decisions():
            t = tasks.get(d.get("task_id"))
            if not t:
                continue
            st = (t.status or "").lower()
            if st in ("done", "archived"):
                if self.resolve_decision(d["id"], "auto-done"):
                    closed += 1
            elif st in ("running", "ready", "todo") and d["kind"] in ("approval", "blocked", "failed"):
                if self.resolve_decision(d["id"], "auto-cleared"):
                    closed += 1
        return closed
