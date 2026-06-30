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
from __future__ import annotations

from .config import Config
from .kanban_source import Event

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
            primary[e.task_id] = {
                "id": key, "task_id": e.task_id, "kind": "failed",
                "title": task_title,
                "detail": _short(_reason_text(e) or "Endgültig fehlgeschlagen nach Retries.", 280),
                "deadline": None,
            }

        elif e.kind == "protocol_violation":
            violations[e.task_id] = violations.get(e.task_id, 0) + 1

    decisions = dict(primary)
    for task_id, n in violations.items():
        if n >= cfg.protocol_violation_alert_threshold and task_id not in primary:
            t = tasks.get(task_id)
            key = f"{task_id}:instability"
            decisions[task_id] = {
                "id": key, "task_id": task_id, "kind": "instability",
                "title": t.title if t else task_id,
                "detail": f"{n}× Protokollverletzung im Zeitraum — Worker stieg ohne complete/block aus.",
                "deadline": None,
            }

    return list(decisions.values())
