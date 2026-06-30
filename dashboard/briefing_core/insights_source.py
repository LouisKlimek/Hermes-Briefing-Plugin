"""Token usage + cost via `hermes insights`.

`hermes insights --days N` is the documented usage-analytics command. We try a
`--json` form first; if that isn't supported we regex-parse the box output as a
fallback (the totals line is stable enough).

CAVEAT (surfaced in the report): insights is keyed on *sessions*. Interactive
surfaces (tui/gateway) are reliably counted; autonomous dispatcher worker runs
may or may not be — verify on your install. When the runs table carries token
columns we prefer those for a true per-run figure (see KanbanSource).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field

from .config import Config

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
