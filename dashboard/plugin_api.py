"""Hermes Reports — backend routes + CLI.

The dashboard loader imports this file and looks for a module-level
`router = APIRouter()`. Routes mount under /api/plugins/reports/.

It also runs standalone so you can get the Markdown report with zero dashboard
wiring:

    python plugin_api.py inspect-schema       # verify column auto-detection
    python plugin_api.py render [YYYY-MM-DD]   # print today's (or a day's) report
    python plugin_api.py build  [YYYY-MM-DD]   # (re)build + cache a digest
    python plugin_api.py range  FROM TO        # weekly/monthly roll-up
"""
from __future__ import annotations

import os
import sys

# make the sibling `reports` package importable however the loader pulls us in
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reports.config import load_config                       # noqa: E402
from reports.aggregate import build_digest, build_range, today_str  # noqa: E402
from reports.render_md import render_day, render_range       # noqa: E402
from reports.store import Store                               # noqa: E402
from reports.kanban_source import KanbanSource               # noqa: E402

try:
    from fastapi import APIRouter, Body, HTTPException
    from fastapi.responses import PlainTextResponse
    router = APIRouter()
except Exception:  # CLI-only environment without fastapi
    router = None


# ---------------------------------------------------------------- routes

if router is not None:

    @router.get("/health")
    async def health():
        cfg = load_config()
        return {"ok": True, "kanban_db": str(cfg.resolved_kanban_db()),
                "db_exists": cfg.resolved_kanban_db().exists(),
                "llm_enabled": cfg.llm.enabled}

    @router.get("/schema")
    async def schema():
        src = KanbanSource(load_config())
        try:
            return src.inspect_schema()
        finally:
            src.close()

    @router.get("/digests")
    async def digests(limit: int = 60):
        store = Store(load_config())
        try:
            return {"digests": store.list_digests(limit)}
        finally:
            store.close()

    @router.get("/digest/{date}")
    async def digest(date: str, rebuild: bool = False):
        cfg = load_config()
        store = Store(cfg)
        try:
            cached = None if rebuild else store.get_digest(date)
        finally:
            store.close()
        return cached or build_digest(cfg, date)

    @router.get("/render/{date}", response_class=PlainTextResponse)
    async def render(date: str, rebuild: bool = False):
        cfg = load_config()
        date = today_str(cfg.timezone) if date in ("today", "heute") else date
        store = Store(cfg)
        try:
            d = None if rebuild else store.get_digest(date)
        finally:
            store.close()
        d = d or build_digest(cfg, date)
        return render_day(d, cfg.timezone)

    @router.get("/range")
    async def range_(from_: str, to: str):
        return build_range(load_config(), from_, to)

    @router.get("/decisions")
    async def decisions():
        store = Store(load_config())
        try:
            return {"decisions": store.open_decisions()}
        finally:
            store.close()

    @router.post("/decisions/{decision_id}/resolve")
    async def resolve(decision_id: str, body: dict = Body(default={})):
        resolution = (body or {}).get("resolution", "ok")
        store = Store(load_config())
        try:
            ok = store.resolve_decision(decision_id, resolution)
        finally:
            store.close()
        if not ok:
            raise HTTPException(404, "decision not open or not found")
        return {"ok": True, "id": decision_id, "resolution": resolution}


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
        print(render_day(build_digest(cfg, date), cfg.timezone))
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
        print(render_range(build_range(cfg, argv[1], argv[2])))
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
