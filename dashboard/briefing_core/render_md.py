"""Render a digest to crisp, bullet-style Markdown (English by default).

Quiet days collapse to a single header line. Everything stays short. Set
`language: de` in config for the original German labels.
"""
from __future__ import annotations

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
