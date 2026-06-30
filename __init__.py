"""Hermes Briefing — plugin entry point.

The whole product is the DASHBOARD plugin in ``dashboard/`` (a Day/Week/Month
briefing tab plus its build-on-demand API). This root module exists for two
reasons only:

1. ``hermes plugins install <user>/Hermes-Briefing-Plugin`` reads ``name`` from
   ``plugin.yaml`` here and installs the repo into ``~/.hermes/plugins/briefing/``
   (matching the dashboard manifest name) instead of the repo name.
2. It lets the plugin be enabled like any other (``hermes plugins enable
   briefing``), even though the dashboard tab is discovered purely via
   ``dashboard/manifest.json`` and needs no enable entry.

There are no agent-side tools or hooks: briefings are built on demand by the
dashboard backend (``dashboard/plugin_api.py``) and on a schedule by
``dashboard/send_report.py``. ``register`` is therefore an intentional no-op.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:  # ctx type is provided by the Hermes host
    """No-op: this plugin's only surface is the dashboard tab in ``dashboard/``."""
    logger.debug("hermes-briefing: dashboard-only plugin; nothing to register")
