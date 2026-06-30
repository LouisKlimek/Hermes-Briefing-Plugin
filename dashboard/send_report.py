#!/usr/bin/env python3
"""Build today's digest, render Markdown, and email it.

Run from cron or a systemd timer (see systemd/). Reads the same config as the
plugin. Sends nothing if SMTP isn't configured — it just prints the report, so
you can pipe it elsewhere.

    python send_report.py                 # today
    python send_report.py 2026-06-26      # a specific day
    python send_report.py --stdout        # print only, never send
"""
from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_api import load_config, build_digest, today_str, render_day  # noqa: E402


def send_email(cfg, subject: str, body_md: str) -> None:
    s = cfg.smtp
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.sender or s.user
    msg["To"] = s.recipient
    msg.set_content(body_md)
    msg.add_alternative(
        f"<pre style=\"font-family:ui-monospace,Menlo,monospace;font-size:13px;"
        f"line-height:1.5;white-space:pre-wrap\">{_html_escape(body_md)}</pre>",
        subtype="html",
    )
    with smtplib.SMTP(s.host, s.port, timeout=30) as server:
        if s.starttls:
            server.starttls()
        if s.user:
            server.login(s.user, s.password)
        server.send_message(msg)


def _html_escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main(argv: list[str]) -> int:
    cfg = load_config()
    stdout_only = "--stdout" in argv
    args = [a for a in argv if not a.startswith("--")]
    date = args[0] if args else today_str(cfg.timezone)

    digest = build_digest(cfg, date)
    body = render_day(digest, cfg.timezone, cfg.language)
    if cfg.language == "de":
        subject = f"Tagesbericht {date} · {digest['header']['open']} offen · ≈${digest['header']['cost_eur']:.2f}"
    else:
        subject = f"Briefing {date} · {digest['header']['open']} open · ≈${digest['header']['cost_eur']:.2f}"

    if stdout_only or not (cfg.smtp.host and cfg.smtp.recipient):
        if not stdout_only:
            sys.stderr.write("SMTP not configured — printing instead.\n")
        print(body)
        return 0

    send_email(cfg, subject, body)
    sys.stderr.write(f"sent: {subject}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
