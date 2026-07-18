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

# Best-effort: ship the IANA tz database so zoneinfo works even on slim images
# that lack the system tzdata package. No-op if not installed.
try:
    import tzdata  # noqa: F401
except Exception:
    pass


def _safe_zoneinfo(name: str):
    """ZoneInfo(name) but never raises — falls back to UTC if the tz DB is
    missing (slim containers without system tzdata). Keeps the plugin working
    on a bare server; times are then UTC instead of the configured zone."""
    try:
        return ZoneInfo(name)
    except Exception:
        try:
            return ZoneInfo("UTC")
        except Exception:
            return timezone.utc


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


# Default $ (USD) price per 1,000,000 tokens, keyed by model name (substring match).
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
    # Explicit extra sources for profile-scoped dashboards. Each root contains
    # <slug>/kanban.db; each path is one allowed board DB. `kanban_db` remains
    # a single-board override and takes precedence over both lists.
    external_board_roots: list[Path] = field(default_factory=list)
    external_board_dbs: list[Path] = field(default_factory=list)
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


def _path_list(value: Any) -> list[Path]:
    """Normalize a YAML/env source list without accepting implicit locations."""
    if not isinstance(value, list):
        value = [value] if value else []
    return [Path(str(p)).expanduser() for p in value if str(p).strip()]


def _apply_yaml(cfg: Config, data: dict[str, Any]) -> None:
    for k in ("timezone", "language", "kanban_db", "reports_dir"):
        if data.get(k) is not None:
            setattr(cfg, k, data[k])
    if "external_board_roots" in data:
        cfg.external_board_roots = _path_list(data["external_board_roots"])
    if "external_board_dbs" in data:
        cfg.external_board_dbs = _path_list(data["external_board_dbs"])
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
    if roots := e("REPORTS_EXTERNAL_BOARD_ROOTS"):
        cfg.external_board_roots = _path_list(roots.split(os.pathsep))
    if dbs := e("REPORTS_EXTERNAL_BOARD_DBS"):
        cfg.external_board_dbs = _path_list(dbs.split(os.pathsep))
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
from dataclasses import dataclass, replace
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
    "task_created_by": ["created_by", "creator", "author", "by", "created_by_user"],
    "task_workspace": ["workspace", "workspace_path", "scratch", "repo", "worktree"],

    "run_id":       ["id", "run_id"],
    "run_task":     ["task_id", "task"],
    "run_outcome":  ["outcome", "status", "result"],
    "run_profile":  ["profile", "assignee", "agent"],
    "run_summary":  ["summary", "result", "handoff"],
    "run_error":    ["error", "err", "message"],
    "run_started":  ["started_at", "created_at", "start", "started"],
    "run_ended":    ["ended_at", "finished_at", "end", "ended"],
    "run_duration": ["duration", "duration_s", "duration_ms", "elapsed", "elapsed_ms", "latency_ms", "took_ms"],
    "run_in_tok":   ["input_tokens", "prompt_tokens", "tokens_in", "in_tokens"],
    "run_out_tok":  ["output_tokens", "completion_tokens", "tokens_out", "out_tokens"],
    "run_cost":     ["cost", "cost_eur", "cost_usd", "price"],
    "run_model":    ["model", "model_name", "llm", "llm_model", "engine", "model_id"],
    "run_thinking": ["thinking", "thinking_mode", "reasoning", "reasoning_effort", "extended_thinking"],
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
    body: str = ""
    created_by: str = ""
    workspace: str = ""


# --------------------------------------------------------------------------- #
# schema-agnostic status classification
#
# We never hard-code a board's exact event names. Any event/status is mapped to
# a canonical bucket (done / blocked / failed / active / todo) by keyword-
# matching BOTH the event `kind` and any status-like field in its JSON payload.
# Renamed or future schemas keep working as long as the words resemble normal
# status vocabulary; unrecognised events simply yield no bucket and are ignored
# rather than breaking the report.
# --------------------------------------------------------------------------- #
_FAIL_WORDS = ("fail", "gave_up", "giveup", "give_up", "timed_out", "timeout",
               "abort", "crash", "cancel", "reject", "spawn_failed", "error")
_BLOCK_WORDS = ("block", "review", "approv", "wait", "hold", "stuck")
_DONE_WORDS = ("done", "complete", "finish", "close", "resolve", "merg",
               "ship", "deploy", "archiv", "success", "succeed")
_ACTIVE_WORDS = ("run", "progress", "doing", "claim", "start", "active",
                 "working", "spawn")
_TODO_WORDS = ("todo", "ready", "backlog", "open", "new", "queue", "pending", "draft")

_STATUS_FIELDS = ("to", "to_status", "to_state", "new_status", "new_state",
                  "to_column", "to_lane", "status", "state", "column", "lane",
                  "stage", "phase", "value")

_TEXT_FIELDS = ("summary", "reason", "message", "detail", "note", "comment",
                "body", "text", "description", "result", "output", "error")


def _canon(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").strip().lower()).strip("_")


def _bucket_of(token: Any) -> Optional[str]:
    """Map one status/kind token to a canonical bucket, or None if unknown.

    Order matters: a clearing (unblock/resume) reads as active, and failure /
    blocked are checked before done/active so 'review' or 'failed' never reads
    as 'done'."""
    t = _canon(token)
    if not t:
        return None
    if "unblock" in t or "unhold" in t or "resume" in t or "reopen" in t:
        return "active"
    if any(w in t for w in _FAIL_WORDS):
        return "failed"
    if any(w in t for w in _BLOCK_WORDS):
        return "blocked"
    if any(w in t for w in _DONE_WORDS):
        return "done"
    if any(w in t for w in _ACTIVE_WORDS):
        return "active"
    if any(w in t for w in _TODO_WORDS):
        return "todo"
    return None


def _bucket_ev(kind: Any, data: Any) -> Optional[str]:
    """Canonical transition for an event: prefer an explicit status-like field
    in the payload (the real transition target), else read the kind as a verb."""
    d = data if isinstance(data, dict) else {}
    for f in _STATUS_FIELDS:
        v = d.get(f)
        if v not in (None, "", [], {}):
            b = _bucket_of(v)
            if b:
                return b
    return _bucket_of(kind)


def event_bucket(e: "Event") -> Optional[str]:
    return _bucket_ev(e.kind, e.data)


def _any_text(d: Any, extra: tuple = ()) -> str:
    """Most informative human string from a payload, schema-agnostic."""
    if isinstance(d, str):
        return d
    if not isinstance(d, dict):
        return str(d or "")
    for k in _TEXT_FIELDS + extra:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    best = ""
    for v in d.values():
        if isinstance(v, str) and len(v) > len(best):
            best = v
    return best


def _looks_like_color(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip().lower()
    if not s:
        return False
    return (s.startswith("#") or s.startswith("rgb") or s.startswith("hsl")
            or bool(re.match(r"^[a-z]+$", s)))   # named css color


def _walk_colors(obj: Any, out: dict) -> None:
    if isinstance(obj, dict):
        nm = (obj.get("name") or obj.get("label") or obj.get("status")
              or obj.get("title") or obj.get("key") or obj.get("id") or obj.get("slug"))
        col = obj.get("color") or obj.get("colour") or obj.get("hex")
        if nm and _looks_like_color(col):
            out.setdefault(_canon(nm), col.strip())
        for v in obj.values():
            _walk_colors(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_colors(v, out)


def _harvest_json_colors(text: Any, out: dict) -> None:
    """Walk a JSON string (or fragment) collecting {name/label/status: color}."""
    if not isinstance(text, str) or "color" not in text.lower():
        return
    try:
        _walk_colors(json.loads(text), out)
    except Exception:
        return


def _scan_files_for_colors(paths, out: dict) -> None:
    """Parse JSON/YAML config files for status/column color definitions."""
    for fp in paths:
        try:
            if not fp.is_file() or fp.stat().st_size > 2_000_000:
                continue
            text = fp.read_text(encoding="utf-8", errors="ignore")
            if "color" not in text.lower():
                continue
            obj = None
            try:
                obj = json.loads(text)
            except Exception:
                try:
                    import yaml  # PyYAML is a dependency
                    obj = yaml.safe_load(text)
                except Exception:
                    obj = None
            if obj is not None:
                _walk_colors(obj, out)
        except Exception:
            continue


_HEADER_ALIASES = {
    "department": ("department", "dept", "abteilung"),
    "capability": ("capability", "cap"),
    "service": ("service",),
    "output": ("output",),
    "depends_on": ("depends_on", "depends-on", "depends", "dependson"),
}


def parse_body_header(body: Any) -> dict:
    """Parse the WFDE task body-header convention (Department/Capability/Service/
    Output/Depends-on). Returns parsed fields + header_present flag."""
    res = {"department": None, "capability": None, "service": None,
           "output": None, "depends_on": [], "header_present": False}
    if not body:
        return res
    for line in str(body).splitlines()[:25]:
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        k = _canon(key)
        v = val.strip()
        for field, aliases in _HEADER_ALIASES.items():
            if k in tuple(_canon(a) for a in aliases):
                res["header_present"] = True
                if field == "depends_on":
                    res[field] = [x.strip() for x in re.split(r"[,\s]+", v)
                                  if x.strip() and x.strip().lower() != "none"]
                else:
                    res[field] = None if v.lower() in ("none", "-", "") else v
    return res


def extract_markers(text: Any) -> set:
    """Find WFDE gate/approval markers in body or comment text."""
    t = str(text or "").upper()
    m = set()
    if re.search(r"QS-?A", t): m.add("QS-A")
    if re.search(r"QS-?B", t): m.add("QS-B")
    if re.search(r"QS-?C", t): m.add("QS-C")
    if "BOSS-GATE" in t or "BOSS GATE" in t or "BOSS-FREIGABE" in t: m.add("BOSS-GATE")
    if "CEO-VERIFIED" in t or "CEO VERIFIED" in t or "CEO-VERIFIZIERT" in t: m.add("CEO-VERIFIED")
    if "APPROVAL-PENDING" in t or "APPROVAL PENDING" in t or "NEEDS APPROVAL" in t or "PRODUKTIVFREIGABE" in t: m.add("APPROVAL-PENDING")
    if "BLOCKED" in t: m.add("BLOCKED")
    return m


_GATE_MARKERS = {"QS-A", "QS-B", "QS-C", "BOSS-GATE", "CEO-VERIFIED"}


def profile_meta(cfg: Config) -> dict:
    """Map profile name -> {model, thinking} by scanning profile config files
    under <hermes_home>/profiles/<name>/. Best-effort and schema-agnostic."""
    out: dict = {}
    try:
        import yaml
    except Exception:
        yaml = None
    base = cfg.hermes_home / "profiles"
    if not base.is_dir():
        return out

    def find(d: dict, keys):
        for k in keys:
            for kk in d:
                if kk.lower() == k:
                    v = d[kk]
                    if isinstance(v, (str, int, float, bool)):
                        return str(v)
        for v in d.values():
            if isinstance(v, dict):
                r = find(v, keys)
                if r:
                    return r
        return None

    for pdir in base.iterdir():
        if not pdir.is_dir():
            continue
        meta = {}
        for fp in list(pdir.glob("*.json")) + list(pdir.glob("*.yaml")) + list(pdir.glob("*.yml")):
            try:
                if fp.stat().st_size > 2_000_000:
                    continue
                text = fp.read_text(encoding="utf-8", errors="ignore")
                obj = None
                try:
                    obj = json.loads(text)
                except Exception:
                    obj = yaml.safe_load(text) if yaml else None
                if isinstance(obj, dict):
                    meta.setdefault("model", find(obj, ("model", "model_name", "llm", "llm_model", "engine")))
                    meta.setdefault("thinking", find(obj, ("thinking", "thinking_mode", "reasoning", "reasoning_effort", "extended_thinking")))
                    if "departments" not in meta:
                        meta["departments"] = _find_list(obj, ("allowed_departments", "departments", "department", "lanes", "lane"))
                    if "capabilities" not in meta:
                        meta["capabilities"] = _find_list(obj, ("allowed_capabilities", "capabilities", "capability", "caps"))
            except Exception:
                continue
        out[_canon(pdir.name)] = {"model": meta.get("model"), "thinking": meta.get("thinking"),
                                  "departments": meta.get("departments") or [],
                                  "capabilities": meta.get("capabilities") or []}
    return out


def _find_list(d: dict, keys) -> list:
    keys = tuple(k.lower() for k in keys)
    for k, v in d.items():
        if k.lower() in keys:
            if isinstance(v, list):
                return [str(x) for x in v]
            if isinstance(v, str):
                return [s.strip() for s in re.split(r"[,;]+", v) if s.strip()]
    for v in d.values():
        if isinstance(v, dict):
            r = _find_list(v, keys)
            if r:
                return r
    return []


def build_models(runs: list[dict], pmeta: dict | None = None) -> dict:
    """Aggregate normalized runs into per-profile and per-model usage stats.
    Returns raw sums so週/Monat roll-ups can merge cheaply; the UI finalizes
    averages. Fills model/thinking from profile config when runs omit them."""
    pmeta = pmeta or {}

    def blank():
        return {"runs": 0, "in_tok": 0, "out_tok": 0, "cost": 0.0,
                "dur_sum": 0.0, "dur_n": 0, "thinking_runs": 0}

    by_profile: dict = {}
    by_model: dict = {}
    have = {"model": False, "tokens": False, "latency": False, "thinking": False, "cost": False}

    for r in runs:
        prof = r.get("profile") or "unknown"
        pm = pmeta.get(_canon(prof), {})
        model = r.get("model") or pm.get("model")
        thinking = r.get("thinking")
        if thinking in (None, "") and pm.get("thinking") is not None:
            thinking = pm.get("thinking")
        if model:
            have["model"] = True
        if r.get("in_tok") or r.get("out_tok"):
            have["tokens"] = True
        if r.get("cost"):
            have["cost"] = True
        if r.get("dur_s") is not None:
            have["latency"] = True
        thinky = _is_thinking_on(thinking)
        if thinky:
            have["thinking"] = True

        for bucket, key, extra in ((by_profile, prof, {"model": model, "thinking": thinking}),
                                   (by_model, model or "unknown", None)):
            ent = bucket.setdefault(key, blank())
            ent["runs"] += 1
            ent["in_tok"] += int(r.get("in_tok") or 0)
            ent["out_tok"] += int(r.get("out_tok") or 0)
            ent["cost"] += float(r.get("cost") or 0.0)
            if r.get("dur_s") is not None:
                ent["dur_sum"] += float(r["dur_s"]); ent["dur_n"] += 1
            if thinky:
                ent["thinking_runs"] += 1
            if extra:
                if extra.get("model"):
                    ent["model"] = extra["model"]
                if extra.get("thinking") not in (None, ""):
                    ent["thinking"] = extra["thinking"]

    def listify(bucket, name_key):
        rows = []
        for k, v in bucket.items():
            row = {name_key: k}
            row.update(v)
            rows.append(row)
        rows.sort(key=lambda x: x["runs"], reverse=True)
        return rows

    return {"by_profile": listify(by_profile, "profile"),
            "by_model": listify(by_model, "model"),
            "available": have, "total_runs": len(runs)}


def _is_thinking_on(v: Any) -> bool:
    if v in (None, "", 0, False):
        return False
    s = str(v).strip().lower()
    return s not in ("0", "false", "off", "no", "none", "disabled", "low_none")


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


_BOARD_SEP = "::"  # namespaces task ids per board so ids never collide across boards


class _BoardSource:
    """Reads ONE board's kanban.db. Task ids are namespaced as
    ``slug \\x1f localid`` so several boards can be merged without collisions."""

    def __init__(self, cfg: Config, path: Path, slug: str = "default"):
        self.cfg = cfg
        self.path = path
        self.slug = slug
        self._conn: sqlite3.Connection | None = None
        self._cols: dict[str, dict[str, str]] = {}   # table -> {logical: actual}
        self._events_table = "task_events"
        self._tasks_table = "tasks"
        self._comments_table = "task_comments"
        self._runs_table: str | None = None

    def _ns(self, localid: Any) -> str:
        return f"{self.slug}{_BOARD_SEP}{localid}"

    @staticmethod
    def local_id(namespaced: str) -> str:
        return namespaced.partition(_BOARD_SEP)[2] or namespaced

    @staticmethod
    def slug_of(namespaced: str) -> str:
        head, sep, _ = namespaced.partition(_BOARD_SEP)
        return head if sep else "default"

    # -- connection / introspection -------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            # Path.as_uri() percent-encodes URI metacharacters (notably # and ?),
            # so an explicitly configured filename cannot truncate the URI and
            # accidentally drop the read-only mode query parameter.
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
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
                            "task_tenant", "task_priority", "task_created", "task_body",
                            "task_created_by", "task_workspace")
        }
        if self._runs_table:
            rn = self._table_cols(self._runs_table)
            self._cols["runs"] = {
                logical: self._pick(logical, rn, ov.get(logical))
                for logical in ("run_id", "run_task", "run_outcome", "run_profile", "run_summary",
                                "run_error", "run_started", "run_ended", "run_duration", "run_in_tok",
                                "run_out_tok", "run_cost", "run_model", "run_thinking")
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
            out.append(Event(id=r["_id"], task_id=self._ns(r["_task"]), kind=str(r["_kind"]),
                             data=payload, ts=ets, run_id=r["_run"]))
        return out

    def _col_is_text(self, table: str, col: str) -> bool:
        # NOTE: query the OWNING table — SQLite returns a quoted unknown column
        # name as a string literal, which would otherwise misclassify the type.
        try:
            row = self._conn.execute(
                f'SELECT "{col}" FROM {table} WHERE "{col}" IS NOT NULL LIMIT 1').fetchone()
            return bool(row) and isinstance(row[0], str) and not str(row[0]).isdigit()
        except Exception:
            return False

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
        cols.append(f'"{c["task_body"]}" AS _body' if c.get("task_body") else "'' AS _body")
        cols.append(f'"{c["task_created_by"]}" AS _cby' if c.get("task_created_by") else "'' AS _cby")
        cols.append(f'"{c["task_workspace"]}" AS _ws' if c.get("task_workspace") else "'' AS _ws")
        q = f'SELECT {", ".join(cols)} FROM {self._tasks_table}'
        rows = self._conn.execute(q).fetchall()
        out: dict[str, Task] = {}
        wanted = set(ids) if ids is not None else None
        for r in rows:
            _local = str(r["_id"])
            _id = self._ns(_local)
            if wanted is not None and _id not in wanted and _local not in wanted:
                continue
            out[_id] = Task(id=_id, title=r["_title"] or "", status=(r["_status"] or "").lower(),
                            assignee=r["_asg"] or "", tenant=r["_ten"] or "",
                            priority=r["_prio"], created=_to_epoch(r["_cr"]),
                            body=r["_body"] or "", created_by=r["_cby"] or "", workspace=r["_ws"] or "")
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

    def _norm_run(self, d: dict, c: dict) -> dict:
        def g(key):
            col = c.get(key)
            return d.get(col) if col else None
        started = _to_epoch(g("run_started")) if g("run_started") not in (None, "") else None
        ended = _to_epoch(g("run_ended")) if g("run_ended") not in (None, "") else None
        dur = None
        raw_dur = g("run_duration")
        if raw_dur not in (None, ""):
            try:
                dv = float(raw_dur)
                dcol = (c.get("run_duration") or "").lower()
                dur = dv / 1000.0 if ("ms" in dcol or dv > 100000) else dv
            except Exception:
                dur = None
        if dur is None and started and ended and ended >= started:
            dur = float(ended - started)

        def num(key):
            v = g(key)
            try:
                return float(v) if v not in (None, "") else 0.0
            except Exception:
                return 0.0
        return {
            "profile": str(g("run_profile") or "unknown"),
            "model": (str(g("run_model")).strip() if g("run_model") not in (None, "") else None),
            "thinking": g("run_thinking"),
            "in_tok": int(num("run_in_tok")), "out_tok": int(num("run_out_tok")),
            "cost": num("run_cost"), "dur_s": dur, "started": started,
            "outcome": (str(g("run_outcome")) if g("run_outcome") not in (None, "") else None),
        }

    def fetch_runs_window(self, start_ts: int, end_ts: int) -> list[dict]:
        """Normalized runs whose start (or end) falls in [start_ts, end_ts)."""
        self.connect()
        if not self._runs_table:
            return []
        c = self._cols.get("runs", {})
        scol = c.get("run_started") or c.get("run_ended")
        scol_text = self._col_is_text(self._runs_table, scol) if scol else False
        try:
            if scol and not scol_text:
                rows = self._conn.execute(
                    f'SELECT * FROM {self._runs_table} WHERE "{scol}" >= ? AND "{scol}" < ?',
                    (start_ts, end_ts)).fetchall()
            else:
                rows = self._conn.execute(f'SELECT * FROM {self._runs_table}').fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            nr = self._norm_run(dict(r), c)
            t = nr["started"]
            if scol and scol_text:
                if t is None or not (start_ts <= t < end_ts):
                    continue
            out.append(nr)
        return out

    def fetch_links(self) -> list[tuple]:
        """Detect a dependency/handoff table and return namespaced (parent, child)."""
        self.connect()
        tables = self._tables()
        for cand in ("task_links", "task_dependencies", "dependencies", "links", "edges"):
            if cand in tables:
                cols = self._table_cols(cand)
                pc = next((x for x in ("parent_id", "parent", "from_id", "from", "src", "source", "blocker_id", "blocker") if x in cols), None)
                cc = next((x for x in ("child_id", "child", "to_id", "to", "dst", "target", "blocked_id", "blocked", "dep_id") if x in cols), None)
                if pc and cc:
                    try:
                        rows = self._conn.execute(f'SELECT "{pc}" AS p, "{cc}" AS c FROM {cand}').fetchall()
                        return [(self._ns(str(r["p"])), self._ns(str(r["c"])))
                                for r in rows if r["p"] and r["c"]]
                    except Exception:
                        return []
        return []

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

    def status_colors(self) -> dict:
        """Best-effort discovery of the board's per-status/column colors, so the
        report can mirror the kanban instead of hard-coding hues. Looks for a
        table with a color column + a name/status column, and also parses any
        JSON config cell that carries name/color pairs. Returns {canon_name: color}."""
        self.connect()
        out: dict = {}
        try:
            tables = [r[0] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
        except Exception:
            return out
        # scan status/column-ish tables first to avoid unrelated 'color' columns
        def rank(t):
            tl = t.lower()
            return 0 if any(k in tl for k in ("column", "lane", "status", "stage", "list", "board")) else 1
        for tbl in sorted(tables, key=rank):
            try:
                cols = [r[1] for r in self._conn.execute(f'PRAGMA table_info("{tbl}")')]
            except Exception:
                continue
            low = {c.lower(): c for c in cols}
            color_col = next((c for c in cols if "color" in c.lower() or "colour" in c.lower() or c.lower() == "hex"), None)
            label_col = next((low[p] for p in ("name", "label", "title", "status", "key", "slug", "column", "lane", "stage") if p in low), None)
            # (a) structured color column
            if color_col and label_col:
                try:
                    for r in self._conn.execute(f'SELECT "{label_col}","{color_col}" FROM "{tbl}"').fetchall():
                        nm, col = _canon(r[0]), r[1]
                        if nm and _looks_like_color(col):
                            out.setdefault(nm, col.strip())
                except Exception:
                    pass
            # (b) JSON config blobs (board/column config stored as text)
            for c in cols:
                try:
                    rows = self._conn.execute(
                        f'SELECT "{c}" FROM "{tbl}" WHERE "{c}" LIKE \'%color%\' LIMIT 50').fetchall()
                except Exception:
                    continue
                for r in rows:
                    _harvest_json_colors(r[0], out)
        # (c) config files next to the board db (board.json, columns.yaml, ...)
        try:
            bdir = self.path.parent
            files = list(bdir.glob("*.json")) + list(bdir.glob("*.yaml")) + list(bdir.glob("*.yml"))
            _scan_files_for_colors(files, out)
        except Exception:
            pass
        return out

    def event_ts_bounds(self) -> "tuple[int, int] | None":
        """(earliest, latest) event epoch in this board, or None if no events."""
        self.connect()
        ts = self._cols.get("events", {}).get("event_ts")
        if not ts:
            return None
        try:
            if self._ts_is_text(ts):
                rows = self._conn.execute(
                    f'SELECT "{ts}" FROM {self._events_table} WHERE "{ts}" IS NOT NULL'
                ).fetchall()
                vals = [_to_epoch(r[0]) for r in rows]
                return (min(vals), max(vals)) if vals else None
            row = self._conn.execute(
                f'SELECT MIN("{ts}"), MAX("{ts}") FROM {self._events_table}'
            ).fetchone()
            if row and row[0] is not None:
                return (_to_epoch(row[0]), _to_epoch(row[1]))
        except Exception:
            return None
        return None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def discover_boards(cfg: Config) -> list[tuple[str, Path]]:
    """All configured kanban DBs as ``(slug, path)``.

    ``kanban_db`` deliberately remains an explicit single-board override. Without
    it, normal profile-local discovery is preserved and explicit external roots
    (``<root>/<slug>/kanban.db``) and DB paths are added read-only as sources.
    """
    out: list[tuple[str, Path]] = []
    seen_paths: set[str] = set()
    seen_slugs: set[str] = set()

    def add(slug: str, p: Path):
        try:
            rp = str(p.resolve())
        except Exception:
            rp = str(p)
        if not p or not p.is_file() or rp in seen_paths:
            return
        base_slug = slug or "default"
        unique_slug = base_slug
        suffix = 2
        while unique_slug in seen_slugs:
            unique_slug = f"{base_slug}-{suffix}"
            suffix += 1
        seen_paths.add(rp)
        seen_slugs.add(unique_slug)
        out.append((unique_slug, p))

    if cfg.kanban_db:                       # explicit override → single board
        add("default", cfg.resolved_kanban_db())
        return out or [("default", cfg.resolved_kanban_db())]

    add("default", cfg.hermes_home / "kanban.db")
    for base in (cfg.hermes_home / "kanban" / "boards", cfg.hermes_home / "boards",
                 *cfg.external_board_roots):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.is_dir():
                    add(d.name, d / "kanban.db")
    for db in cfg.external_board_dbs:
        add(db.parent.name or "external", db)
    return out


class KanbanSource:
    """Aggregates one or many boards behind the same interface the digest uses.

    ``board=None`` (default) merges every discovered board; ``board=<slug>``
    restricts to a single board. Task ids returned are namespaced per board, so
    the digest never mixes two boards' tasks up."""

    def __init__(self, cfg: Config, board: str | None = None):
        self.cfg = cfg
        self.board = None if board in (None, "", "all") else board
        all_boards = discover_boards(cfg)
        if self.board:
            chosen = [(s, p) for s, p in all_boards if s == self.board]
            if not chosen:                  # asked-for board not found → empty source
                chosen = []
            self._sources = [_BoardSource(cfg, p, s) for s, p in chosen]
        else:
            self._sources = [_BoardSource(cfg, p, s) for s, p in all_boards]
        # always keep at least one source so /health etc. resolve a path
        if not self._sources:
            self._sources = [_BoardSource(cfg, cfg.resolved_kanban_db(), self.board or "default")]
        self._by_slug = {s.slug: s for s in self._sources}

    @property
    def path(self) -> Path:
        return self._sources[0].path if self._sources else self.cfg.resolved_kanban_db()

    def _for(self, namespaced: str) -> "_BoardSource | None":
        return self._by_slug.get(_BoardSource.slug_of(namespaced))

    def fetch_events(self, start_ts: int, end_ts: int) -> list[Event]:
        evs: list[Event] = []
        for s in self._sources:
            try:
                evs.extend(s.fetch_events(start_ts, end_ts))
            except Exception:
                continue
        evs.sort(key=lambda e: (e.ts, str(e.id)))
        return evs

    def fetch_tasks(self, ids: Iterable[str] | None = None) -> dict[str, Task]:
        out: dict[str, Task] = {}
        for s in self._sources:
            try:
                out.update(s.fetch_tasks(ids))
            except Exception:
                continue
        return out

    def fetch_comments(self, namespaced: str) -> list[dict]:
        s = self._for(namespaced)
        return s.fetch_comments(_BoardSource.local_id(namespaced)) if s else []

    def fetch_runs(self, namespaced: str) -> list[dict]:
        s = self._for(namespaced)
        return s.fetch_runs(_BoardSource.local_id(namespaced)) if s else []

    def fetch_runs_window(self, start_ts: int, end_ts: int) -> list[dict]:
        out: list[dict] = []
        for s in self._sources:
            try:
                out.extend(s.fetch_runs_window(start_ts, end_ts))
            except Exception:
                continue
        return out

    def fetch_links(self) -> list[tuple]:
        out: list[tuple] = []
        for s in self._sources:
            try:
                out.extend(s.fetch_links())
            except Exception:
                continue
        return out

    def has_links_table(self) -> bool:
        for s in self._sources:
            try:
                s.connect()
                if any(t in s._tables() for t in ("task_links", "task_dependencies", "dependencies", "links", "edges")):
                    return True
            except Exception:
                continue
        return False

    def task_bundle(self, task_id: str, window_events: list[Event]) -> dict:
        evs = [e for e in window_events if e.task_id == task_id]
        return {
            "task_id": task_id,
            "events": [{"kind": e.kind, "data": e.data, "ts": e.ts} for e in evs],
            "runs": self.fetch_runs(task_id),
            "comments": self.fetch_comments(task_id),
            "last_event_id": max((e.id for e in evs), default=0),
        }

    def has_run_token_columns(self) -> bool:
        return any(s.has_run_token_columns() for s in self._sources
                   if (s.connect() or True))

    def inspect_schema(self) -> dict:
        boards = []
        for s in self._sources:
            try:
                boards.append({"slug": s.slug, **s.inspect_schema()})
            except Exception as exc:
                boards.append({"slug": s.slug, "error": str(exc), "db_path": str(s.path)})
        return {"board_filter": self.board or "all", "boards": boards}

    def board_slugs(self) -> list[str]:
        return [s.slug for s in self._sources]

    def history_bounds(self) -> "tuple[int | None, int | None]":
        mn = mx = None
        for s in self._sources:
            try:
                b = s.event_ts_bounds()
            except Exception:
                b = None
            if b:
                mn = b[0] if mn is None else min(mn, b[0])
                mx = b[1] if mx is None else max(mx, b[1])
        return (mn, mx)

    def status_colors(self) -> dict:
        out: dict = {}
        for s in self._sources:
            try:
                for k, v in s.status_colors().items():
                    out.setdefault(k, v)
            except Exception:
                continue
        # global kanban config files (colors may live outside per-board dbs)
        try:
            roots = [self.cfg.hermes_home, self.cfg.hermes_home / "kanban"]
            files = []
            for root in roots:
                if root.is_dir():
                    for ext in ("*.json", "*.yaml", "*.yml"):
                        files += list(root.glob(ext))
            _scan_files_for_colors(files, out)
        except Exception:
            pass
        return out

    def close(self) -> None:
        for s in self._sources:
            try:
                s.close()
            except Exception:
                pass


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
    """Approximate $ (USD) cost. Marked '≈' wherever it is rendered."""
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
    board        TEXT NOT NULL DEFAULT 'all',
    date         TEXT NOT NULL,           -- YYYY-MM-DD (local)
    json         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (board, date)
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
        # migrate an older digests table that predates the per-board key — the
        # digests are a rebuildable cache, so dropping is safe and simplest.
        try:
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(digests)")]
            if "board" not in cols:
                self.conn.execute("DROP TABLE IF EXISTS digests")
                self.conn.executescript(_SCHEMA)
        except Exception:
            pass
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

    def put_digest(self, board: str, date: str, digest: dict) -> None:
        self.conn.execute(
            "INSERT INTO digests(board,date,json,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(board,date) DO UPDATE SET json=excluded.json, created_at=excluded.created_at",
            (board, date, json.dumps(digest, ensure_ascii=False), int(time.time())),
        )
        self.conn.commit()

    def get_digest(self, board: str, date: str) -> dict | None:
        row = self.conn.execute(
            "SELECT json FROM digests WHERE board=? AND date=?", (board, date)
        ).fetchone()
        return json.loads(row["json"]) if row else None

    def list_digests(self, board: str, limit: int = 60) -> list[dict]:
        rows = self.conn.execute(
            "SELECT date, json FROM digests WHERE board=? ORDER BY date DESC LIMIT ?",
            (board, limit),
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
            sb = _bucket_of(t.status)
            if sb == "done":
                if self.resolve_decision(d["id"], "auto-done"):
                    closed += 1
            elif sb in ("active", "todo") and d["kind"] in ("approval", "blocked", "failed"):
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
    return _any_text(e.data or {})


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
        b = event_bucket(e)

        if e.kind == "blocked" or b == "blocked":
            reason = _reason_text(e)
            if _is_approval(reason, cfg):
                key = f"{e.task_id}:approval"
                primary[e.task_id] = {
                    "id": key, "task_id": e.task_id, "kind": "approval",
                    "title": task_title, "detail": _short(reason, 280),
                    "deadline": e.data.get("deadline") or e.data.get("expires"),
                    "status": t.status if t else None,
                }
            else:
                key = f"{e.task_id}:blocked"
                primary[e.task_id] = {
                    "id": key, "task_id": e.task_id, "kind": "blocked",
                    "title": task_title, "detail": _short(reason, 280), "deadline": None,
                    "status": t.status if t else None,
                }

        elif e.kind in _FAILED_KINDS or b == "failed":
            key = f"{e.task_id}:failed"
            default_detail = ("Gave up after retries." if cfg.language != "de"
                              else "Endgültig fehlgeschlagen nach Retries.")
            primary[e.task_id] = {
                "id": key, "task_id": e.task_id, "kind": "failed",
                "title": task_title,
                "detail": _short(_reason_text(e) or default_detail, 280),
                "deadline": None,
                "status": t.status if t else None,
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
                "status": t.status if t else None,
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


def _last_text(bundle: dict, buckets: tuple[str, ...]) -> str:
    for ev in reversed(bundle.get("events", [])):
        if _bucket_ev(ev.get("kind"), ev.get("data")) in buckets:
            t = _any_text(ev.get("data") or {})
            if t:
                return t
    for r in bundle.get("runs", []):
        t = _any_text(r, extra=("stdout", "log"))
        if t:
            return t
    return ""


def _fallback(bundle: dict, lang: str = "en") -> dict:
    buckets = [_bucket_ev(e.get("kind"), e.get("data")) for e in bundle.get("events", [])]
    done = "done" in buckets
    blocked = "blocked" in buckets
    failed = "failed" in buckets
    words = ({"done": "fertig", "gave up": "aufgegeben", "blocked": "blockiert", "wip": "in Arbeit"}
             if lang == "de" else
             {"done": "done", "gave up": "gave up", "blocked": "blocked", "wip": "in progress"})
    outcome = words["done"] if done else words["gave up"] if failed else words["blocked"] if blocked else words["wip"]
    why = _last_text(bundle, ("done",)) if done else \
          _last_text(bundle, ("blocked", "failed"))
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
        bit = _any_text(ev.get("data") or {})
        lines.append(f"- {ev.get('kind')}: {str(bit)[:240]}")
    for c in bundle.get("comments", [])[-2:]:
        body = _any_text(c)
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

_STATUS_WORD = {"en": {"active": "active", "quiet": "quiet"},
                "de": {"active": "läuft", "quiet": "ruhig"}}
_CAVEAT = {
    "en": "insights counts mostly interactive sessions; worker runs may be missing.",
    "de": "insights zählt v.a. interaktive Sessions; Worker-Runs evtl. nicht erfasst.",
}


def day_bounds(date_str: str, tz: str) -> tuple[int, int, datetime]:
    z = _safe_zoneinfo(tz)
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=z)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp()), start


def today_str(tz: str) -> str:
    return datetime.now(_safe_zoneinfo(tz)).strftime("%Y-%m-%d")


def _month_start_ts(date_str: str, tz: str) -> int:
    z = _safe_zoneinfo(tz)
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=z, day=1, hour=0, minute=0, second=0)
    return int(d.timestamp())


def _final_bucket(task_id: str, events: list) -> Optional[str]:
    """The last determinable status bucket for a task within the given events
    (events come ordered oldest->newest). Lets us tell whether an escalated task
    was still blocked at end of day or got resolved during it."""
    last = None
    for e in events:
        if e.task_id != task_id:
            continue
        b = event_bucket(e)
        if b:
            last = b
    return last


def _event_status(e) -> Optional[str]:
    """Best-effort raw status/column a status-bearing event moves a task INTO."""
    d = e.data if isinstance(e.data, dict) else {}
    for k in ("to", "to_status", "new_status", "to_column", "column", "status", "state"):
        v = d.get(k)
        if v:
            return str(v)
    kb = _canon(e.kind)
    for word in ("blocked", "completed", "done", "review", "ready", "running",
                 "scheduled", "archived", "triage", "todo", "failed"):
        if word in kb:
            return word
    return None


def _status_as_of(task_id: str, events: list) -> Optional[str]:
    """The task's raw status as of the END of the given (windowed) events —
    i.e. its state on that report's day, ignoring any later changes."""
    best, best_ts = None, -1
    for e in events:
        if e.task_id != task_id:
            continue
        s = _event_status(e)
        if s is not None and e.ts >= best_ts:
            best, best_ts = s, e.ts
    return best


_AGENT_HINTS = ("orchestrator", "agent", "worker", "curator", "ceo", "cto", "cpo", "bot")
_TH_HANG = int(os.environ.get("REPORTS_HANG_HOURS", "48") or 48)
_TH_LATENCY = int(os.environ.get("REPORTS_LATENCY_HOURS", "12") or 12)
_TH_UNROUTED = int(os.environ.get("REPORTS_UNROUTED_HOURS", "6") or 6)


def _read_phase(cfg: Config) -> str | None:
    env = os.environ.get("REPORTS_PHASE")
    if env:
        return env.strip()
    for rel in ("PHASE", "phase", "strategy-lab/PHASE", "strategy-lab/phase.txt"):
        fp = cfg.hermes_home / rel
        try:
            if fp.is_file():
                line = fp.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
                if line:
                    return line[0].strip()[:60]
        except Exception:
            continue
    return None


def _looks_human(creator: str) -> bool:
    """Fallback heuristic when no profile registry is available."""
    c = _canon(creator)
    if not c:
        return False
    return not any(hint in c for hint in _AGENT_HINTS)


def _is_human(creator: str, profile_slugs: set) -> bool:
    """A card author is human iff it is not a known agent profile (E2 rule)."""
    c = _canon(creator)
    if not c:
        return False
    if profile_slugs:
        return c not in profile_slugs
    return _looks_human(creator)


def _board_light(done_ct: int, blocked_ct: int, active_ct: int, open_ct: int) -> str:
    if blocked_ct > 0:
        return "blocked"
    if done_ct > 0 or active_ct > 0:
        return "on_track"
    if open_ct > 0:
        return "waiting"
    return "on_track"


def build_overview(cfg: Config, date_str: str, board: str, events: list, tasks: dict,
                   runs: list, hand: list, learned: list, cost: dict,
                   board_slugs: list, start_ts: int, end_ts: int, pmeta: dict | None = None) -> dict:
    pmeta = pmeta or {}
    profile_slugs = set(pmeta.keys())
    # per-board traffic lights + KPIs
    lights = []
    slugs = board_slugs if board in (None, "all") else [board]
    new_total = 0
    for slug in slugs:
        bev = [e for e in events if _BoardSource.slug_of(e.task_id) == slug]
        btasks = {k: v for k, v in tasks.items() if _BoardSource.slug_of(k) == slug}
        done_ct = len({e.task_id for e in bev if e.kind in _DONE_KINDS or event_bucket(e) == "done"})
        new_ct = len({e.task_id for e in bev if "creat" in _canon(e.kind)})
        new_total += new_ct
        blocked_now = sum(1 for t in btasks.values() if _bucket_of(t.status) in ("blocked", "failed"))
        active_ct = len([e for e in bev if event_bucket(e) == "active"])
        open_ct = sum(1 for t in btasks.values() if _bucket_of(t.status) in ("todo", "active", "blocked", "failed"))
        lights.append({"board": slug, "light": _board_light(done_ct, blocked_now, active_ct, open_ct),
                       "done": done_ct, "blocked": blocked_now, "open": open_ct})

    # active profiles today (runs + assignees touched)
    profs = set(r.get("profile") for r in runs if r.get("profile"))
    touched = {e.task_id for e in events}
    for tid in touched:
        t = tasks.get(tid)
        if t and t.assignee:
            profs.add(t.assignee)
    profs.discard("unknown"); profs.discard("")

    done_ids = {e.task_id for e in events if e.kind in _DONE_KINDS or event_bucket(e) == "done"}
    # "Blocked" is a STATE, not a per-day flow: count every task still blocked/
    # failed as of this report (carried over from earlier days), matching the
    # board lights and the "Needs your call" list — not just newly-blocked today.
    blocked_now_total = sum(lg["blocked"] for lg in lights)

    # team input: human-created cards today + still unrouted (real created_by)
    team_new = 0; team_unrouted = 0
    cby_in_use = any(t.created_by for t in tasks.values())
    if cby_in_use:
        for t in tasks.values():
            if _is_human(t.created_by, profile_slugs):
                team_new += 1
                if not t.assignee:
                    team_unrouted += 1
    else:
        for e in events:
            if "creat" not in _canon(e.kind):
                continue
            t = tasks.get(e.task_id)
            if t and not t.assignee:
                team_new += 1; team_unrouted += 1

    # skill/SOUL change counter (best-effort: events/comments mentioning skill or soul edits)
    skill_soul = 0
    for e in events:
        blob = (_canon(e.kind) + " " + _any_text(e.data or {}).lower())
        if ("skill" in blob or "soul" in blob) and any(w in blob for w in ("edit", "change", "diff", "update", "freigab", "gate", "modif")):
            skill_soul += 1

    # next priority per board (top ready/todo by priority)
    nexts = []
    for slug in slugs:
        cand = [t for k, t in tasks.items() if _BoardSource.slug_of(k) == slug and _bucket_of(t.status) == "todo"]
        cand.sort(key=lambda t: _prio_num(t.priority), reverse=True)
        if cand:
            nexts.append({"board": slug, "title": cand[0].title, "task_id": cand[0].id})

    phase = _read_phase(cfg)
    if not phase:
        caps = {}
        for t in tasks.values():
            if _bucket_of(t.status) in ("todo", "active", "blocked"):
                cap = parse_body_header(t.body).get("capability")
                if cap:
                    caps[cap] = caps.get(cap, 0) + 1
        if caps:
            phase = max(caps, key=caps.get)

    return {
        "mode": "day", "board": board or "all", "phase": phase,
        "board_lights": lights,
        "kpis": {"done": len(done_ids), "new": new_total, "blocked": blocked_now_total,
                 "active_profiles": len(profs)},
        "counters": {"lessons": len(learned), "skill_soul": skill_soul},
        "team_input": {"new": team_new, "unrouted": team_unrouted},
        "next_priorities": nexts,
    }


def _prio_num(p: Any) -> float:
    try:
        return float(p)
    except Exception:
        m = {"urgent": 3, "high": 2, "normal": 1, "med": 1, "medium": 1, "low": 0}
        return m.get(_canon(p), 0)


def run_verification(cfg: Config, date_str: str, src, events: list, tasks: dict,
                     pmeta: dict, runs: list, hand: list, cost: dict, system: dict) -> dict:
    """The 12 WFDE behavior benchmarks (B1-B12), deterministic. Each computes
    live where the data exists, else reports 'na' (gray) — never a false red.
    B4-B7 stay 'na' until the memory/skill/contract paths are wired (M5)."""
    now = int(time.time())
    headers = {tid: parse_body_header(t.body) for tid, t in tasks.items()}
    done_ids = {e.task_id for e in events if (e.kind in _DONE_KINDS) or event_bucket(e) == "done"}
    last_ev: dict = {}
    for e in events:
        last_ev[e.task_id] = max(last_ev.get(e.task_id, 0), e.ts)
    checks = []

    def add(cid, label, status, detail="", refs=None):
        checks.append({"id": cid, "label": label, "status": status,
                       "detail": detail, "refs": refs or []})

    # B1 · Routing & ownership (needs profile lanes from E11)
    lanes_present = any((pmeta.get(p, {}).get("departments") or pmeta.get(p, {}).get("capabilities"))
                        for p in pmeta)
    if lanes_present:
        mism = []
        for tid, t in tasks.items():
            if _bucket_of(t.status) in ("done", "active") and t.assignee:
                hd = headers[tid]; pm = pmeta.get(_canon(t.assignee), {})
                deps = [_canon(x) for x in pm.get("departments", [])]
                caps = [_canon(x) for x in pm.get("capabilities", [])]
                if deps and hd.get("department") and _canon(hd["department"]) not in deps:
                    mism.append(tid)
                elif caps and hd.get("capability") and _canon(hd["capability"]) not in caps:
                    mism.append(tid)
        add("routing", "Routing & ownership", "red" if mism else "green",
            (f"{len(mism)} lane mismatch" if mism else "assignees fit their lane"), mism[:8])
    else:
        add("routing", "Routing & ownership", "na", "no profile lanes found (E11)")

    # B2 · Handoffs closed (needs task_links from E4)
    if src.has_links_table():
        links = src.fetch_links()
        seen = set(t for t in tasks if _bucket_of(tasks[t].status) in ("done", "active"))
        for e in events:
            k = _canon(e.kind)
            if "claim" in k or "assign" in k or event_bucket(e) in ("active", "done"):
                seen.add(e.task_id)
        orphans = [c for (p, c) in links if c not in seen]
        add("handoffs", "Handoffs closed", "red" if orphans else "green",
            (f"{len(orphans)} open handoff(s)" if orphans else "chains closed"), orphans[:8])
    else:
        add("handoffs", "Handoffs closed", "na", "no task_links table (E4)")

    # B3 · Gate integrity (markers in body + comments)
    def task_markers(tid, t):
        m = extract_markers(t.body)
        try:
            for cm in src.fetch_comments(tid):
                m |= extract_markers(_any_text(cm))
        except Exception:
            pass
        return m
    gates_anywhere = any(extract_markers(t.body) & _GATE_MARKERS for t in tasks.values())
    done_missing = []
    for tid in done_ids:
        t = tasks.get(tid)
        if not t:
            continue
        mk = task_markers(tid, t)
        if mk & _GATE_MARKERS:
            gates_anywhere = True
        else:
            done_missing.append(tid)
    if gates_anywhere:
        add("gate_integrity", "Gate integrity", "red" if done_missing else "green",
            (f"{len(done_missing)} done w/o gate" if done_missing else "every done passed a gate"),
            done_missing[:8])
    else:
        add("gate_integrity", "Gate integrity", "na", "no gate markers in use (E5)")

    # B4-B7 · need memory / skill / contract paths (M5)
    add("ceo_exclusivity", "CEO exclusivity", "na", "needs validated-decisions.md (E8)")
    add("memory_provenance", "Memory provenance", "na", "needs memory files (E7/E8)")
    add("memory_pollution", "Memory pollution", "na", "needs memory-curator signal (E?)")
    add("skill_soul_auth", "Skill/SOUL authorization", "na", "needs skill diffs + _backups (E9)")

    # B8 · Execution anchoring (workspace / output)
    ws_in_use = any(t.workspace for t in tasks.values())
    out_in_use = any(headers[tid].get("output") for tid in tasks)
    if ws_in_use or out_in_use:
        miss = []
        for tid in done_ids:
            t = tasks.get(tid)
            if not t:
                continue
            hd = headers[tid]
            is_build = bool(hd.get("service") or hd.get("output"))
            if (is_build or ws_in_use) and not t.workspace:
                miss.append(tid)
        add("execution_anchor", "Execution anchoring", "red" if miss else "green",
            (f"{len(miss)} done w/o workspace" if miss else "outputs anchored"), miss[:8])
    else:
        add("execution_anchor", "Execution anchoring", "na", "no workspace/output data (E2)")

    # B9 · Manual inputs routed
    profile_slugs = set(pmeta.keys())
    cby_in_use = any(t.created_by for t in tasks.values())
    cutoff = _TH_UNROUTED * 3600
    if cby_in_use:
        unrouted = [t for t in tasks.values() if _is_human(t.created_by, profile_slugs)
                    and not t.assignee and (now - (t.created or now)) > cutoff]
        add("manual_inputs", "Manual inputs routed", "red" if unrouted else "green",
            (f"{len(unrouted)} unrouted >{_TH_UNROUTED}h" if unrouted else "team cards routed"),
            [t.id for t in unrouted][:8])
    else:
        unrouted = [t for t in tasks.values() if not t.assignee and (now - (t.created or now)) > cutoff]
        add("manual_inputs", "Manual inputs routed", "red" if unrouted else "green",
            (f"{len(unrouted)} unrouted >{_TH_UNROUTED}h (no created_by)" if unrouted else "all routed"),
            [t.id for t in unrouted][:8])

    # B10 · Progress vs phase (hang_hours)
    stuck = [tid for tid, t in tasks.items()
             if _bucket_of(t.status) not in ("done",) and t.created
             and (now - last_ev.get(tid, t.created)) > _TH_HANG * 3600]
    add("progress_phase", "Progress vs phase", "red" if stuck else "green",
        (f"{len(stuck)} cards stuck >{_TH_HANG}h" if stuck else "cards moving toward done"), stuck[:8])

    # B11 · Reaction latency (latency_hours)
    created_ts: dict = {}; claimed_ts: dict = {}
    for e in events:
        k = _canon(e.kind)
        if "creat" in k:
            created_ts[e.task_id] = min(created_ts.get(e.task_id, e.ts), e.ts)
        if "claim" in k or "assign" in k or event_bucket(e) == "active":
            claimed_ts[e.task_id] = min(claimed_ts.get(e.task_id, e.ts), e.ts)
    laggy = [tid for tid, cts in created_ts.items()
             if (claimed_ts.get(tid, now) - cts) > _TH_LATENCY * 3600 and (now - cts) > _TH_LATENCY * 3600]
    add("latency", "Reaction latency", "red" if laggy else "green",
        (f"{len(laggy)} inputs unhandled >{_TH_LATENCY}h" if laggy else "responsive"), laggy[:8])

    # B12 · Cost & health
    over = (cost.get("today_eur", 0) or 0) > (cost.get("budget_daily", 0) or 0)
    unstable = not system.get("stable", True)
    add("cost_health", "Cost & health", "red" if (over or unstable) else "green",
        (("over budget" if over else "") + (" / unstable" if unstable else "")) or "in budget, stable")

    green = sum(1 for c in checks if c["status"] == "green")
    red = sum(1 for c in checks if c["status"] == "red")
    na = sum(1 for c in checks if c["status"] == "na")
    return {"checks": checks, "green": green, "red": red, "na": na, "total": len(checks)}


def build_insights(cfg: Config, start_ts: int, end_ts: int) -> dict:
    """Windowed usage analytics from Hermes' state.db (sessions + messages),
    scoped to [start_ts, end_ts) so day/week/month reports stay accurate even
    when rebuilt for the past. Mirrors `hermes insights`, but date-windowed."""
    import sqlite3
    import datetime as _dt
    empty = {"available": False}
    db = cfg.hermes_home / "state.db"
    if not db.exists():
        return empty
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=4000")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if not cols or "started_at" not in cols:
            return empty

        def has(c):
            return c in cols

        rows = conn.execute(
            "SELECT * FROM sessions WHERE started_at >= ? AND started_at < ?",
            (start_ts, end_ts)).fetchall()
        n = len(rows)

        def tok(r):
            return ((r["input_tokens"] or 0) if has("input_tokens") else 0) + \
                   ((r["output_tokens"] or 0) if has("output_tokens") else 0)

        in_tok = sum((r["input_tokens"] or 0) for r in rows) if has("input_tokens") else 0
        out_tok = sum((r["output_tokens"] or 0) for r in rows) if has("output_tokens") else 0
        msgs = sum((r["message_count"] or 0) for r in rows) if has("message_count") else 0
        tools = sum((r["tool_call_count"] or 0) for r in rows) if has("tool_call_count") else 0
        dur = 0.0
        for r in rows:
            if has("ended_at") and has("started_at") and r["ended_at"] and r["started_at"]:
                d = r["ended_at"] - r["started_at"]
                if d > 0:
                    dur += d

        by_model: dict = {}
        by_platform: dict = {}
        z = _safe_zoneinfo(cfg.timezone)
        weekday = [0] * 7
        hours = [0] * 24
        days = set()
        for r in rows:
            m = (r["model"] if has("model") and r["model"] else "?")
            bm = by_model.setdefault(m, {"sessions": 0, "tokens": 0})
            bm["sessions"] += 1
            bm["tokens"] += tok(r)
            p = (r["source"] if has("source") and r["source"] else "?")
            bp = by_platform.setdefault(p, {"sessions": 0, "messages": 0, "tokens": 0})
            bp["sessions"] += 1
            bp["messages"] += (r["message_count"] or 0) if has("message_count") else 0
            bp["tokens"] += tok(r)
            t = r["started_at"]
            if t:
                dt = _dt.datetime.fromtimestamp(t, z)
                weekday[dt.weekday()] += 1
                hours[dt.hour] += 1
                days.add(dt.strftime("%Y-%m-%d"))

        user_msgs = 0
        total_msgs_tbl = 0
        try:
            mc = {r2[1] for r2 in conn.execute("PRAGMA table_info(messages)")}
            if "timestamp" in mc and "role" in mc:
                for rr in conn.execute(
                        "SELECT role, COUNT(*) c FROM messages WHERE timestamp>=? AND timestamp<? GROUP BY role",
                        (start_ts, end_ts)):
                    total_msgs_tbl += rr["c"]
                    if str(rr["role"]).lower() == "user":
                        user_msgs = rr["c"]
        except Exception:
            pass
        if not msgs:
            msgs = total_msgs_tbl

        peak_hour = max(range(24), key=lambda h: hours[h]) if any(hours) else None
        overview = {
            "sessions": n, "messages": msgs, "tool_calls": tools, "user_messages": user_msgs,
            "input_tokens": in_tok, "output_tokens": out_tok, "total_tokens": in_tok + out_tok,
            "active_minutes": round(dur / 60), "avg_session_min": round((dur / 60) / n, 1) if n else 0,
            "avg_msgs": round(msgs / n, 1) if n else 0,
        }
        model_list = sorted([dict(model=k, **v) for k, v in by_model.items()],
                            key=lambda x: x["tokens"], reverse=True)
        plat_list = sorted([dict(platform=k, **v) for k, v in by_platform.items()],
                           key=lambda x: x["sessions"], reverse=True)
        return {"available": n > 0, "overview": overview, "by_model": model_list,
                "by_platform": plat_list, "weekday": weekday, "hours": hours,
                "peak_hour": peak_hour, "active_days": len(days)}
    except Exception:
        return empty
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def build_digest(cfg: Config, date_str: str, persist: bool = True, mark: bool = True,
                 board: str = "all") -> dict:
    start_ts, end_ts, _ = day_bounds(date_str, cfg.timezone)
    src = KanbanSource(cfg, board)
    store = Store(cfg)
    lang = cfg.language if cfg.language in _STATUS_WORD else "en"
    if mark:
        store.build_begin(f"Building briefing for {date_str}", 1)
    try:
        store.expire_due()
        events = src.fetch_events(start_ts, end_ts)
        tasks = src.fetch_tasks()  # snapshot of current statuses

        # For historical rebuilds, reconstruct each task's status AS OF THIS DAY
        # from the full event history up to end-of-day, so lights/KPIs/pills show
        # the real state then — not a status the task only reached later.
        is_today = (date_str == today_str(cfg.timezone))
        day_status: dict = {}
        if not is_today:
            try:
                for e in sorted(src.fetch_events(0, end_ts), key=lambda ev: ev.ts):
                    s = _event_status(e)
                    if s:
                        day_status[e.task_id] = s
            except Exception:
                day_status = {}
        # day-accurate task view (status overridden where we could reconstruct it)
        if day_status:
            view_tasks = {tid: (replace(t, status=day_status[tid]) if tid in day_status else t)
                          for tid, t in tasks.items()}
        else:
            view_tasks = tasks

        # 1) escalation. Persist to the live decision store (for the current
        #    "what's open now" view), but the per-day report is a HISTORICAL
        #    record: `hand` = what was escalated THAT day, even if the task has
        #    since been resolved. Today additionally carries over anything still
        #    open from earlier days.
        day_esc = escalate(events, tasks, cfg)
        for d in day_esc:
            store.upsert_decision(d)
        store.reconcile_decisions(tasks)

        day_ids = {d["id"] for d in day_esc}
        # keep only items that were STILL open at end of that day (a task that
        # got unblocked or finished later the same day no longer needs your hand)
        hand = [d for d in day_esc
                if _final_bucket(d["task_id"], events) in ("blocked", "failed", None)]
        if date_str == today_str(cfg.timezone):
            for od in store.open_decisions():
                if od["id"] in day_ids:
                    continue
                if board not in (None, "all") and _BoardSource.slug_of(od.get("task_id", "")) != board:
                    continue        # other board's carry-over — not in this view
                hand.append(od)
            # guarantee every currently blocked/failed task is listed (carried
            # over from earlier days), even if no decision record exists for it —
            # so the "Blocked" count and this list always agree.
            present = {d.get("task_id") for d in hand}
            for tid, t in tasks.items():
                if tid in present:
                    continue
                if board not in (None, "all") and _BoardSource.slug_of(tid) != board:
                    continue
                if _bucket_of(t.status) in ("blocked", "failed"):
                    kind = "failed" if _bucket_of(t.status) == "failed" else "blocked"
                    detail = ("Still blocked — carried over from an earlier day."
                              if cfg.language != "de"
                              else "Weiterhin blockiert — von einem früheren Tag übernommen.")
                    hand.append({"id": f"{tid}:{kind}", "task_id": tid, "kind": kind,
                                 "title": t.title, "detail": detail, "deadline": None,
                                 "status": t.status})
        # Color each pill by the status the task had ON THIS DAY, so a rebuilt
        # historical briefing shows the real state then — not a later "done".
        #  • today        -> live kanban status (exact, incl. custom columns)
        #  • past day      -> status reconstructed from events up to end-of-day
        #  • approval kind -> always its semantic gold (it's a derived state)
        #  • fallback      -> unset, so the frontend colors by escalation kind
        for d in hand:
            if d.get("kind") == "approval":
                d.pop("status", None)
                continue
            tid = d.get("task_id")
            if is_today:
                t = tasks.get(tid)
                if t and t.status:
                    d["status"] = t.status
                else:
                    d.pop("status", None)
            else:
                sa = day_status.get(tid) or _status_as_of(tid, events)
                if sa:
                    d["status"] = sa
                else:
                    d.pop("status", None)

        # 2) completed today -> AI/heuristic summary. Schema-agnostic: a task is
        #    "done today" if any event this day transitions it to a done-bucket
        #    (covers literal 'completed' kinds AND status-change payloads).
        done_ids = {e.task_id for e in events
                    if e.kind in _DONE_KINDS or event_bucket(e) == "done"}
        done = []
        for tid in done_ids:
            bundle = src.task_bundle(tid, events)
            s = summarize_task(bundle, cfg, store)
            t = tasks.get(tid)
            done.append({"task_id": tid, "title": t.title if t else tid,
                         "status": (t.status if t else "done"),
                         "bullets": s["bullets"], "why": s["why"]})

        # 3) in progress now (snapshot, not window)
        in_progress = [
            {"task_id": t.id, "title": t.title, "status": t.status}
            for t in tasks.values() if _bucket_of(t.status) == "active"
        ][:25]

        # 4) cost / usage
        usage = fetch_usage(cfg, days=1)
        usage_month = fetch_usage(cfg, days=_days_into_month(date_str, cfg.timezone))
        today_eur = estimate_cost_eur(usage, cfg)
        month_eur = estimate_cost_eur(usage_month, cfg)
        runs = len([e for e in events
                    if e.kind in ("claimed", "spawned") or event_bucket(e) == "active"])
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
        system = {"stable": not err, "notes": notes,
                  "insights": build_insights(cfg, start_ts, end_ts)}

        # 6) learned (optional, lightweight: pull short comment lines tagged as notes)
        learned = _extract_learned(src, events, tasks)

        # 7) model / profile usage (latency, tokens, thinking) for model selection
        pmeta = profile_meta(cfg)
        runs_window = src.fetch_runs_window(start_ts, end_ts)
        models = build_models(runs_window, pmeta)

        # 8) Part-1 overview + 12-point verification layer
        overview = build_overview(cfg, date_str, board, events, view_tasks, runs_window,
                                  hand, learned, cost, src.board_slugs(), start_ts, end_ts, pmeta)
        verification = run_verification(cfg, date_str, src, events, view_tasks, pmeta,
                                        runs_window, hand, cost, system)

        status = (_STATUS_WORD[lang]["active"] if hand or done
                  else _STATUS_WORD[lang]["quiet"])
        digest = {
            "date": date_str, "range": "day", "board": board,
            "generated_at": int(time.time()),
            "header": {"status": status, "open": len(hand),
                       "cost_eur": round(today_eur, 2), "budget_eur": cfg.budget_daily_eur},
            "hand": [_decision_view(d) for d in hand],
            "in_progress": in_progress, "done": done, "learned": learned,
            "models": models, "overview": overview, "verification": verification,
            "cost": cost, "system": system,
            "decision_stats": store.decision_stats(_month_start_ts(date_str, cfg.timezone)),
        }
        if persist:
            store.put_digest(board, date_str, digest)
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
            "status": d.get("status") or d.get("kind"),
            "deadline": d.get("deadline")}


_LEARNED_TAGS = ("gelernt", "learned", "notiert", "erkenntnis", "lesson", "insight")
_LEARNED_PREFIXES = ("notiert:", "gelernt:", "learned:", "erkenntnis:", "lesson:", "insight:")


def _clean_md(text: str) -> str:
    t = text or ""
    t = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", t)      # inline code
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)          # bold
    t = re.sub(r"[*_#>~]+", " ", t)                   # stray md tokens / headers
    t = re.sub(r"^\s*[-•\d.]+\s*", "", t)             # leading list/number markers
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_learned(src: KanbanSource, events: list[Event], tasks: dict | None = None) -> list[dict]:
    """Surface short, cleaned 'learned/erkenntnis'-style notes from comments and
    tie each to its task so the UI can link to the ticket. Markdown is stripped."""
    tasks = tasks or {}
    out: list[dict] = []
    seen: set[str] = set()
    for tid in {e.task_id for e in events}:
        for c in src.fetch_comments(tid):
            body = _any_text(c).strip()
            low = body.lower()
            if not any(tag in low for tag in _LEARNED_TAGS):
                continue
            # pick the specific line carrying the tag, not the whole document
            line = None
            for raw in re.split(r"[\r\n]+", body):
                if any(tag in raw.lower() for tag in _LEARNED_TAGS):
                    line = raw
                    break
            line = _clean_md(line or body)
            low2 = line.lower()
            for pre in _LEARNED_PREFIXES:
                if low2.startswith(pre):
                    line = line[len(pre):].strip()
                    break
            # "Kern-Erkenntnis: X" -> "X" when the label itself is the tag
            if ":" in line:
                head, _, rest = line.partition(":")
                if rest.strip() and len(head) <= 60 and any(t in head.lower() for t in _LEARNED_TAGS):
                    line = rest.strip()
            line = line[:180].strip()
            if line and line not in seen:
                seen.add(line)
                t = tasks.get(tid)
                out.append({"text": line, "task_id": tid, "title": t.title if t else None})
    return out[:8]


def build_recent(cfg: Config, days: int = 7, board: str = "all") -> dict:
    """Build the last `days` daily digests (oldest->newest), skipping ones already
    cached except today (always refreshed). Manages the shared build status so the
    dashboard can show progress. Safe to call on first open."""
    z = _safe_zoneinfo(cfg.timezone)
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
            if not is_today and store.get_digest(board, ds):
                skipped.append(ds)
            else:
                build_digest(cfg, ds, mark=False, board=board)   # don't touch overall status
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
    z = _safe_zoneinfo(cfg.timezone)
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


def build_range(cfg: Config, from_date: str, to_date: str, board: str = "all") -> dict:
    """Roll up daily digests into a weekly/monthly view (builds missing days)."""
    z = _safe_zoneinfo(cfg.timezone)
    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=z)
    end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=z)
    store = Store(cfg)
    days, cur = [], start
    src = KanbanSource(cfg, None if board in (None, "", "all") else board)
    try:
        rstart, _, _ = day_bounds(from_date, cfg.timezone)
        _, rend, _ = day_bounds(to_date, cfg.timezone)
        range_models = build_models(src.fetch_runs_window(rstart, rend), profile_meta(cfg))
        range_insights = build_insights(cfg, rstart, rend)
    except Exception:
        range_models = {"by_profile": [], "by_model": [], "available": {}, "total_runs": 0}
        range_insights = {"available": False}
    finally:
        src.close()
    try:
        while cur <= end:
            ds = cur.strftime("%Y-%m-%d")
            d = store.get_digest(board, ds) or build_digest(cfg, ds, board=board)
            days.append(d)
            cur += timedelta(days=1)
        cost_sum = round(sum(d["cost"]["today_eur"] for d in days), 2)
        done = [item for d in days for item in d["done"]]
        hand_open = days[-1]["hand"] if days else []
        seen_l: set = set(); learned = []
        for d in days:
            for l in d.get("learned", []):
                key = l.get("text") if isinstance(l, dict) else l
                if key and key not in seen_l:
                    seen_l.add(key); learned.append(l)
        stats = days[-1]["decision_stats"] if days else {}
        return {
            "range": "custom", "from": from_date, "to": to_date, "board": board,
            "generated_at": int(time.time()),
            "cost_eur": cost_sum, "done": done, "hand": hand_open,
            "budget_daily": cfg.budget_daily_eur, "budget_monthly": cfg.budget_monthly_eur,
            "num_days": len(days),
            "learned": learned, "decision_stats": stats, "models": range_models,
            "system": {"insights": range_insights},
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
    head = f"{dd} · {h['status']} · {open_str} · ≈ ${cost['today_eur']:.2f} / ${cost['budget_daily']:.0f}"

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
        txt = l.get("text") if isinstance(l, dict) else l
        lines.append(f"  {L['noted']:<9} {txt}")

    cd = cost["today_eur"] / cost["budget_daily"] if cost["budget_daily"] else 0
    near = L["near"] if cd >= 0.8 else ""
    lines.append(
        f"  {L['cost']:<9} {L['today']} ≈${cost['today_eur']:.2f}/${cost['budget_daily']:.0f} · "
        f"{L['month']} ≈${cost['month_eur']:.2f}/${cost['budget_monthly']:.0f} · {cost['runs']} {L['runs']}{near}"
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
    lines.append(f"  {L['cost']:<9} ≈ ${roll['cost_eur']:.2f} total")
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
            txt = l.get("text") if isinstance(l, dict) else l
            lines.append(f"  • {txt}")
    return "\n".join(lines) + "\n"


def _fmt_deadline(deadline, tz: str) -> str:
    try:
        v = float(deadline)
        if v > 1e12:
            v /= 1000
        dt = datetime.fromtimestamp(v, _safe_zoneinfo(tz))
        return dt.strftime("%a %H:%M")
    except Exception:
        return str(deadline) if deadline else ""


def build_task_view(src: KanbanSource, start_ts: int | None = None,
                    end_ts: int | None = None) -> dict:
    """Return a read-only task list for a report window.

    When a window is supplied, the list is deliberately transition-based: each
    row represents the most recent done/blocked transition inside that window,
    not the task's current global status.  This keeps the TaskList and its
    chart aligned and excludes carried-over terminal tasks.
    """
    tasks = src.fetch_tasks()
    links = src.fetch_links()
    parents = {child: parent for parent, child in links if child in tasks and parent in tasks}
    children: dict[str, list[str]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)

    transitions: dict[str, Event] = {}
    windowed = start_ts is not None and end_ts is not None
    if windowed:
        assert start_ts is not None and end_ts is not None
        for event in src.fetch_events(start_ts, end_ts):
            bucket = event_bucket(event)
            if event.task_id not in tasks or bucket not in ("done", "blocked", "failed"):
                continue
            prior = transitions.get(event.task_id)
            if prior is None or (event.ts, str(event.id)) >= (prior.ts, str(prior.id)):
                transitions[event.task_id] = event
    else:
        now = int(time.time())
        for event in src.fetch_events(0, now + 1):
            if event.task_id in tasks and _bucket_of(tasks[event.task_id].status) == "done" and event_bucket(event) == "done":
                transitions[event.task_id] = event

    rows = []
    selected_ids = set(transitions) if windowed else set(tasks)
    for task_id in selected_ids:
        task = tasks[task_id]
        transition = transitions.get(task_id)
        bucket = event_bucket(transition) if transition else None
        status = (_event_status(transition) or bucket) if transition else task.status
        rows.append({
            "id": task_id,
            "title": task.title,
            "status": status,
            "priority": task.priority,
            "assignee": task.assignee,
            "board": _BoardSource.slug_of(task_id),
            "created_at": task.created or None,
            "completed_at": transition.ts if transition and bucket == "done" else None,
            "comment_count": len(src.fetch_comments(task_id)),
            "parent_id": parents.get(task_id) if parents.get(task_id) in selected_ids else None,
            "child_ids": sorted(child for child in children.get(task_id, []) if child in selected_ids),
        })
    return {"tasks": rows}


# ---------------------------------------------------------------- routes

if router is not None:

    @router.get("/health")
    def health():
        cfg = load_config()
        try:
            found = discover_boards(cfg)
        except Exception:
            found = []
        return {"ok": True, "kanban_db": str(cfg.resolved_kanban_db()),
                "db_exists": cfg.resolved_kanban_db().exists(),
                "boards": [{"slug": s, "path": str(p)} for s, p in found],
                "llm_enabled": cfg.llm.enabled}

    @router.get("/schema")
    def schema():
        src = KanbanSource(load_config())
        try:
            return src.inspect_schema()
        finally:
            src.close()

    @router.get("/digests")
    def digests(limit: int = 60, board: str = "all"):
        store = Store(load_config())
        try:
            return {"digests": store.list_digests(board, limit)}
        finally:
            store.close()

    @router.get("/boards")
    def boards():
        """List selectable boards for the report's board picker."""
        src = KanbanSource(load_config(), None)
        try:
            slugs = src.board_slugs()
        finally:
            src.close()
        return {"boards": ["all"] + slugs}

    @router.get("/colors")
    def colors(board: str = "all"):
        """Per-status colors auto-discovered from the kanban DB so report badges
        match the board. Empty if none found (UI then uses its own palette)."""
        cfg = load_config()
        src = KanbanSource(cfg, None if board in (None, "", "all") else board)
        try:
            return {"status_colors": src.status_colors()}
        finally:
            src.close()

    @router.get("/tasks")
    def tasks(from_: str, to: str, board: str = "all"):
        """Read-only terminal task transitions for the requested report dates."""
        cfg = load_config()
        start_ts, _, _ = day_bounds(from_, cfg.timezone)
        _, end_ts, _ = day_bounds(to, cfg.timezone)
        src = KanbanSource(cfg, None if board in (None, "", "all") else board)
        try:
            return build_task_view(src, start_ts, end_ts)
        finally:
            src.close()

    @router.get("/history")
    def history(board: str = "all"):
        """Earliest/latest activity across the (selected) boards, so the UI can
        span reports over the whole history instead of only the last few days."""
        cfg = load_config()
        src = KanbanSource(cfg, None if board in (None, "", "all") else board)
        try:
            mn, mx = src.history_bounds()
        finally:
            src.close()
        if not mn:
            today = today_str(cfg.timezone)
            return {"first_date": today, "last_date": today, "days": 1}
        z = _safe_zoneinfo(cfg.timezone)
        fd = datetime.fromtimestamp(mn, z).strftime("%Y-%m-%d")
        ld = datetime.fromtimestamp(mx, z).strftime("%Y-%m-%d")
        span = (datetime.strptime(ld, "%Y-%m-%d") - datetime.strptime(fd, "%Y-%m-%d")).days + 1
        return {"first_date": fd, "last_date": ld, "days": span}

    # NOTE: these are sync `def` on purpose — FastAPI runs them in a threadpool,
    # so building (which is blocking) never stalls the dashboard event loop.
    def _resolve_date(cfg, date: str) -> str:
        return today_str(cfg.timezone) if date in ("today", "heute") else date

    @router.get("/digest/{date}")
    def digest(date: str, rebuild: bool = False, board: str = "all"):
        cfg = load_config()
        date = _resolve_date(cfg, date)
        store = Store(cfg)
        try:
            cached = None if rebuild else store.get_digest(board, date)
        finally:
            store.close()
        return cached or build_digest(cfg, date, board=board)   # build on demand

    @router.get("/render/{date}", response_class=PlainTextResponse)
    def render(date: str, rebuild: bool = False, board: str = "all"):
        cfg = load_config()
        date = _resolve_date(cfg, date)
        store = Store(cfg)
        try:
            d = None if rebuild else store.get_digest(board, date)
        finally:
            store.close()
        d = d or build_digest(cfg, date, board=board)
        return render_day(d, cfg.timezone, cfg.language)

    @router.get("/range")
    def range_(from_: str, to: str, board: str = "all"):
        return build_range(load_config(), from_, to, board=board)

    @router.get("/ensure")
    def ensure(days: int = 7, board: str = "all"):
        """Build the last `days` daily digests (today first). Safe to call on open."""
        return build_recent(load_config(), days, board=board)

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
        board = body.get("board") or "all"
        if body.get("days"):
            return build_recent(cfg, int(body["days"]), board=board)
        date = body.get("date") or today_str(cfg.timezone)
        return build_digest(cfg, date, board=board)

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
