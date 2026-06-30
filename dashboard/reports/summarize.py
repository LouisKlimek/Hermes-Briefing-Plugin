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
from __future__ import annotations

import json
import urllib.request

from .config import Config
from .store import Store

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
            prompt = f"Aufgabe {task_id}. Aktivität:\n{_compact_bundle(bundle)}"
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
