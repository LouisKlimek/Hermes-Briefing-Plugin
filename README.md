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
26.06 · active · 1 open · ≈ $12.40 / $15

▸ YOUR CALL
  • Needs approval: QS knowledge base
    review-required: wording B is waiting on your sign-off. A written, C done.

▸ LOG
  Done      Collect food-truck events — craftplaces confirmed as the most reliable source
  Active    Pitch-spot matching
  Noted     Food-truck owners usually book < 3 days ahead
  Cost      today ≈$12.40/$15 · month ≈$210/$400 · 47 runs  ← near budget limit
  System    stable
```

A quiet day collapses to a single line:

```text
26.06 · quiet · nothing open · ≈ $4.00 / $15 · all clear
```

> Verbatim agent text (block reasons, summaries) is shown exactly as the agent wrote it, in whatever language your agents work in. Everything Briefing generates around it is English by default (`language: de` for German).

---

## Why

A mostly-autonomous agent system produces a firehose of events. You don't want to read the firehose — you want to know the two things that need a decision, glance at what shipped, and see whether you're burning budget. Briefing is built around that human-attention split, and around one rule it never breaks:

> **Rules decide what reaches your hand. The AI only ever shortens the wording.**

What gets escalated is pure, auditable event-type logic. The LLM is a compressor, not a judge — so you can trust the `▸ YOUR CALL` section.

---

## Features

- **Attention-first layout** — `DEINE HAND` (needs a decision) vs. `PROTOKOLL` (for the record), exactly how you'd triage the board yourself.
- **Deterministic escalation** — `blocked` + an approval keyword → _Needs approval_; `gave_up` / `timed_out` → _Gave up_; repeated `protocol_violation` → _Unstable_. No AI in the decision path.
- **Stateful decisions** — an open item persists across days until you give **OK**, **veto** it, or its deadline passes — that's the `1 offen` counter. Resolve straight from the dashboard.
- **One digest, many renderers** — Markdown (email) and the dashboard tab render the _same_ JSON, so they never drift. Weekly/monthly are pure roll-ups of the daily digests.
- **Cost-aware & cheap** — token→$ from `hermes insights`, against a budget bar. Task summaries are cached on `(task_id, last_event_id)` and only recomputed on new events. The AI summarizer is **off by default**; the deterministic fallback uses the `completed` summary / `blocked` reason directly.
- **Zero runtime dependencies** — reads the board read-only (WAL-safe), talks to any OpenAI-compatible or Anthropic endpoint via stdlib only.
- **Day / Week / Month tabs** — switch timeframes inline; the daily view has a 14-day picker, week/month are rolled-up views. Everything renders in the dashboard, nothing to download.
- **Instant, zero-setup first run** — opening the tab builds today on demand and shows it right away (animated), then fills in recent days in the background. A live status bar shows background builds and the next scheduled run.
- **Multi-board aware** — by default a briefing aggregates **every** kanban board (default board + all `<hermes_home>/kanban/boards/<slug>/kanban.db`). A board picker in the tab lets you narrow any report to a single board; each board's digests are cached separately. Task ids are namespaced per board so nothing gets mixed up.
- **Full-history reports** — the briefing spans your entire kanban history, not just the last few days. On open it asks `/history` for the earliest event across all boards and builds a daily digest for every day since (cached, so it's a one-time cost). The day sidebar shows a `done`/`open` badge per day; Week and Month views roll the days up with `done-per-day` and `cost-per-day` charts, and Month has prev/next navigation to browse back through time. Reports show successes (Done) as compact cards with one-line bullets — not only items that need your hand.
- **Schema-agnostic activity detection** — reconstructs each day from the kanban event log without hard-coding event names. Any event is mapped to a canonical bucket (done / blocked / failed / active) by matching the event `kind` *and* status fields in its JSON payload (`to`, `status`, `column`, …), so renamed or future board schemas keep working. Past days are filled from when statuses were actually set, even after every ticket is already done.
- **GET-only data path** — builds, rebuilds and decision resolves all work over GET, so it stays reliable even on Hermes builds whose `fetchJSON` doesn't forward POST options.
- **English by default** — set `language: de` for the original German labels. Verbatim agent text (block reasons, summaries) is always shown as the agent wrote it.
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
        │   • insights    → tokens × price = ≈ $               │
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
# clone into your plugins dir; the target folder "briefing" keeps API URLs clean
git clone https://github.com/LouisKlimek/Hermes-Briefing-Plugin.git <PLUGINS_DIR>/briefing
```
> `<PLUGINS_DIR>` is your install's plugin folder — `~/.hermes/plugins` on a normal install, or e.g. `/opt/data/plugins` in some Docker images. Find it with `find / -name manifest.json -path "*dashboard*"`. The plugin **auto-detects its own API mount path** from where the dashboard serves it, so it works regardless of the folder name (clone-named, `briefing`, anything) — naming it `briefing` just keeps the URLs tidy.


Final layout (the standard Hermes plugin contract — a `dashboard/` subfolder):

```text
~/.hermes/plugins/briefing/
└── dashboard/
    ├── manifest.json
    ├── plugin_api.py          # the whole plugin in one file: routes + aggregation + renderers + CLI
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

# Pre-build the last N days (the dashboard does this for you on first open)
python plugin_api.py bootstrap 7
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
| `pricing` | $ (USD) per 1M tokens per model. **Verify against your provider** — defaults are placeholders, so cost is rendered with `≈`. |
| `approval_keywords` | Substrings in a `blocked` reason that mean "a human must decide". German + English defaults included. |
| `protocol_violation_alert_threshold` | Flag a task as _Instabil_ after this many violations in a window. |
| `schedule` | Local `HH:MM` times your timer/cron runs — drives the "next build" hint in the dashboard. |
| `language` | `en` (default) or `de`. |
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
| `/ensure?days=N` | GET | build the last N days on demand (today first) |
| `/status` | GET | build-in-progress state, next scheduled run, last built |
| `/build` | POST | background build — `{"days":N}` bootstraps, `{"date":"…"}` rebuilds one day |
| `/range?from_=&to=` | GET | weekly/monthly roll-up |
| `/decisions` | GET | open "needs your hand" items |
| `/decisions/{id}/resolve` | POST/GET | resolve a decision; GET form `?resolution=ok\|veto` for reliability |

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

## Troubleshooting

**The Briefing tab shows but every request 404s** (`No such API endpoint: /api/plugins/briefing/status`). The tab (frontend) is picked up by a plugin *rescan*, but **backend API routes mount only when `hermes dashboard` starts** — a `rescan` is not enough. Fully restart the dashboard:

```bash
# stop the running dashboard, then:
hermes dashboard
```

Then hard-refresh the browser (Ctrl/Cmd+Shift+R). If it still 404s:

1. Confirm the file exists at `~/.hermes/plugins/briefing/dashboard/plugin_api.py` and the manifest has `"api": "plugin_api.py"`.
2. Open `/api/plugins/briefing/health` in the browser. If the plugin loaded but a dependency failed, it returns the **Python traceback** here (instead of a 404) — copy that into an issue.
3. Tail `~/.hermes/logs/errors.log` for `Failed to load plugin briefing API routes`.
4. In Docker, a `docker restart <container>` must actually restart the dashboard process. Verify after: open `/api/plugins/briefing/health` — `{"ok": true}` means the routes mounted. The backend is a single self-contained `plugin_api.py` (no sub-packages), so if the bundled kanban plugin's API works, this one mounts the same way.

**The tab is empty / no briefings.** It builds today on first open via GET. If that 404s, it's the mount problem above. Once routes are mounted, opening the tab (or `python plugin_api.py render`) builds on demand.

---

## License

[MIT](LICENSE) — same license as Hermes Agent and the official example plugins.

---

<sub>Keywords: Hermes Agent dashboard plugin · multi-agent kanban report · daily standup digest · agent activity report · LLM cost tracking · human-in-the-loop approvals · Nous Research Hermes · self-hosted AI agent orchestration · kanban daily report plugin.</sub>
