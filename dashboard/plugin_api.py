"""Hermes Briefing — single-file dashboard plugin (backend routes + CLI).

Self-contained on purpose: the dashboard imports THIS module and mounts the
module-level ``router = APIRouter()`` at ``/api/plugins/briefing/`` — exactly
like the Hermes example plugins. No sibling packages, so nothing about how the
loader imports the file can break the import.

Standalone CLI (no dashboard needed):
    python plugin_api.py inspect-schema
    python plugin_api.py render [YYYY-MM-DD]
    python plugin_api.py bootstrap [N]
    python plugin_api.py range FROM TO
"""
from __future__ import annotations

import os
import sys
import json
import re
import time
import sqlite3
import subprocess
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Iterable, Optional

try:
    import yaml
except Exception:
    yaml = None

try:
    from fastapi import APIRouter, Body, HTTPException
    from fastapi.responses import PlainTextResponse, JSONResponse
    router = APIRouter()
except Exception:
    router = None

# ===================== config =====================
"""Configuration for the Hermes Reports plugin.

Resolution order (later wins):
  1. built-in defaults below
  2. ~/.hermes/reports/config.yaml  (or $HERMES_HOME/reports/config.yaml)
  3. environment variables (REPORTS_*)

Nothing here is required — the plugin runs with defaults and degrades
gracefully when a data source or API key is missing.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # provided by the hermes dashboard env
except Exception:  # pragma: no cover
    yaml = None


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


# Default € price per 1,000,000 tokens, keyed by model name (substring match).
# !!! VERIFY against your provider's current pricing — these are placeholders. !!!
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.5":   {"input": 1.10, "output": 9.00},
    "gpt-5":     {"input": 1.10, "output": 9.00},
    "claude":    {"input": 2.70, "output": 13.50},
    "_default":  {"input": 1.00, "output": 5.00},
}

# Substrings in a `blocked` event reason that mean "a human needs to decide".
DEFAULT_APPROVAL_KEYWORDS = [
    "review-required", "freigabe", "approval", "approve", "boss",
    "wartet auf", "waiting on", "ok?", "dein ok", "sign-off", "signoff",
    "human", "genehmigung", "bestätigung", "bestaetigung",
]


@dataclass
class LLMConfig:
    enabled: bool = False                  # off by default — opt in, stay cheap
    provider: str = "openai"               # "openai" (OpenAI-compatible) | "anthropic"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.5-mini"
    api_key: str = ""
    max_tokens: int = 120
    temperature: float = 0.1
    timeout: int = 30


@dataclass
class SMTPConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    starttls: bool = True
    sender: str = ""
    recipient: str = ""


@dataclass
class Config:
    hermes_home: Path = field(default_factory=_hermes_home)
    kanban_db: Path | None = None          # default: <hermes_home>/kanban.db
    reports_dir: Path | None = None        # default: <hermes_home>/reports
    timezone: str = "Europe/Berlin"
    language: str = "en"          # "en" | "de"
    schedule: list[str] = field(default_factory=lambda: ["19:30"])  # local times the timer runs

    budget_daily_eur: float = 15.0
    budget_monthly_eur: float = 400.0
    pricing: dict[str, dict[str, float]] = field(default_factory=lambda: dict(DEFAULT_PRICING))

    approval_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_APPROVAL_KEYWORDS))
    protocol_violation_alert_threshold: int = 2   # N+ violations on one task -> flag

    # Optional: only treat these tenants/assignees as "WFDE suggestions" feed.
    suggestion_tenants: list[str] = field(default_factory=list)

    llm: LLMConfig = field(default_factory=LLMConfig)
    smtp: SMTPConfig = field(default_factory=SMTPConfig)

    # Schema overrides — only needed if PRAGMA auto-detection picks wrong columns.
    schema: dict[str, Any] = field(default_factory=dict)

    def resolved_kanban_db(self) -> Path:
        return Path(self.kanban_db) if self.kanban_db else self.hermes_home / "kanban.db"

    def resolved_reports_dir(self) -> Path:
        p = Path(self.reports_dir) if self.reports_dir else self.hermes_home / "briefing"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def reports_db(self) -> Path:
        return self.resolved_reports_dir() / "briefing.db"


def _apply_yaml(cfg: Config, data: dict[str, Any]) -> None:
    for k in ("timezone", "language", "kanban_db", "reports_dir"):
        if data.get(k) is not None:
            setattr(cfg, k, data[k])
    if isinstance(data.get("schedule"), list):
        cfg.schedule = [str(s) for s in data["schedule"]]
    elif data.get("schedule"):
        cfg.schedule = [str(data["schedule"])]
    if "budget" in data:
        b = data["budget"]
        cfg.budget_daily_eur = float(b.get("daily_eur", cfg.budget_daily_eur))
        cfg.budget_monthly_eur = float(b.get("monthly_eur", cfg.budget_monthly_eur))
    if isinstance(data.get("pricing"), dict):
        cfg.pricing.update(data["pricing"])
    if isinstance(data.get("approval_keywords"), list):
        cfg.approval_keywords = [str(s).lower() for s in data["approval_keywords"]]
    if data.get("protocol_violation_alert_threshold") is not None:
        cfg.protocol_violation_alert_threshold = int(data["protocol_violation_alert_threshold"])
    if isinstance(data.get("suggestion_tenants"), list):
        cfg.suggestion_tenants = [str(s) for s in data["suggestion_tenants"]]
    if isinstance(data.get("schema"), dict):
        cfg.schema = data["schema"]
    if isinstance(data.get("llm"), dict):
        for k, v in data["llm"].items():
            if hasattr(cfg.llm, k):
                setattr(cfg.llm, k, v)
    if isinstance(data.get("smtp"), dict):
        for k, v in data["smtp"].items():
            if hasattr(cfg.smtp, k):
                setattr(cfg.smtp, k, v)


def _apply_env(cfg: Config) -> None:
    e = os.environ.get
    if e("REPORTS_KANBAN_DB"):       cfg.kanban_db = e("REPORTS_KANBAN_DB")
    if e("REPORTS_DIR"):             cfg.reports_dir = e("REPORTS_DIR")
    if e("REPORTS_TIMEZONE"):        cfg.timezone = e("REPORTS_TIMEZONE")
    if e("REPORTS_LANGUAGE"):        cfg.language = e("REPORTS_LANGUAGE")
    if e("REPORTS_SCHEDULE"):        cfg.schedule = [s.strip() for s in e("REPORTS_SCHEDULE").split(",") if s.strip()]
    if e("REPORTS_BUDGET_DAILY"):    cfg.budget_daily_eur = float(e("REPORTS_BUDGET_DAILY"))
    if e("REPORTS_BUDGET_MONTHLY"):  cfg.budget_monthly_eur = float(e("REPORTS_BUDGET_MONTHLY"))
    # LLM
    if e("REPORTS_LLM_ENABLED"):     cfg.llm.enabled = e("REPORTS_LLM_ENABLED") not in ("0", "false", "False", "")
    if e("REPORTS_LLM_PROVIDER"):    cfg.llm.provider = e("REPORTS_LLM_PROVIDER")
    if e("REPORTS_LLM_BASE_URL"):    cfg.llm.base_url = e("REPORTS_LLM_BASE_URL")
    if e("REPORTS_LLM_MODEL"):       cfg.llm.model = e("REPORTS_LLM_MODEL")
    if e("REPORTS_LLM_API_KEY"):     cfg.llm.api_key = e("REPORTS_LLM_API_KEY")
    elif e("OPENAI_API_KEY") and cfg.llm.provider == "openai":   cfg.llm.api_key = e("OPENAI_API_KEY")
    elif e("ANTHROPIC_API_KEY") and cfg.llm.provider == "anthropic": cfg.llm.api_key = e("ANTHROPIC_API_KEY")
    # SMTP
    if e("REPORTS_SMTP_HOST"):       cfg.smtp.host = e("REPORTS_SMTP_HOST")
    if e("REPORTS_SMTP_PORT"):       cfg.smtp.port = int(e("REPORTS_SMTP_PORT"))
    if e("REPORTS_SMTP_USER"):       cfg.smtp.user = e("REPORTS_SMTP_USER")
    if e("REPORTS_SMTP_PASSWORD"):   cfg.smtp.password = e("REPORTS_SMTP_PASSWORD")
    if e("REPORTS_SMTP_SENDER"):     cfg.smtp.sender = e("REPORTS_SMTP_SENDER")
    if e("REPORTS_SMTP_RECIPIENT"):  cfg.smtp.recipient = e("REPORTS_SMTP_RECIPIENT")


_cached: Config | None = None


def load_config(reload: bool = False) -> Config:
    global _cached
    if _cached is not None and not reload:
        return _cached
    cfg = Config()
    path = cfg.resolved_reports_dir() / "config.yaml"
    if yaml and path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                _apply_yaml(cfg, data)
        except Exception:
            pass
    _apply_env(cfg)
    cfg.approval_keywords = [k.lower() for k in cfg.approval_keywords]
    _cached = cfg
    return cfg


# ===================== kanban_source =====================
"""Read-only access to the Hermes kanban SQLite board.

Table names (tasks, task_events, task_comments + a runs table) are documented
and stable. Column names can drift across Hermes versions, so we resolve them
at runtime via PRAGMA table_info and allow explicit overrides in config.schema.

We open the DB read-only (mode=ro) over a file: URI so we never block the
dispatcher's writers (the board runs in WAL mode).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable



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


# ===================== insights_source =====================
"""Token usage + cost via `hermes insights`.

`hermes insights --days N` is the documented usage-analytics command. We try a
`--json` form first; if that isn't supported we regex-parse the box output as a
fallback (the totals line is stable enough).

CAVEAT (surfaced in the report): insights is keyed on *sessions*. Interactive
surfaces (tui/gateway) are reliably counted; autonomous dispatcher worker runs
may or may not be — verify on your install. When the runs table carries token
columns we prefer those for a true per-run figure (see KanbanSource).
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass, field


# short-lived memo so a 7-day bootstrap doesn't shell out to `hermes insights`
# 14+ times — the daily window repeats and results barely change minute to minute
_USAGE_CACHE: dict[int, tuple[float, "Usage"]] = {}
_USAGE_TTL = 120.0


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    sessions: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    source: str = "insights"
    caveat: str = ""


def _run(days: int, json_flag: bool) -> str | None:
    cmd = ["hermes", "insights", "--days", str(days)]
    if json_flag:
        cmd.append("--json")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None


def _parse_text(out: str) -> Usage:
    def num(pat: str) -> int:
        m = re.search(pat, out)
        return int(m.group(1).replace(",", "")) if m else 0

    u = Usage(
        input_tokens=num(r"Input tokens:\s*([\d,]+)"),
        output_tokens=num(r"Output tokens:\s*([\d,]+)"),
        total_tokens=num(r"Total tokens:\s*([\d,]+)"),
        sessions=num(r"Sessions:\s*([\d,]+)"),
    )
    for m in re.finditer(r"^\s*([\w.\-/]+)\s+\d+\s+([\d,]+)\s*$", out, re.MULTILINE):
        name = m.group(1)
        if name.lower() in ("model", "platform", "sessions"):
            continue
        u.by_model[name] = int(m.group(2).replace(",", ""))
    if not u.total_tokens:
        u.total_tokens = u.input_tokens + u.output_tokens
    return u


def fetch_usage(cfg: Config, days: int) -> Usage:
    hit = _USAGE_CACHE.get(days)
    if hit and (time.time() - hit[0]) < _USAGE_TTL:
        return hit[1]
    usage = _fetch_usage_uncached(cfg, days)
    _USAGE_CACHE[days] = (time.time(), usage)
    return usage


def _fetch_usage_uncached(cfg: Config, days: int) -> Usage:
    raw = _run(days, json_flag=True)
    if raw:
        try:
            data = json.loads(raw)
            u = Usage(
                input_tokens=int(data.get("input_tokens", data.get("inputTokens", 0)) or 0),
                output_tokens=int(data.get("output_tokens", data.get("outputTokens", 0)) or 0),
                total_tokens=int(data.get("total_tokens", data.get("totalTokens", 0)) or 0),
                sessions=int(data.get("sessions", 0) or 0),
                by_model={m.get("model", "?"): int(m.get("tokens", 0) or 0)
                          for m in data.get("models", []) if isinstance(m, dict)},
                source="insights --json",
            )
            if not u.total_tokens:
                u.total_tokens = u.input_tokens + u.output_tokens
            return u
        except Exception:
            pass
    raw = _run(days, json_flag=False)
    if raw:
        u = _parse_text(raw)
        u.source = "insights (text)"
        return u
    return Usage(source="unavailable", caveat="hermes insights returned no data")


def _price_for(model: str, pricing: dict) -> dict:
    ml = model.lower()
    for key, val in pricing.items():
        if key != "_default" and key.lower() in ml:
            return val
    return pricing.get("_default", {"input": 1.0, "output": 5.0})


def estimate_cost_eur(usage: Usage, cfg: Config) -> float:
    """Approximate € cost. Marked '≈' wherever it is rendered."""
    pricing = cfg.pricing
    # Prefer the input/output split applied at the dominant model's price.
    if usage.input_tokens or usage.output_tokens:
        model = max(usage.by_model, key=usage.by_model.get) if usage.by_model else "_default"
        pr = _price_for(model, pricing)
        return (usage.input_tokens / 1e6) * pr["input"] + (usage.output_tokens / 1e6) * pr["output"]
    # Else fall back to per-model totals with a blended in/out assumption (40% output).
    total = 0.0
    for model, toks in (usage.by_model or {"_default": usage.total_tokens}).items():
        pr = _price_for(model, pricing)
        total += (toks * 0.6 / 1e6) * pr["input"] + (toks * 0.4 / 1e6) * pr["output"]
    return total


# ===================== store =====================
"""Persistent overlay store for the reports plugin (its own SQLite db).

Three concerns:
  - summaries: AI/heuristic task summaries, cached by (task_id, last_event_id)
  - decisions: stateful "needs your hand" items that persist across days until
    you resolve them or their veto window expires
  - digests:   cached daily digest JSON, so weekly/monthly are pure roll-ups
"""

import json
import sqlite3
import time
from typing import Any


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


# ===================== escalate =====================
"""Deterministic escalation — RULES decide what reaches your hand, never the AI.

The AI only ever compresses wording later. Escalation is pure event-type logic
so it stays trustworthy and auditable:

  - blocked  + reason matches approval keywords  -> 'approval'  (waiting on you)
  - blocked  (generic)                           -> 'blocked'   (needs a look)
  - gave_up                                       -> 'failed'    (terminal failure)
  - >= N protocol_violation on one task in window -> 'instability'

Each decision gets a stable id (task_id + kind) so re-runs update rather than
duplicate, and resolved/expired items are never reopened.
"""


# event kinds we treat as terminal failures
_FAILED_KINDS = {"gave_up", "timed_out"}


def _reason_text(e: Event) -> str:
    d = e.data or {}
    for k in ("reason", "error", "message", "summary", "detail"):
        if d.get(k):
            return str(d[k])
    return ""


def _is_approval(reason: str, cfg: Config) -> bool:
    r = reason.lower()
    return any(kw in r for kw in cfg.approval_keywords)


def _short(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def escalate(events: list[Event], tasks: dict, cfg: Config) -> list[dict]:
    """Return decision dicts (id, task_id, kind, title, detail, deadline).

    One decision per task: the *latest* escalating state wins, since a gave_up
    that was retried and ended blocked-for-approval is the same incident, not
    two. The render layer adds the kind label, so titles stay clean (the task
    name). Instability is a fallback only when nothing more specific fired, and
    raw violation counts still feed the SYSTEM section upstream.
    """
    primary: dict[str, dict] = {}      # task_id -> latest specific decision
    violations: dict[str, int] = {}

    for e in events:
        t = tasks.get(e.task_id)
        task_title = t.title if t else e.task_id

        if e.kind == "blocked":
            reason = _reason_text(e)
            if _is_approval(reason, cfg):
                key = f"{e.task_id}:approval"
                primary[e.task_id] = {
                    "id": key, "task_id": e.task_id, "kind": "approval",
                    "title": task_title, "detail": _short(reason, 280),
                    "deadline": e.data.get("deadline") or e.data.get("expires"),
                }
            else:
                key = f"{e.task_id}:blocked"
                primary[e.task_id] = {
                    "id": key, "task_id": e.task_id, "kind": "blocked",
                    "title": task_title, "detail": _short(reason, 280), "deadline": None,
                }

        elif e.kind in _FAILED_KINDS:
            key = f"{e.task_id}:failed"
            default_detail = ("Gave up after retries." if cfg.language != "de"
                              else "Endgültig fehlgeschlagen nach Retries.")
            primary[e.task_id] = {
                "id": key, "task_id": e.task_id, "kind": "failed",
                "title": task_title,
                "detail": _short(_reason_text(e) or default_detail, 280),
                "deadline": None,
            }

        elif e.kind == "protocol_violation":
            violations[e.task_id] = violations.get(e.task_id, 0) + 1

    decisions = dict(primary)
    for task_id, n in violations.items():
        if n >= cfg.protocol_violation_alert_threshold and task_id not in primary:
            t = tasks.get(task_id)
            key = f"{task_id}:instability"
            detail = (f"{n}× protocol violation in window — worker exited without complete/block."
                      if cfg.language != "de"
                      else f"{n}× Protokollverletzung im Zeitraum — Worker stieg ohne complete/block aus.")
            decisions[task_id] = {
                "id": key, "task_id": task_id, "kind": "instability",
                "title": t.title if t else task_id,
                "detail": detail,
                "deadline": None,
            }

    return list(decisions.values())


# ===================== summarize =====================
"""Summarize one task's activity into <=2 crisp bullets.

The AI is *only* a compressor here. Escalation already decided what matters;
this turns a task's event/run/comment bundle into stichpunkt-kurze German text.

Cost discipline:
  - cache keyed on (task_id, last_event_id) — re-summarize only on new events
  - tiny max_tokens, low temperature
  - disabled by default; falls back to a deterministic extract from the
    `completed` summary / `blocked` reason (which are already informative)

Providers: OpenAI-compatible chat/completions, or Anthropic messages. Uses
stdlib urllib so the plugin pulls in no extra dependencies.
"""

import json
import urllib.request


def _sys(lang: str) -> str:
    out_lang = "German" if lang == "de" else "English"
    return (
        "You summarize the activity of ONE kanban task for a daily briefing. "
        "Respond ONLY with JSON: {\"outcome\": str, \"why\": str, "
        "\"waiting_on\": str|null, \"bullets\": [str, ...]}. "
        f"At most 2 bullets, each <= 12 words, in {out_lang}, terse, no preamble, no filler. "
        "outcome = what happened (e.g. 'done', 'blocked', 'gave up'). "
        "why = the single reason in <= 12 words. waiting_on = what it waits on, or null."
    )


def _last_text(bundle: dict, kinds: tuple[str, ...]) -> str:
    for ev in reversed(bundle.get("events", [])):
        if ev.get("kind") in kinds:
            d = ev.get("data") or {}
            for k in ("summary", "reason", "error", "message"):
                if d.get(k):
                    return str(d[k])
    # try runs
    for r in bundle.get("runs", []):
        for k in ("summary", "error"):
            if r.get(k):
                return str(r[k])
    return ""


def _fallback(bundle: dict, lang: str = "en") -> dict:
    kinds = [e.get("kind") for e in bundle.get("events", [])]
    done = "completed" in kinds
    blocked = "blocked" in kinds
    failed = any(k in kinds for k in ("gave_up", "timed_out"))
    words = ({"done": "fertig", "gave up": "aufgegeben", "blocked": "blockiert", "wip": "in Arbeit"}
             if lang == "de" else
             {"done": "done", "gave up": "gave up", "blocked": "blocked", "wip": "in progress"})
    outcome = words["done"] if done else words["gave up"] if failed else words["blocked"] if blocked else words["wip"]
    why = _last_text(bundle, ("completed",)) if done else \
          _last_text(bundle, ("blocked", "gave_up", "timed_out"))
    why = " ".join(why.split())
    if len(why) > 140:
        why = why[:139] + "…"
    waiting = ""
    if blocked and not done:
        bt = _last_text(bundle, ("blocked",)).lower()
        if any(w in bt for w in ("freigabe", "boss", "wartet", "approval", "review")):
            waiting = "Freigabe" if lang == "de" else "approval"
    bullets = [why] if why else [outcome]
    return {"outcome": outcome, "why": why, "waiting_on": waiting, "bullets": bullets}


def _compact_bundle(bundle: dict) -> str:
    lines = []
    for ev in bundle.get("events", [])[-12:]:
        d = ev.get("data") or {}
        bit = d.get("summary") or d.get("reason") or d.get("error") or ""
        lines.append(f"- {ev.get('kind')}: {str(bit)[:240]}")
    for c in bundle.get("comments", [])[-2:]:
        body = c.get("body") or c.get("text") or c.get("content") or ""
        if body:
            lines.append(f"- comment: {str(body)[:240]}")
    return "\n".join(lines) or "(keine Details)"


def _call_openai(cfg: Config, prompt: str) -> str:
    body = json.dumps({
        "model": cfg.llm.model,
        "messages": [{"role": "system", "content": _sys(cfg.language)}, {"role": "user", "content": prompt}],
        "max_tokens": cfg.llm.max_tokens, "temperature": cfg.llm.temperature,
    }).encode()
    req = urllib.request.Request(
        cfg.llm.base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.llm.api_key}"})
    with urllib.request.urlopen(req, timeout=cfg.llm.timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


def _call_anthropic(cfg: Config, prompt: str) -> str:
    body = json.dumps({
        "model": cfg.llm.model, "max_tokens": cfg.llm.max_tokens,
        "temperature": cfg.llm.temperature, "system": _sys(cfg.language),
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        cfg.llm.base_url.rstrip("/") + "/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": cfg.llm.api_key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=cfg.llm.timeout) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _parse_json(text: str) -> dict:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def summarize_task(bundle: dict, cfg: Config, store: Store) -> dict:
    task_id = bundle["task_id"]
    last_id = bundle.get("last_event_id", 0)
    cached = store.get_summary(task_id, last_id)
    if cached:
        return {
            "outcome": cached["outcome"], "why": cached["why"],
            "waiting_on": cached["waiting_on"],
            "bullets": json.loads(cached["bullets"] or "[]"),
        }

    result = _fallback(bundle, cfg.language)
    if cfg.llm.enabled and cfg.llm.api_key:
        try:
            prompt = f"Task {task_id}. Activity:\n{_compact_bundle(bundle)}"
            raw = _call_anthropic(cfg, prompt) if cfg.llm.provider == "anthropic" else _call_openai(cfg, prompt)
            parsed = _parse_json(raw)
            result = {
                "outcome": str(parsed.get("outcome", result["outcome"]))[:40],
                "why": str(parsed.get("why", result["why"]))[:160],
                "waiting_on": (parsed.get("waiting_on") or "") if parsed.get("waiting_on") else "",
                "bullets": [str(b)[:120] for b in (parsed.get("bullets") or [])][:2],
            }
        except Exception:
            pass  # keep deterministic fallback

    store.put_summary(task_id, last_id, result)
    return result


# ===================== aggregate =====================
"""Build the structured digest — one JSON artifact per day.

The digest is the single source of truth; both the Markdown renderer and the
dashboard tab consume it. Weekly/monthly reports are roll-ups of daily digests,
so the heavy work (event diff + summarize) happens once per day.

Digest shape:
{
  "date": "2026-06-26", "range": "day", "generated_at": 1750000000,
  "header": {"status": "active"|"quiet", "open": int, "cost_eur": float, "budget_eur": float},
  "hand":   [ {kind, title, detail, task_id, deadline} ],        # needs your hand
  "in_progress": [ {task_id, title, status} ],
  "done":   [ {task_id, title, bullets:[...], why} ],
  "learned":[ str ],                                             # from comments (optional)
  "cost":   {today_eur, month_eur, budget_daily, budget_monthly, runs, tokens, source, caveat, approx},
  "system": {"stable": bool, "notes": [str]},
  "decision_stats": {total, resolved, vetoed, expired, open}     # for weekly/monthly
}
"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


_ERROR_KINDS = {"protocol_violation", "crashed", "spawn_failed", "timed_out", "gave_up"}
_DONE_KINDS = {"completed"}
_ACTIVE_STATUS = {"running", "ready", "claimed", "todo"}

_STATUS_WORD = {"en": {"active": "active", "quiet": "quiet"},
                "de": {"active": "läuft", "quiet": "ruhig"}}
_CAVEAT = {
    "en": "insights counts mostly interactive sessions; worker runs may be missing.",
    "de": "insights zählt v.a. interaktive Sessions; Worker-Runs evtl. nicht erfasst.",
}


def day_bounds(date_str: str, tz: str) -> tuple[int, int, datetime]:
    z = ZoneInfo(tz)
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=z)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp()), start


def today_str(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


def _month_start_ts(date_str: str, tz: str) -> int:
    z = ZoneInfo(tz)
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=z, day=1, hour=0, minute=0, second=0)
    return int(d.timestamp())


def build_digest(cfg: Config, date_str: str, persist: bool = True, mark: bool = True) -> dict:
    start_ts, end_ts, _ = day_bounds(date_str, cfg.timezone)
    src = KanbanSource(cfg)
    store = Store(cfg)
    lang = cfg.language if cfg.language in _STATUS_WORD else "en"
    if mark:
        store.build_begin(f"Building briefing for {date_str}", 1)
    try:
        store.expire_due()
        events = src.fetch_events(start_ts, end_ts)
        tasks = src.fetch_tasks()  # snapshot of current statuses

        # 1) deterministic escalation -> persistent decisions, then reconcile
        for d in escalate(events, tasks, cfg):
            store.upsert_decision(d)
        store.reconcile_decisions(tasks)
        hand = store.open_decisions()

        # 2) completed today -> AI/heuristic summary
        done_ids = {e.task_id for e in events if e.kind in _DONE_KINDS}
        done = []
        for tid in done_ids:
            bundle = src.task_bundle(tid, events)
            s = summarize_task(bundle, cfg, store)
            t = tasks.get(tid)
            done.append({"task_id": tid, "title": t.title if t else tid,
                         "bullets": s["bullets"], "why": s["why"]})

        # 3) in progress now (snapshot, not window)
        in_progress = [
            {"task_id": t.id, "title": t.title, "status": t.status}
            for t in tasks.values() if t.status in _ACTIVE_STATUS
        ][:25]

        # 4) cost / usage
        usage = fetch_usage(cfg, days=1)
        usage_month = fetch_usage(cfg, days=_days_into_month(date_str, cfg.timezone))
        today_eur = estimate_cost_eur(usage, cfg)
        month_eur = estimate_cost_eur(usage_month, cfg)
        runs = len([e for e in events if e.kind in ("claimed", "spawned")])
        caveat = _CAVEAT[lang] if not src.has_run_token_columns() else ""
        cost = {
            "today_eur": round(today_eur, 2), "month_eur": round(month_eur, 2),
            "budget_daily": cfg.budget_daily_eur, "budget_monthly": cfg.budget_monthly_eur,
            "runs": runs, "tokens": usage.total_tokens, "source": usage.source,
            "caveat": caveat or usage.caveat, "approx": True,
        }

        # 5) system health from error events
        err = [e for e in events if e.kind in _ERROR_KINDS]
        notes = []
        if err:
            by_kind: dict[str, int] = {}
            for e in err:
                by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
            notes = [f"{n}× {k}" for k, n in by_kind.items()]
        system = {"stable": not err, "notes": notes}

        # 6) learned (optional, lightweight: pull short comment lines tagged as notes)
        learned = _extract_learned(src, events)

        status = (_STATUS_WORD[lang]["active"] if hand or done
                  else _STATUS_WORD[lang]["quiet"])
        digest = {
            "date": date_str, "range": "day", "generated_at": int(time.time()),
            "header": {"status": status, "open": len(hand),
                       "cost_eur": round(today_eur, 2), "budget_eur": cfg.budget_daily_eur},
            "hand": [_decision_view(d) for d in hand],
            "in_progress": in_progress, "done": done, "learned": learned,
            "cost": cost, "system": system,
            "decision_stats": store.decision_stats(_month_start_ts(date_str, cfg.timezone)),
        }
        if persist:
            store.put_digest(date_str, digest)
        return digest
    finally:
        if mark:
            store.build_finish()
        src.close()
        store.close()


def _days_into_month(date_str: str, tz: str) -> int:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.day


def _decision_view(d: dict) -> dict:
    return {"id": d["id"], "kind": d["kind"], "title": d["title"],
            "detail": d.get("detail", ""), "task_id": d.get("task_id"),
            "deadline": d.get("deadline")}


def _extract_learned(src: KanbanSource, events: list[Event]) -> list[str]:
    """Very light: surface short 'notiert'/'learned'-style comment lines, if any."""
    out: list[str] = []
    seen = set()
    for tid in {e.task_id for e in events if e.kind == "commented"}:
        for c in src.fetch_comments(tid):
            body = (c.get("body") or c.get("text") or c.get("content") or "").strip()
            low = body.lower()
            if any(tag in low for tag in ("gelernt", "learned", "notiert", "erkenntnis", "lesson")):
                line = " ".join(body.split())
                for tag in ("Notiert:", "notiert:", "Gelernt:", "gelernt:", "Learned:", "learned:", "Erkenntnis:"):
                    if line.startswith(tag):
                        line = line[len(tag):].strip()
                        break
                line = line[:120]
                if line and line not in seen:
                    seen.add(line)
                    out.append(line)
    return out[:5]


def build_recent(cfg: Config, days: int = 7) -> dict:
    """Build the last `days` daily digests (oldest->newest), skipping ones already
    cached except today (always refreshed). Manages the shared build status so the
    dashboard can show progress. Safe to call on first open."""
    z = ZoneInfo(cfg.timezone)
    today = datetime.now(z).date()
    # newest first: today, yesterday, ... so the most relevant briefing shows first
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    store = Store(cfg)
    built, skipped = [], []
    try:
        if store.build_status().get("running"):
            return {"started": False, "reason": "already running", **store.build_status()}
        store.build_begin("Today", len(dates))
        for i, ds in enumerate(dates, 1):
            store.build_step(ds, i - 1)
            is_today = i == 1
            if not is_today and store.get_digest(ds):
                skipped.append(ds)
            else:
                build_digest(cfg, ds, mark=False)   # don't touch overall status
                built.append(ds)
            store.build_step(ds, i)
        store.build_finish()
        return {"started": True, "built": built, "skipped": skipped}
    except Exception as e:  # pragma: no cover
        store.build_finish(error=str(e))
        return {"started": True, "error": str(e), "built": built}
    finally:
        store.close()


def next_run(cfg: Config) -> dict:
    """Compute the next scheduled build time from cfg.schedule (local HH:MM list)."""
    z = ZoneInfo(cfg.timezone)
    now = datetime.now(z)
    candidates = []
    for hm in cfg.schedule:
        try:
            hh, mm = [int(x) for x in str(hm).split(":")[:2]]
        except Exception:
            continue
        for day_offset in (0, 1):
            cand = (now + timedelta(days=day_offset)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand > now:
                candidates.append(cand)
                break
    if not candidates:
        return {"epoch": None, "iso": None}
    nxt = min(candidates)
    return {"epoch": int(nxt.timestamp()), "iso": nxt.isoformat(), "schedule": cfg.schedule}


def build_range(cfg: Config, from_date: str, to_date: str) -> dict:
    """Roll up daily digests into a weekly/monthly view (builds missing days)."""
    z = ZoneInfo(cfg.timezone)
    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=z)
    end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=z)
    store = Store(cfg)
    days, cur = [], start
    try:
        while cur <= end:
            ds = cur.strftime("%Y-%m-%d")
            d = store.get_digest(ds) or build_digest(cfg, ds)
            days.append(d)
            cur += timedelta(days=1)
        cost_sum = round(sum(d["cost"]["today_eur"] for d in days), 2)
        done = [item for d in days for item in d["done"]]
        hand_open = days[-1]["hand"] if days else []
        learned = list({l for d in days for l in d.get("learned", [])})
        stats = days[-1]["decision_stats"] if days else {}
        return {
            "range": "custom", "from": from_date, "to": to_date,
            "generated_at": int(time.time()),
            "cost_eur": cost_sum, "done": done, "hand": hand_open,
            "learned": learned, "decision_stats": stats,
            "days": [{"date": d["date"], "cost": d["cost"]["today_eur"],
                      "done": len(d["done"]), "open": len(d["hand"])} for d in days],
        }
    finally:
        store.close()


# ===================== render_md =====================
"""Render a digest to crisp, bullet-style Markdown (English by default).

Quiet days collapse to a single header line. Everything stays short. Set
`language: de` in config for the original German labels.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

LABELS = {
    "en": {
        "open": "open", "nothing_open": "nothing open", "hand": "YOUR CALL", "log": "LOG",
        "done": "Done", "wip": "Active", "noted": "Noted",
        "cost": "Cost", "system": "System", "stable": "stable",
        "all_clear": " · all clear", "near": "  ← near budget limit",
        "today": "today", "month": "month", "runs": "runs",
        "veto_window": "Veto window", "report": "Report", "still_open": "STILL OPEN",
        "finished": "DONE", "learned": "LEARNED", "decisions": "decisions",
        "vetoed": "vetoed", "expired": "expired",
        "kind": {"approval": "Needs approval", "blocked": "Blocked",
                 "failed": "Gave up", "instability": "Unstable"},
    },
    "de": {
        "open": "offen", "nothing_open": "nichts offen", "hand": "DEINE HAND", "log": "PROTOKOLL",
        "done": "Fertig", "wip": "In Arbeit", "noted": "Notiert",
        "cost": "Kosten", "system": "System", "stable": "stabil",
        "all_clear": " · alles in Ordnung", "near": "  ← knapp am Limit",
        "today": "heute", "month": "Monat", "runs": "Runs",
        "veto_window": "Stopp-Fenster", "report": "Bericht", "still_open": "NOCH OFFEN",
        "finished": "ERLEDIGT", "learned": "GELERNT", "decisions": "Entscheidungen",
        "vetoed": "vetot", "expired": "abgelaufen",
        "kind": {"approval": "Freigabe nötig", "blocked": "Blockiert",
                 "failed": "Aufgegeben", "instability": "Instabil"},
    },
}


def _L(lang: str) -> dict:
    return LABELS.get(lang, LABELS["en"])


def render_day(digest: dict, tz: str = "Europe/Berlin", lang: str = "en") -> str:
    L = _L(lang)
    h = digest["header"]
    cost = digest["cost"]
    date = digest["date"]
    dd = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m") if date else date

    open_n = h["open"]
    open_str = f"{open_n} {L['open']}" if open_n else L["nothing_open"]
    head = f"{dd} · {h['status']} · {open_str} · ≈ {cost['today_eur']:.2f} € / {cost['budget_daily']:.0f} €"

    if not digest["hand"] and not digest["done"]:
        tail = L["all_clear"] if digest["system"]["stable"] else ""
        return f"{head}{tail}\n"

    lines = [head, ""]

    if digest["hand"]:
        lines.append(f"▸ {L['hand']}")
        for d in digest["hand"]:
            label = L["kind"].get(d["kind"], "Attention")
            lines.append(f"  • {label}: {d['title']}")
            if d.get("detail"):
                lines.append(f"    {d['detail']}")
            if d.get("deadline"):
                dl = _fmt_deadline(d["deadline"], tz)
                if dl:
                    lines.append(f"    {L['veto_window']}: {dl}")
        lines.append("")

    lines.append(f"▸ {L['log']}")
    if digest["done"]:
        for item in digest["done"]:
            b = item["bullets"][0] if item["bullets"] else (item.get("why") or "done")
            lines.append(f"  {L['done']:<9} {item['title']} — {b}")
    if digest["in_progress"]:
        names = ", ".join(t["title"] for t in digest["in_progress"][:6])
        more = f" (+{len(digest['in_progress']) - 6})" if len(digest["in_progress"]) > 6 else ""
        lines.append(f"  {L['wip']:<9} {names}{more}")
    for l in digest.get("learned", []):
        lines.append(f"  {L['noted']:<9} {l}")

    cd = cost["today_eur"] / cost["budget_daily"] if cost["budget_daily"] else 0
    near = L["near"] if cd >= 0.8 else ""
    lines.append(
        f"  {L['cost']:<9} {L['today']} ≈{cost['today_eur']:.2f} €/{cost['budget_daily']:.0f} € · "
        f"{L['month']} ≈{cost['month_eur']:.2f} €/{cost['budget_monthly']:.0f} € · {cost['runs']} {L['runs']}{near}"
    )
    if cost.get("caveat"):
        lines.append(f"            ⚠ {cost['caveat']}")

    if digest["system"]["stable"]:
        lines.append(f"  {L['system']:<9} {L['stable']}")
    else:
        lines.append(f"  {L['system']:<9} " + ", ".join(digest["system"]["notes"]))

    return "\n".join(lines) + "\n"


def render_range(roll: dict, title: str | None = None, lang: str = "en") -> str:
    L = _L(lang)
    title = title or L["report"]
    lines = [f"{title} · {roll['from']} – {roll['to']}", ""]
    lines.append(f"  {L['cost']:<9} ≈ {roll['cost_eur']:.2f} € total")
    lines.append(f"  {L['done']:<9} {len(roll['done'])} tasks")
    st = roll.get("decision_stats", {})
    if st:
        lines.append(
            f"  {L['hand'].title():<9} {st.get('total', 0)} {L['decisions']} · "
            f"{st.get('vetoed', 0)} {L['vetoed']} · {st.get('expired', 0)} {L['expired']} · "
            f"{st.get('open', 0)} {L['open']}"
        )
    if roll["hand"]:
        lines += ["", f"▸ {L['still_open']}"]
        for d in roll["hand"]:
            lines.append(f"  • {d['title']}")
    if roll["done"]:
        lines += ["", f"▸ {L['finished']}"]
        for item in roll["done"][:20]:
            b = item["bullets"][0] if item.get("bullets") else item.get("why", "")
            lines.append(f"  • {item['title']} — {b}")
    if roll.get("learned"):
        lines += ["", f"▸ {L['learned']}"]
        for l in roll["learned"][:10]:
            lines.append(f"  • {l}")
    return "\n".join(lines) + "\n"


def _fmt_deadline(deadline, tz: str) -> str:
    try:
        v = float(deadline)
        if v > 1e12:
            v /= 1000
        dt = datetime.fromtimestamp(v, ZoneInfo(tz))
        return dt.strftime("%a %H:%M")
    except Exception:
        return str(deadline) if deadline else ""


# ---------------------------------------------------------------- routes

if router is not None:

    @router.get("/health")
    def health():
        cfg = load_config()
        return {"ok": True, "kanban_db": str(cfg.resolved_kanban_db()),
                "db_exists": cfg.resolved_kanban_db().exists(),
                "llm_enabled": cfg.llm.enabled}

    @router.get("/schema")
    def schema():
        src = KanbanSource(load_config())
        try:
            return src.inspect_schema()
        finally:
            src.close()

    @router.get("/digests")
    def digests(limit: int = 60):
        store = Store(load_config())
        try:
            return {"digests": store.list_digests(limit)}
        finally:
            store.close()

    # NOTE: these are sync `def` on purpose — FastAPI runs them in a threadpool,
    # so building (which is blocking) never stalls the dashboard event loop.
    def _resolve_date(cfg, date: str) -> str:
        return today_str(cfg.timezone) if date in ("today", "heute") else date

    @router.get("/digest/{date}")
    def digest(date: str, rebuild: bool = False):
        cfg = load_config()
        date = _resolve_date(cfg, date)
        store = Store(cfg)
        try:
            cached = None if rebuild else store.get_digest(date)
        finally:
            store.close()
        return cached or build_digest(cfg, date)   # build on demand

    @router.get("/render/{date}", response_class=PlainTextResponse)
    def render(date: str, rebuild: bool = False):
        cfg = load_config()
        date = _resolve_date(cfg, date)
        store = Store(cfg)
        try:
            d = None if rebuild else store.get_digest(date)
        finally:
            store.close()
        d = d or build_digest(cfg, date)
        return render_day(d, cfg.timezone, cfg.language)

    @router.get("/range")
    def range_(from_: str, to: str):
        return build_range(load_config(), from_, to)   # builds missing days on demand

    @router.get("/ensure")
    def ensure(days: int = 7):
        """Build the last `days` daily digests (today first). Safe to call on open."""
        return build_recent(load_config(), days)

    @router.get("/status")
    def status():
        cfg = load_config()
        store = Store(cfg)
        try:
            bs = store.build_status()
        finally:
            store.close()
        return {"build": bs, "next_run": next_run(cfg), "schedule": cfg.schedule,
                "timezone": cfg.timezone}

    @router.post("/build")
    def build(body: dict = Body(default={})):
        cfg = load_config()
        body = body or {}
        if body.get("days"):
            return build_recent(cfg, int(body["days"]))
        date = body.get("date") or today_str(cfg.timezone)
        return build_digest(cfg, date)

    @router.get("/decisions")
    def decisions():
        store = Store(load_config())
        try:
            return {"decisions": store.open_decisions()}
        finally:
            store.close()

    def _do_resolve(decision_id: str, resolution: str):
        store = Store(load_config())
        try:
            ok = store.resolve_decision(decision_id, resolution)
        finally:
            store.close()
        if not ok:
            raise HTTPException(404, "decision not open or not found")
        return {"ok": True, "id": decision_id, "resolution": resolution}

    @router.post("/decisions/{decision_id}/resolve")
    def resolve_post(decision_id: str, body: dict = Body(default={})):
        return _do_resolve(decision_id, (body or {}).get("resolution", "ok"))

    # GET alias — some Hermes builds' fetchJSON doesn't forward POST bodies/method,
    # so the UI uses this to resolve decisions reliably.
    @router.get("/decisions/{decision_id}/resolve")
    def resolve_get(decision_id: str, resolution: str = "ok"):
        return _do_resolve(decision_id, resolution)


# ------------------------------------------------------------------ CLI

def _cli(argv: list[str]) -> int:
    cfg = load_config()
    cmd = argv[0] if argv else "render"

    if cmd == "inspect-schema":
        import json
        src = KanbanSource(cfg)
        try:
            print(json.dumps(src.inspect_schema(), indent=2, default=str))
        finally:
            src.close()
        return 0

    if cmd == "render":
        date = argv[1] if len(argv) > 1 else today_str(cfg.timezone)
        print(render_day(build_digest(cfg, date), cfg.timezone, cfg.language))
        return 0

    if cmd == "bootstrap":
        days = int(argv[1]) if len(argv) > 1 else 7
        out = build_recent(cfg, days)
        print(f"built: {out.get('built')}  skipped: {out.get('skipped')}")
        return 0

    if cmd == "build":
        import json
        date = argv[1] if len(argv) > 1 else today_str(cfg.timezone)
        print(json.dumps(build_digest(cfg, date), indent=2, ensure_ascii=False, default=str))
        return 0

    if cmd == "range":
        if len(argv) < 3:
            print("usage: range FROM TO  (YYYY-MM-DD YYYY-MM-DD)")
            return 2
        print(render_range(build_range(cfg, argv[1], argv[2]), lang=cfg.language))
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
