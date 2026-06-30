"""Read-only access to the Hermes kanban SQLite board.

Table names (tasks, task_events, task_comments + a runs table) are documented
and stable. Column names can drift across Hermes versions, so we resolve them
at runtime via PRAGMA table_info and allow explicit overrides in config.schema.

We open the DB read-only (mode=ro) over a file: URI so we never block the
dispatcher's writers (the board runs in WAL mode).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import Config


# Candidate column names per logical field, best-first.
_CANDIDATES: dict[str, list[str]] = {
    "event_id":     ["id", "event_id", "rowid", "seq"],
    "event_task":   ["task_id", "task", "card_id", "tid"],
    "event_kind":   ["kind", "type", "event", "event_type", "name"],
    "event_data":   ["data", "payload", "body", "detail", "details", "meta", "json"],
    "event_ts":     ["created_at", "ts", "timestamp", "created", "time", "at"],
    "event_run":    ["run_id", "run", "attempt", "attempt_id"],

    "task_id":      ["id", "task_id"],
    "task_title":   ["title", "name", "summary"],
    "task_status":  ["status", "state"],
    "task_assignee":["assignee", "profile", "assigned_to", "owner"],
    "task_tenant":  ["tenant", "project", "namespace"],
    "task_priority":["priority", "prio"],
    "task_created": ["created_at", "ts", "created", "time"],
    "task_body":    ["body", "description", "desc"],

    "run_id":       ["id", "run_id"],
    "run_task":     ["task_id", "task"],
    "run_outcome":  ["outcome", "status", "result"],
    "run_profile":  ["profile", "assignee", "agent"],
    "run_summary":  ["summary", "result", "handoff"],
    "run_error":    ["error", "err", "message"],
    "run_started":  ["started_at", "created_at", "start", "started"],
    "run_ended":    ["ended_at", "finished_at", "end", "ended"],
    "run_in_tok":   ["input_tokens", "prompt_tokens", "tokens_in", "in_tokens"],
    "run_out_tok":  ["output_tokens", "completion_tokens", "tokens_out", "out_tokens"],
    "run_cost":     ["cost", "cost_eur", "cost_usd", "price"],
}


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    data: dict
    ts: int          # epoch seconds
    run_id: Any


@dataclass
class Task:
    id: str
    title: str
    status: str
    assignee: str
    tenant: str
    priority: Any
    created: int


def _to_epoch(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        v = float(v)
        return int(v / 1000) if v > 1e12 else int(v)
    s = str(v).strip()
    if s.isdigit():
        n = int(s)
        return n // 1000 if n > 1e12 else n
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return int(datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            continue
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


class KanbanSource:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.resolved_kanban_db()
        self._conn: sqlite3.Connection | None = None
        self._cols: dict[str, dict[str, str]] = {}   # table -> {logical: actual}
        self._events_table = "task_events"
        self._tasks_table = "tasks"
        self._comments_table = "task_comments"
        self._runs_table: str | None = None

    # -- connection / introspection -------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            uri = f"file:{self.path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=5)
            self._conn.row_factory = sqlite3.Row
            self._introspect()
        return self._conn

    def _tables(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return [r[0] for r in rows]

    def _table_cols(self, table: str) -> list[str]:
        try:
            return [r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()]
        except Exception:
            return []

    def _pick(self, logical: str, available: list[str], override: str | None) -> str | None:
        if override and override in available:
            return override
        for cand in _CANDIDATES.get(logical, []):
            if cand in available:
                return cand
        return None

    def _introspect(self) -> None:
        tables = self._tables()
        ov = self.cfg.schema or {}
        # locate runs table (one row per attempt)
        for cand in ("task_runs", "runs", "kanban_runs"):
            if cand in tables:
                self._runs_table = cand
                break
        if not self._runs_table:
            for t in tables:
                if "run" in t.lower():
                    self._runs_table = t
                    break

        ev = self._table_cols(self._events_table)
        self._cols["events"] = {
            logical: self._pick(logical, ev, ov.get(logical))
            for logical in ("event_id", "event_task", "event_kind", "event_data", "event_ts", "event_run")
        }
        tk = self._table_cols(self._tasks_table)
        self._cols["tasks"] = {
            logical: self._pick(logical, tk, ov.get(logical))
            for logical in ("task_id", "task_title", "task_status", "task_assignee",
                            "task_tenant", "task_priority", "task_created", "task_body")
        }
        if self._runs_table:
            rn = self._table_cols(self._runs_table)
            self._cols["runs"] = {
                logical: self._pick(logical, rn, ov.get(logical))
                for logical in ("run_id", "run_task", "run_outcome", "run_profile", "run_summary",
                                "run_error", "run_started", "run_ended", "run_in_tok",
                                "run_out_tok", "run_cost")
            }

    def inspect_schema(self) -> dict:
        self.connect()
        out = {
            "db_path": str(self.path),
            "tables": self._tables(),
            "runs_table": self._runs_table,
            "resolved_columns": self._cols,
            "raw_columns": {
                self._events_table: self._table_cols(self._events_table),
                self._tasks_table: self._table_cols(self._tasks_table),
            },
        }
        if self._runs_table:
            out["raw_columns"][self._runs_table] = self._table_cols(self._runs_table)
        return out

    # -- queries ---------------------------------------------------------

    def _ev_col(self, logical: str) -> str | None:
        return self._cols.get("events", {}).get(logical)

    def fetch_events(self, start_ts: int, end_ts: int) -> list[Event]:
        self.connect()
        c = self._cols["events"]
        tid, kind, data, ts, eid, run = (
            c["event_task"], c["event_kind"], c["event_data"],
            c["event_ts"], c["event_id"], c["event_run"],
        )
        if not (tid and kind and ts):
            return []
        eid = eid or "rowid"
        sel = f'"{eid}" AS _id, "{tid}" AS _task, "{kind}" AS _kind, "{ts}" AS _ts'
        sel += f', "{data}" AS _data' if data else ", NULL AS _data"
        sel += f', "{run}" AS _run' if run else ", NULL AS _run"
        q = (f'SELECT {sel} FROM {self._events_table} '
             f'WHERE "{ts}" >= ? AND "{ts}" < ? ORDER BY "{eid}" ASC')
        # ts may be epoch or ISO; we filter in python if it's text
        rows = self._conn.execute(f'SELECT {sel} FROM {self._events_table} ORDER BY "{eid}" ASC').fetchall() \
            if self._ts_is_text(ts) else self._conn.execute(q, (start_ts, end_ts)).fetchall()
        out: list[Event] = []
        for r in rows:
            ets = _to_epoch(r["_ts"])
            if not (start_ts <= ets < end_ts):
                continue
            payload = r["_data"]
            try:
                payload = json.loads(payload) if isinstance(payload, str) and payload.strip().startswith(("{", "[")) else (payload or {})
            except Exception:
                payload = {"raw": payload}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            out.append(Event(id=r["_id"], task_id=str(r["_task"]), kind=str(r["_kind"]),
                             data=payload, ts=ets, run_id=r["_run"]))
        return out

    def _ts_is_text(self, col: str) -> bool:
        try:
            row = self._conn.execute(
                f'SELECT "{col}" FROM {self._events_table} '
                f'WHERE "{col}" IS NOT NULL LIMIT 1').fetchone()
            return bool(row) and isinstance(row[0], str) and not str(row[0]).isdigit()
        except Exception:
            return False

    def fetch_tasks(self, ids: Iterable[str] | None = None) -> dict[str, Task]:
        self.connect()
        c = self._cols["tasks"]
        cid, title, status = c["task_id"], c["task_title"], c["task_status"]
        if not cid:
            return {}
        cols = [f'"{cid}" AS _id']
        cols.append(f'"{title}" AS _title' if title else "'' AS _title")
        cols.append(f'"{status}" AS _status' if status else "'' AS _status")
        cols.append(f'"{c["task_assignee"]}" AS _asg' if c["task_assignee"] else "'' AS _asg")
        cols.append(f'"{c["task_tenant"]}" AS _ten' if c["task_tenant"] else "'' AS _ten")
        cols.append(f'"{c["task_priority"]}" AS _prio' if c["task_priority"] else "NULL AS _prio")
        cols.append(f'"{c["task_created"]}" AS _cr' if c["task_created"] else "NULL AS _cr")
        q = f'SELECT {", ".join(cols)} FROM {self._tasks_table}'
        rows = self._conn.execute(q).fetchall()
        out: dict[str, Task] = {}
        wanted = set(ids) if ids is not None else None
        for r in rows:
            _id = str(r["_id"])
            if wanted is not None and _id not in wanted:
                continue
            out[_id] = Task(id=_id, title=r["_title"] or "", status=(r["_status"] or "").lower(),
                            assignee=r["_asg"] or "", tenant=r["_ten"] or "",
                            priority=r["_prio"], created=_to_epoch(r["_cr"]))
        return out

    def fetch_runs(self, task_id: str) -> list[dict]:
        if not self._runs_table:
            return []
        c = self._cols.get("runs", {})
        tcol = c.get("run_task")
        if not tcol:
            return []
        rows = self._conn.execute(
            f'SELECT * FROM {self._runs_table} WHERE "{tcol}" = ?', (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def fetch_comments(self, task_id: str) -> list[dict]:
        self.connect()
        if self._comments_table not in self._tables():
            return []
        cols = self._table_cols(self._comments_table)
        tcol = next((x for x in ("task_id", "task", "card_id") if x in cols), None)
        if not tcol:
            return []
        rows = self._conn.execute(
            f'SELECT * FROM {self._comments_table} WHERE "{tcol}" = ?', (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def task_bundle(self, task_id: str, window_events: list[Event]) -> dict:
        """Everything needed to summarize one task: its events, runs, comments."""
        evs = [e for e in window_events if e.task_id == task_id]
        return {
            "task_id": task_id,
            "events": [{"kind": e.kind, "data": e.data, "ts": e.ts} for e in evs],
            "runs": self.fetch_runs(task_id),
            "comments": self.fetch_comments(task_id),
            "last_event_id": max((e.id for e in evs), default=0),
        }

    def has_run_token_columns(self) -> bool:
        c = self._cols.get("runs", {})
        return bool(c.get("run_in_tok") or c.get("run_out_tok") or c.get("run_cost"))

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
