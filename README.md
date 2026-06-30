# Hermes Briefing

> A drop-in dashboard plugin for **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** that turns the multi-agent kanban activity into crisp, bullet-style **daily / weekly / monthly briefings** — what got done, what it cost, and what's waiting on _you_, and why. Delivered as Markdown by email **and** as a tab in the Hermes dashboard. No fork, no build step.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](#license)
[![Hermes Agent](https://img.shields.io/badge/Hermes%20Agent-dashboard%20plugin-7c3aed.svg)](https://github.com/NousResearch/hermes-agent)
[![Build](https://img.shields.io/badge/build-none%20required-success.svg)](#install)
[![No deps](https://img.shields.io/badge/runtime%20deps-0-blue.svg)](#how-it-works)

Companion to **[Hermes TaskList](https://github.com/LouisKlimek/Hermes-Tasklist-Plugin)**. Where TaskList gives you a live ClickUp-style _view_ of the board, Briefing gives you the _narrative over time_ — a report you can read in 20 seconds that separates **what needs your hand** from **what's just for the record**.

---

## The briefing

```text
26.06 · läuft · 1 offen · ≈ 12.40 € / 15 €

▸ DEINE HAND
  • Freigabe nötig: QS-Wissensbasis
    review-required: B-Wortlaut wartet auf BOSS-Freigabe. A geschrieben, C erledigt.

▸ PROTOKOLL
  Fertig    Foodtruck-Events sammeln — craftplaces als zuverlässigste Quelle bestätigt
  In Arbeit Standplatz-Vermittlung
  Notiert   Foodtrucker buchen meist < 3 Tage vorher
  Kosten    heute ≈12.40 €/15 € · Monat ≈210 €/400 € · 47 Runs  ← knapp am Limit
  System    stabil
```

A quiet day collapses to a single line:

```text
26.06 · ruhig · nichts offen · ≈ 4.00 € / 15 € · alles in Ordnung
```

---

## Why

A mostly-autonomous agent system produces a firehose of events. You don't want to read the firehose — you want to know the two things that need a decision, glance at what shipped, and see whether you're burning budget. Briefing is built around that human-attention split, and around one rule it never breaks:

> **Rules decide what reaches your hand. The AI only ever shortens the wording.**

What gets escalated is pure, auditable event-type logic. The LLM is a compressor, not a judge — so you can trust the `▸ DEINE HAND` section.

---

## Features

- **Attention-first layout** — `DEINE HAND` (needs a decision) vs. `PROTOKOLL` (for the record), exactly how you'd triage the board yourself.
- **Deterministic escalation** — `blocked` + an approval keyword → _Freigabe nötig_; `gave_up` / `timed_out` → _Aufgegeben_; repeated `protocol_violation` → _Instabil_. No AI in the decision path.
- **Stateful decisions** — an open item persists across days until you give **OK**, **veto** it, or its deadline passes — that's the `1 offen` counter. Resolve straight from the dashboard.
- **One digest, many renderers** — Markdown (email) and the dashboard tab render the _same_ JSON, so they never drift. Weekly/monthly are pure roll-ups of the daily digests.
- **Cost-aware & cheap** — token→€ from `hermes insights`, against a budget bar. Task summaries are cached on `(task_id, last_event_id)` and only recomputed on new events. The AI summarizer is **off by default**; the deterministic fallback uses the `completed` summary / `blocked` reason directly.
- **Zero runtime dependencies** — reads the board read-only (WAL-safe), talks to any OpenAI-compatible or Anthropic endpoint via stdlib only.
- **Works without the dashboard** — a standalone CLI prints any day's briefing; email it with the bundled systemd timer.

---

## How it works

A thin read-only layer over the existing board, plus its own small overlay DB for the things the kanban model doesn't have (stateful decisions, cached summaries, cached digests):

```text
┌──────────────────────────────┐        ┌──────────────────────────────┐
│  Briefing tab (React, SDK)    │        │  send_report.py  →  e-mail    │
└──────────────┬───────────────┘        └───────────────┬──────────────┘
               │  both render the SAME digest JSON       │
               ▼                                          ▼
        ┌─────────────────────────────────────────────────────┐
        │  aggregate.build_digest(date)                        │
        │   • event diff over the day's window                 │
        │   • escalate()  → deterministic "needs your hand"    │
        │   • summarize() → cheap LLM/heuristic, cached        │
        │   • insights    → tokens × price = ≈ €               │
        └───────┬───────────────────────────────────┬─────────┘
                │ read-only (mode=ro)                │ overlay
                ▼                                    ▼
        ~/.hermes/kanban.db                 ~/.hermes/briefing/briefing.db
        (tasks, task_events, …)             (decisions, summaries, digests)
```

Task edits are never written to the board — Briefing only reads it. The only state it owns is its own overlay DB.

---

## Install

```bash
git clone https://github.com/LouisKlimek/Hermes-Briefing-Plugin.git ~/.hermes/plugins/briefing
```

Final layout (the standard Hermes plugin contract — a `dashboard/` subfolder):

```text
~/.hermes/plugins/briefing/
└── dashboard/
    ├── manifest.json
    ├── plugin_api.py          # backend routes (FastAPI) + standalone CLI
    ├── reports/               # aggregation, escalation, summarizer, renderers
    ├── send_report.py         # build + email today's briefing
    ├── dist/index.js          # the Briefing tab (React, via the Plugin SDK)
    ├── config.example.yaml
    └── systemd/               # daily timer + service
```

Pick it up:

```bash
# backend routes mount at dashboard startup → restart once for the API:
hermes dashboard
# tab-only refresh (no restart):
curl http://127.0.0.1:9119/api/dashboard/plugins/rescan
```

Hard-refresh the browser (Ctrl/Cmd+Shift+R). A **Briefing** tab appears after **Skills**.

> Requires a working Hermes Agent install with the dashboard enabled and the bundled kanban board initialised (`hermes kanban init`). Built against Hermes `main` (≈ v0.14.x); uses only the documented Plugin SDK and stable kanban schema (`tasks`, `task_events`, `task_comments`, runs).

---

## Quick start — no dashboard needed

The Markdown path is fully standalone. From `~/.hermes/plugins/briefing/dashboard/`:

```bash
# 1. Confirm the DB schema auto-detected correctly (tables + column mapping)
python plugin_api.py inspect-schema

# 2. Print today's briefing
python plugin_api.py render

# 3. A specific day, or a weekly roll-up
python plugin_api.py render 2026-06-26
python plugin_api.py range  2026-06-22 2026-06-26
```

If `inspect-schema` shows a wrong column under `resolved_columns`, set an override in `config.yaml` — that's the only thing that ever needs hand-tuning.

### Email it daily

Copy `config.example.yaml` → `~/.hermes/briefing/config.yaml`, fill in `smtp:`, then:

```bash
python send_report.py            # prints if SMTP is unset, otherwise sends
```

Schedule it (edit paths/time in the unit files first):

```bash
cp systemd/hermes-report.* ~/.config/systemd/user/
systemctl --user enable --now hermes-report.timer
```

Or one cron line:

```cron
30 19 * * * /usr/bin/python3 ~/.hermes/plugins/briefing/dashboard/send_report.py
```

---

## Configuration

Everything is optional and lives in `~/.hermes/briefing/config.yaml`; `REPORTS_*` env vars override it.

| Key | What it does |
| --- | --- |
| `budget.daily_eur` / `monthly_eur` | Drives the budget bar and the `← knapp am Limit` flag. |
| `pricing` | € per 1M tokens per model. **Verify against your provider** — defaults are placeholders, so cost is rendered with `≈`. |
| `approval_keywords` | Substrings in a `blocked` reason that mean "a human must decide". German + English defaults included. |
| `protocol_violation_alert_threshold` | Flag a task as _Instabil_ after this many violations in a window. |
| `llm.enabled` | Turn on for richer "why" lines. Any OpenAI-compatible endpoint or Anthropic. ≤120 tokens, cached, cheap. |
| `schema` | Column-name overrides — only if PRAGMA auto-detection guesses wrong. |

---

## Backend API

Mounted at `/api/plugins/briefing/`:

| Endpoint | Method | Description |
| --- | --- | --- |
| `/health` | GET | DB path, exists?, llm on/off |
| `/schema` | GET | resolved + raw column mapping |
| `/digests?limit=` | GET | list of cached days (date, open, cost) |
| `/digest/{date}?rebuild=` | GET | structured digest JSON |
| `/render/{date}` | GET | the Markdown briefing (text/plain) |
| `/range?from_=&to=` | GET | weekly/monthly roll-up |
| `/decisions` | GET | open "needs your hand" items |
| `/decisions/{id}/resolve` | POST | body `{"resolution":"ok"\|"veto"}` |

Like all Hermes plugin routes, these bypass session auth because the dashboard binds to localhost. **Do not run `hermes dashboard --host 0.0.0.0`** with this installed.

---

## Caveats worth knowing

- **Cost source.** `hermes insights` is keyed on sessions; it reliably counts interactive surfaces (tui/gateway) but may not capture autonomous dispatcher worker runs. The briefing flags this. If your runs table carries token/cost columns, Briefing auto-detects and prefers them for a true per-run figure.
- **"Notiert" is light.** It surfaces short comment lines tagged `notiert:` / `gelernt:` / `learned:`. For richer learnings, have your orchestrator emit them as tagged comments.
- **Single host.** Like the board itself, this reads one local SQLite file.

---

## Roadmap

- **Veto-rate trend** in the monthly view as an agent-calibration metric — _you reviewed N auto-decisions, vetoed M_ → widen or narrow autonomy.
- **Per-run cost** once the orchestrator logs token usage per attempt.
- **Nudge tasks** — auto-create a kanban task for each `DEINE HAND` item, assigned to you.
- Saved filters / per-tenant briefings.

Contributions welcome — please include your `hermes --version` and, for UI issues, a screenshot plus browser console output.

---

## License

[MIT](LICENSE) — same license as Hermes Agent and the official example plugins.

---

<sub>Keywords: Hermes Agent dashboard plugin · multi-agent kanban report · daily standup digest · agent activity report · LLM cost tracking · human-in-the-loop approvals · Nous Research Hermes · self-hosted AI agent orchestration · kanban daily report plugin.</sub>
