"""Render a digest to crisp, bullet-style Markdown (the format Peter liked).

Quiet days collapse to a single header line. Everything stays short.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def _bar(frac: float, width: int = 16) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)


_KIND_LABEL = {
    "approval": "Freigabe nötig",
    "blocked": "Blockiert",
    "failed": "Aufgegeben",
    "instability": "Instabil",
}


def render_day(digest: dict, tz: str = "Europe/Berlin") -> str:
    h = digest["header"]
    cost = digest["cost"]
    date = digest["date"]
    try:
        weekday = datetime.strptime(date, "%Y-%m-%d").strftime("%a")
    except Exception:
        weekday = ""
    dd = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m") if date else date

    open_n = h["open"]
    head = f"{dd} · {h['status']} · {open_n} offen · ≈ {cost['today_eur']:.2f} € / {cost['budget_daily']:.0f} €"

    # quiet day -> one line
    if h["status"] == "ruhig" and not digest["hand"] and not digest["done"]:
        tail = " · alles in Ordnung" if digest["system"]["stable"] else ""
        return f"{head}{tail}\n"

    lines = [head, ""]

    if digest["hand"]:
        lines.append("▸ DEINE HAND")
        for d in digest["hand"]:
            label = _KIND_LABEL.get(d["kind"], "Achtung")
            lines.append(f"  • {label}: {d['title']}")
            if d.get("detail"):
                lines.append(f"    {d['detail']}")
            if d.get("deadline"):
                dl = _fmt_deadline(d["deadline"], tz)
                if dl:
                    lines.append(f"    Stopp-Fenster: {dl}")
        lines.append("")

    lines.append("▸ PROTOKOLL")
    if digest["done"]:
        for item in digest["done"]:
            b = item["bullets"][0] if item["bullets"] else (item.get("why") or "fertig")
            lines.append(f"  Fertig    {item['title']} — {b}")
    if digest["in_progress"]:
        names = ", ".join(t["title"] for t in digest["in_progress"][:6])
        more = f" (+{len(digest['in_progress']) - 6})" if len(digest["in_progress"]) > 6 else ""
        lines.append(f"  In Arbeit {names}{more}")
    for l in digest.get("learned", []):
        lines.append(f"  Notiert   {l}")

    # costs
    cd = cost["today_eur"] / cost["budget_daily"] if cost["budget_daily"] else 0
    cm = cost["month_eur"] / cost["budget_monthly"] if cost["budget_monthly"] else 0
    near = "  ← knapp am Limit" if cd >= 0.8 else ""
    lines.append(
        f"  Kosten    heute ≈{cost['today_eur']:.2f} €/{cost['budget_daily']:.0f} € · "
        f"Monat ≈{cost['month_eur']:.2f} €/{cost['budget_monthly']:.0f} € · {cost['runs']} Runs{near}"
    )
    if cost.get("caveat"):
        lines.append(f"            ⚠ {cost['caveat']}")

    # system
    if digest["system"]["stable"]:
        lines.append("  System    stabil")
    else:
        lines.append("  System    " + ", ".join(digest["system"]["notes"]))

    return "\n".join(lines) + "\n"


def render_range(roll: dict, title: str = "Bericht") -> str:
    lines = [f"{title} · {roll['from']} – {roll['to']}", ""]
    lines.append(f"  Kosten    ≈ {roll['cost_eur']:.2f} € gesamt")
    lines.append(f"  Fertig    {len(roll['done'])} Aufgaben")
    st = roll.get("decision_stats", {})
    if st:
        lines.append(
            f"  Hand      {st.get('total', 0)} Entscheidungen · "
            f"{st.get('vetoed', 0)} vetot · {st.get('expired', 0)} abgelaufen · "
            f"{st.get('open', 0)} offen"
        )
    if roll["hand"]:
        lines.append("")
        lines.append("▸ NOCH OFFEN")
        for d in roll["hand"]:
            lines.append(f"  • {d['title']}")
    if roll["done"]:
        lines.append("")
        lines.append("▸ ERLEDIGT")
        for item in roll["done"][:20]:
            b = item["bullets"][0] if item.get("bullets") else item.get("why", "")
            lines.append(f"  • {item['title']} — {b}")
    if roll.get("learned"):
        lines.append("")
        lines.append("▸ GELERNT")
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
