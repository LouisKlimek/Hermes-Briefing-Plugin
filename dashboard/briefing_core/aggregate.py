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
from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config
from .kanban_source import KanbanSource, Event
from .insights_source import fetch_usage, estimate_cost_eur
from .escalate import escalate
from .summarize import summarize_task
from .store import Store

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
