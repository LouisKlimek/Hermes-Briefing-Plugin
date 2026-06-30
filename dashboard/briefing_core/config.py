"""Configuration for the Hermes Reports plugin.

Resolution order (later wins):
  1. built-in defaults below
  2. ~/.hermes/reports/config.yaml  (or $HERMES_HOME/reports/config.yaml)
  3. environment variables (REPORTS_*)

Nothing here is required — the plugin runs with defaults and degrades
gracefully when a data source or API key is missing.
"""
from __future__ import annotations

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
