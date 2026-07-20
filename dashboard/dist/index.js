(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var hooks = SDK.hooks || {};
  var useState = hooks.useState, useEffect = hooks.useEffect, useCallback = hooks.useCallback, useRef = hooks.useRef;
  var C = SDK.components || {};
  var Card = C.Card, CardHeader = C.CardHeader, CardTitle = C.CardTitle, CardContent = C.CardContent;
  var Badge = C.Badge, Button = C.Button, Separator = C.Separator;
  var fetchJSON = SDK.fetchJSON;
  var timeAgo = (SDK.utils && SDK.utils.timeAgo) || function (t) { return new Date(t * 1000).toLocaleString(); };

  // The dashboard mounts a plugin's API under its INSTALL DIRECTORY name
  // (/api/plugins/<dir>/), and serves this bundle from /dashboard-plugins/<dir>/dist/.
  // So we derive the base from our own <script src> and verify it with /health —
  // that way the plugin works no matter what the install folder is called
  // (briefing, Hermes-Briefing-Plugin, a git-clone name, anything).
  function apiCandidates() {
    var c = [];
    try {
      var scripts = document.querySelectorAll("script[src]");
      for (var i = 0; i < scripts.length; i++) {
        var m = (scripts[i].src || "").match(/\/dashboard-plugins\/([^\/]+)\//);
        if (m && c.indexOf(m[1]) < 0) c.push(m[1]);
      }
      if (document.currentScript && document.currentScript.src) {
        var m2 = document.currentScript.src.match(/\/dashboard-plugins\/([^\/]+)\//);
        if (m2 && c.indexOf(m2[1]) < 0) c.push(m2[1]);
      }
    } catch (e) {}
    ["briefing", "Hermes-Briefing-Plugin"].forEach(function (n) { if (c.indexOf(n) < 0) c.push(n); });
    return c;
  }
  function addBoard(url, b) {
    b = b || "all";
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + "board=" + encodeURIComponent(b);
  }
  function resolveApi() {
    var cands = apiCandidates();
    return cands.reduce(function (p, name) {
      return p.then(function (found) {
        if (found) return found;
        return fetchJSON("/api/plugins/" + name + "/health")
          .then(function () { return "/api/plugins/" + name; })
          .catch(function () { return null; });
      });
    }, Promise.resolve(null)).then(function (found) { return found || "/api/plugins/" + cands[0]; });
  }

  var KIND_LABEL = { approval: "Needs approval", blocked: "Blocked", failed: "Gave up", instability: "Unstable" };
  var KIND_TONE = { approval: "default", blocked: "secondary", failed: "destructive", instability: "destructive" };
  // The kanban board's colors live in ITS stylesheet as .hermes-kanban-dot-<status>
  // rules. We fetch and parse that CSS at runtime (see loadKanbanCss) into
  // KANBAN_CSS_COLORS so the briefing always tracks the real board colors — even
  // across CSS updates. STATUS_COLORS below is only an offline fallback and is
  // kept in sync with the current kanban CSS values.
  var KANBAN_CSS_COLORS = {};
  var STATUS_COLORS = {
    triage: "#b47dd6", todo: "#9ca3af", scheduled: "#818cf8", ready: "#d4b348",
    running: "#3fb97d", blocked: "#d14a4a", review: "#c084fc", done: "#4a8cd1", archived: "#6b7280",
    // derived / bucket aliases used by the briefing:
    approval: "#d4b348", failed: "#d14a4a", instability: "#d14a4a", violation: "#c084fc",
    active: "#3fb97d", completed: "#4a8cd1", complete: "#4a8cd1", "in_progress": "#3fb97d",
    error: "#d14a4a", gave_up: "#d14a4a", new: "#9ca3af", backlog: "#9ca3af"
  };
  var KANBAN_COLORS = {};
  var KIND_TO_STATUS = {
    blocked: ["blocked"],
    approval: ["ready", "review", "blocked"],
    failed: ["blocked", "archived"],
    instability: ["blocked"],
    violation: ["review", "blocked"],
    done: ["done", "completed", "complete", "archived"],
    active: ["running", "in_progress", "review", "claimed", "doing"],
    todo: ["todo", "ready", "triage", "scheduled", "backlog", "new"]
  };

  // Build the URL(s) to the kanban plugin's stylesheet, derived from our own
  // <script src> so it survives reverse-proxy prefixes and custom install dirs.
  function kanbanCssUrls() {
    var urls = [];
    try {
      var scripts = document.querySelectorAll("script[src]");
      for (var i = 0; i < scripts.length; i++) {
        var src = scripts[i].src || "";
        var idx = src.indexOf("/dashboard-plugins/");
        if (idx >= 0) {
          var u = src.slice(0, idx) + "/dashboard-plugins/kanban/dist/style.css";
          if (urls.indexOf(u) < 0) urls.push(u);
        }
      }
    } catch (e) {}
    ["/dashboard-plugins/kanban/dist/style.css"].forEach(function (u) { if (urls.indexOf(u) < 0) urls.push(u); });
    return urls;
  }
  function parseKanbanCss(text) {
    var map = {}, re = /\.hermes-kanban-dot-([a-z0-9_]+)\s*\{[^}]*?background\s*:\s*([^;}]+)[;}]/gi, m;
    while ((m = re.exec(text))) { map[canonStatus(m[1])] = m[2].trim(); }
    return map;
  }
  function loadKanbanCss(bump) {
    var urls = kanbanCssUrls();
    (function tryNext(i) {
      if (i >= urls.length) return;
      fetch(urls[i]).then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (t) {
          var map = parseKanbanCss(t);
          if (Object.keys(map).length) { KANBAN_CSS_COLORS = map; if (bump) bump(); }
          else tryNext(i + 1);
        })
        .catch(function () { tryNext(i + 1); });
    })(0);
  }
  function canonStatus(s) { return String(s || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, ""); }
  function kanbanColorFor(kind) {
    var k = (kind || "").toLowerCase();
    var cands = KIND_TO_STATUS[k] || [k];
    for (var i = 0; i < cands.length; i++) { if (KANBAN_COLORS[cands[i]]) return KANBAN_COLORS[cands[i]]; }
    return null;
  }
  // Resolution order: live kanban CSS (authoritative, tracks board updates) →
  // offline palette → DB-discovered custom colors → kind mapping. Values may be
  // raw CSS (hex OR var(--token)); both resolve correctly inline in the dashboard.
  function resolveColor(status, kind) {
    if (status) {
      var cs = canonStatus(status);
      if (KANBAN_CSS_COLORS[cs]) return KANBAN_CSS_COLORS[cs];
      if (STATUS_COLORS[cs]) return STATUS_COLORS[cs];
      if (KANBAN_COLORS[cs]) return KANBAN_COLORS[cs];
    }
    var kc = kanbanCssColorFor(kind) || kanbanColorFor(kind || status); if (kc) return kc;
    if (kind && STATUS_COLORS[canonStatus(kind)]) return STATUS_COLORS[canonStatus(kind)];
    return "#71717a";
  }
  function kanbanCssColorFor(kind) {
    var k = (kind || "").toLowerCase();
    var cands = KIND_TO_STATUS[k] || [k];
    for (var i = 0; i < cands.length; i++) { if (KANBAN_CSS_COLORS[cands[i]]) return KANBAN_CSS_COLORS[cands[i]]; }
    return null;
  }
  function statusColor(kind) { return resolveColor(null, kind); }
  function colorChrome(c) {
    var isHex = /^#([0-9a-fA-F]{6})$/.test(c);
    return { color: c, background: isHex ? c + "22" : "transparent", border: "1px solid " + (isHex ? c + "77" : c) };
  }
  function StatusBadge(props) {
    var ch = colorChrome(resolveColor(props.status, props.kind));
    return h("span", { style: Object.assign({
      display: "inline-block", fontSize: "0.64rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em",
      padding: "0.14rem 0.5rem", borderRadius: "999px", whiteSpace: "nowrap" }, ch) }, props.label);
  }
  var MUTED = "var(--color-muted-foreground)";

  function eur(n) { return "\u2248 $" + (Number(n) || 0).toFixed(2); }
  function ymd(d, tz) {
    try { return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(d); }
    catch (e) { return d.toISOString().slice(0, 10); }
  }
  function daysAgo(n) { return new Date(Date.now() - n * 86400000); }

  function nextBuildLabel(nextRun) {
    if (!nextRun || !nextRun.iso) return null;
    var d = new Date(nextRun.iso); if (isNaN(d.getTime())) return null;
    var now = new Date();
    var when = d.toDateString() === now.toDateString() ? "today"
      : d.toDateString() === new Date(now.getTime() + 86400000).toDateString() ? "tomorrow"
      : d.toLocaleDateString();
    return when + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function Section(props) {
    var s = useState(props.defaultCollapsed ? false : true);
    var open = s[0], setOpen = s[1];
    return h("div", { className: "brf-fade-in", style: { marginBottom: "0.6rem", border: "1px solid var(--color-border)", borderRadius: "0.6rem", background: "var(--color-card)", overflow: "hidden" } },
      h("div", { onClick: function () { setOpen(!open); },
        style: { display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer", userSelect: "none", padding: "0.5rem 0.7rem" } },
        h("span", { style: { display: "inline-block", fontSize: "0.62rem", color: MUTED, transition: "transform .2s ease", transform: open ? "rotate(90deg)" : "rotate(0deg)" } }, "\u25B6"),
        h("span", { style: { flex: 1, fontSize: "0.72rem", letterSpacing: "0.08em", textTransform: "uppercase", color: MUTED, fontWeight: 600 } }, props.title)),
      open ? h("div", { style: { padding: "0 0.7rem 0.65rem" } }, props.children) : null);
  }

  function Skeleton() {
    function bar(w, hgt, mb) { return h("div", { className: "brf-skel", style: { height: (hgt || 12) + "px", width: w, marginBottom: (mb == null ? 8 : mb) + "px" } }); }
    return h("div", { className: "brf-fade-in", style: { paddingLeft: "1rem", flex: 1 } },
      bar("38%", 18, 16), bar("88%"), bar("64%"), bar("72%"),
      h("div", { style: { height: "12px" } }), bar("80%"), bar("52%"));
  }

  function Spinner(props) { return h("span", { className: "brf-spinner" + (props && props.lg ? " brf-spinner-lg" : ""), style: props && props.style }); }

  function TabButton(props) {
    var active = props.active;
    return h("button", {
      onClick: props.onClick,
      className: "brf-card",
      style: {
        border: "1px solid var(--color-border)", borderRadius: "999px",
        padding: "0.25rem 0.9rem", fontSize: "0.82rem", cursor: "pointer",
        background: active ? "var(--color-primary, var(--color-accent))" : "transparent",
        color: active ? "var(--color-primary-foreground, var(--color-accent-foreground))" : "inherit"
      }
    }, props.label);
  }

  // Where a ticket opens. If the Hermes TaskList plugin is installed we deep-link
  // to its tab (opens the task popup); otherwise we fall back to the Kanban board.
  // Detected at runtime via /api/dashboard/plugins, so this works standalone too.
  function ticketHref(taskId, target) {
    var ns = taskId || "";
    var i = ns.indexOf("::");
    var localId = i >= 0 ? ns.slice(i + 2) : ns;
    var board = i >= 0 ? ns.slice(0, i) : "";
    var root = (window.location.pathname || "").replace(/\/briefing(\/.*)?$/, "");
    var path = (target && target.path) || "/kanban";
    var url = root + path + (localId ? "?task=" + encodeURIComponent(localId) : "");
    if (localId && board) url += "&board=" + encodeURIComponent(board);
    return { url: url, localId: localId, board: board, kind: (target && target.kind) || "kanban" };
  }
  function detectTicketTarget(fetchJSON) {
    return fetchJSON("/api/dashboard/plugins").then(function (list) {
      list = Array.isArray(list) ? list : (list && list.plugins) || [];
      function find(n) { return list.filter(function (p) { return p && p.name === n; })[0]; }
      var tl = find("tasklist");
      if (tl) return { path: (tl.tab && tl.tab.path) || "/list", kind: "tasklist" };
      var kb = find("kanban");
      return { path: (kb && kb.tab && kb.tab.path) || "/kanban", kind: "kanban" };
    }).catch(function () { return { path: "/kanban", kind: "kanban" }; });
  }

  function HandItem(props) {
    var d = props.d;
    var t = ticketHref(d.task_id, props.target);
    return h("div", { className: "brf-card brf-fade-in", style: {
        border: "1px solid var(--color-border)", borderRadius: "var(--radius, 0.5rem)",
        padding: "0.6rem 0.75rem", marginBottom: "0.5rem", background: "var(--color-card)" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.25rem" } },
        h(StatusBadge, { kind: d.kind, status: d.status, label: KIND_LABEL[d.kind] || d.kind }),
        h("strong", { style: { fontSize: "0.9rem" } }, d.title)),
      d.detail ? h("div", { style: { fontSize: "0.8rem", color: MUTED, marginBottom: "0.4rem" } }, d.detail) : null,
      h("div", { style: { display: "flex", gap: "0.6rem", alignItems: "center" } },
        h("a", { href: t.url,
                 style: { fontSize: "0.8rem", fontWeight: 600, textDecoration: "none",
                          border: "1px solid var(--color-border)", borderRadius: "0.4rem",
                          padding: "0.2rem 0.6rem", color: "inherit", background: "var(--color-card)" } },
          "Open ticket \u2192"),
        (t.board || t.localId)
          ? h("span", { style: { fontSize: "0.72rem", color: MUTED } },
              (t.board ? t.board + " · " : "") + t.localId)
          : null));
  }

  function MiniBars(props) {
    var rows = props.rows || [], field = props.field || "cost";
    var vals = rows.map(function (r) { return r[field] || 0; });
    var max = Math.max.apply(null, vals.concat([0.0001]));
    var H = 64; // px height available for a full bar
    return h("div", { style: { display: "flex", alignItems: "flex-end", gap: "3px", height: (H + 16) + "px", marginTop: "0.4rem" } },
      rows.map(function (r, i) {
        var v = r[field] || 0;
        var px = v > 0 ? Math.max(4, Math.round((v / max) * H)) : 0;
        return h("div", { key: r.date, title: r.date + ": " + v, style: { flex: 1, display: "flex", flexDirection: "column", justifyContent: "flex-end", alignItems: "center", height: "100%" } },
          h("div", { style: { width: "72%", height: px + "px", borderRadius: "3px 3px 0 0",
              background: "var(--color-primary, var(--color-accent-foreground, #6b8afd))",
              transition: "height .5s ease", transitionDelay: (i * 20) + "ms" } }),
          h("div", { style: { fontSize: "0.6rem", color: MUTED, marginTop: "2px" } }, r.date.slice(8)));
      }));
  }

  function fmtNum(n) { n = Number(n) || 0; try { return n.toLocaleString("en-US"); } catch (e) { return "" + n; } }
  function fmtMin(m) { m = Math.round(Number(m) || 0); if (m < 60) return "~" + m + "m"; var hh = Math.floor(m / 60), mm = m % 60; return "~" + hh + "h" + (mm ? " " + mm + "m" : ""); }
  function fmtHour(h24) { if (h24 == null) return ""; var ap = h24 < 12 ? "AM" : "PM"; var h12 = (h24 % 12) || 12; return h12 + ap; }

  function InsightsBlock(props) {
    var ins = props.insights || {};
    if (!ins.available) return h("div", { style: { fontSize: "0.82rem", color: MUTED } },
      props.stable ? "Stable \u00b7 no session analytics for this period." : "\u2014");
    var o = ins.overview || {}, accent = "var(--color-primary, #6b8afd)";
    function stat(label, val) {
      return h("div", { style: { padding: "0.65rem 0.9rem", border: "1px solid var(--color-border)", borderRadius: "0.6rem", background: "var(--color-card)", minWidth: "6rem" } },
        h("div", { style: { fontSize: "1.3rem", fontWeight: 700, lineHeight: 1.1 } }, val),
        h("div", { style: { fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.05em", color: MUTED, marginTop: "0.15rem" } }, label));
    }
    var stats = h("div", { style: { display: "flex", flexWrap: "wrap", gap: "0.6rem", marginBottom: "0.85rem" } },
      stat("Sessions", fmtNum(o.sessions)), stat("Messages", fmtNum(o.messages)),
      stat("Tool calls", fmtNum(o.tool_calls)), stat("User msgs", fmtNum(o.user_messages)),
      stat("Total tokens", fmtNum(o.total_tokens)), stat("Active time", fmtMin(o.active_minutes)),
      stat("Avg session", (o.avg_session_min || 0) + "m"), stat("Msgs/sess", o.avg_msgs || 0));

    var inTok = o.input_tokens || 0, outTok = o.output_tokens || 0, tot = Math.max(1, inTok + outTok);
    var tokenBar = h("div", { style: { marginBottom: "0.8rem" } },
      h("div", { style: { display: "flex", justifyContent: "space-between", fontSize: "0.66rem", color: MUTED, marginBottom: "0.22rem" } },
        h("span", null, "Input " + fmtNum(inTok)), h("span", null, "Output " + fmtNum(outTok))),
      h("div", { style: { display: "flex", height: "10px", borderRadius: "999px", overflow: "hidden", background: "var(--color-card)", border: "1px solid var(--color-border)" } },
        h("div", { style: { width: (inTok / tot * 100) + "%", background: accent, transition: "width .5s ease" } }),
        h("div", { style: { width: (outTok / tot * 100) + "%", background: "#d4b348", transition: "width .5s ease" } })));

    var wd = ins.weekday || [0, 0, 0, 0, 0, 0, 0], wmax = Math.max.apply(null, wd.concat([1]));
    var labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], H = 52;
    var weekChart = h("div", null,
      h("div", { style: { fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.05em", color: MUTED, marginBottom: "0.35rem" } },
        "Activity by weekday" + (ins.peak_hour != null ? " \u00b7 peak " + fmtHour(ins.peak_hour) : "") + " \u00b7 " + (ins.active_days || 0) + " active day" + ((ins.active_days === 1) ? "" : "s")),
      h("div", { style: { display: "flex", alignItems: "flex-end", gap: "4px", height: (H + 20) + "px" } },
        wd.map(function (v, i) {
          var px = v > 0 ? Math.max(4, Math.round(v / wmax * H)) : 0;
          return h("div", { key: i, title: labels[i] + ": " + v, style: { flex: 1, display: "flex", flexDirection: "column", justifyContent: "flex-end", alignItems: "center", height: "100%" } },
            h("div", { style: { fontSize: "0.58rem", color: MUTED, marginBottom: "2px" } }, v || ""),
            h("div", { style: { width: "68%", height: px + "px", borderRadius: "3px 3px 0 0", background: accent, transition: "height .5s ease", transitionDelay: (i * 25) + "ms" } }),
            h("div", { style: { fontSize: "0.56rem", color: MUTED, marginTop: "2px" } }, labels[i]));
        })));

    function hbars(title, rows, nameKey, valKey, unit, color) {
      if (!rows || !rows.length) return null;
      var mx = Math.max.apply(null, rows.map(function (r) { return r[valKey] || 0; }).concat([1]));
      return h("div", { style: { flex: "1 1 240px", minWidth: 0 } },
        h("div", { style: { fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.05em", color: MUTED, marginBottom: "0.35rem" } }, title),
        rows.slice(0, 6).map(function (r, i) {
          return h("div", { key: i, style: { marginBottom: "0.32rem" } },
            h("div", { style: { display: "flex", justifyContent: "space-between", gap: "0.5rem", fontSize: "0.72rem", marginBottom: "1px" } },
              h("span", { style: { fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, r[nameKey]),
              h("span", { style: { color: MUTED, flex: "0 0 auto" } }, fmtNum(r[valKey]) + " " + unit)),
            h("div", { style: { height: "6px", borderRadius: "999px", background: "var(--color-card)", border: "1px solid var(--color-border)", overflow: "hidden" } },
              h("div", { style: { width: ((r[valKey] || 0) / mx * 100) + "%", height: "100%", background: color, transition: "width .5s ease" } })));
        }));
    }
    var breakdown = h("div", { style: { display: "flex", flexWrap: "wrap", gap: "1.4rem", marginTop: "0.85rem" } },
      hbars("Models \u00b7 tokens", ins.by_model, "model", "tokens", "tok", accent),
      hbars("Platforms \u00b7 sessions", ins.by_platform, "platform", "sessions", "sess", "#3fb97d"));

    return h("div", null, stats, tokenBar, weekChart, breakdown);
  }

  function LearnedCards(props) {
    var items = props.items || [];
    return h("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: "0.55rem" } },
      items.map(function (l, i) {
        var text = (typeof l === "string") ? l : (l && l.text) || "";
        var tid = (l && l.task_id) || "";
        var title = (l && l.title) || "";
        var t = ticketHref(tid, props.target);
        return h("div", { key: i, className: "brf-card", style: { position: "relative", border: "1px solid var(--color-border)", borderLeft: "3px solid var(--color-primary, var(--color-accent-foreground, #6b8afd))", borderRadius: "0.55rem", padding: "0.55rem 0.7rem", background: "var(--color-card)" } },
          h("div", { style: { fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.07em", color: "var(--color-primary, #6b8afd)", fontWeight: 700, marginBottom: "0.2rem" } }, "\uD83D\uDCA1 Insight"),
          h("div", { style: { fontSize: "0.81rem", lineHeight: 1.5 } }, text),
          (title || tid) ? h("div", { style: { display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "0.45rem" } },
            title ? h("span", { style: { fontSize: "0.72rem", color: MUTED, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, title) : null,
            tid ? h("a", { href: t.url, style: { marginLeft: "auto", fontSize: "0.72rem", fontWeight: 600, textDecoration: "none", color: "inherit", border: "1px solid var(--color-border)", borderRadius: "0.4rem", padding: "0.08rem 0.45rem", whiteSpace: "nowrap" } }, "Open ticket \u2192") : null) : null);
      }));
  }

  function kpiCell(label, val, color, key) {
    return h("div", { key: key, style: { padding: "0.65rem 0.9rem", border: "1px solid var(--color-border)", borderRadius: "0.6rem", background: "var(--color-card)" } },
      h("div", { style: { fontSize: "1.45rem", fontWeight: 700, color: color || "inherit", lineHeight: 1.1 } }, val),
      h("div", { style: { fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.05em", color: MUTED, marginTop: "0.15rem" } }, label));
  }
  function KpiGrid(props) {
    var ov = props.overview || {}, k = ov.kpis || {}, ct = ov.counters || {};
    var tiles = [
      ["Done", k.done || 0, "#3fb97d"],
      ["New", k.new || 0, null],
      ["Blocked", k.blocked || 0, k.blocked ? "#d14a4a" : null],
      ["Profiles", k.active_profiles || 0, null],
      ["Lessons", ct.lessons || 0, null],
      ["Skill/SOUL", ct.skill_soul || 0, null]
    ];
    return h("div", { style: { display: "grid", gridTemplateColumns: "repeat(3, minmax(5.4rem, 1fr))", gap: "0.6rem", flex: "0 0 auto", minWidth: "17rem" } },
      tiles.map(function (t, i) { return kpiCell(t[0], t[1], t[2], i); }));
  }
  function lightColor(light) { return light === "blocked" ? "#d14a4a" : light === "waiting" ? "#d4b348" : "#3fb97d"; }
  function lightWord(light) { return light === "blocked" ? "blocked" : light === "waiting" ? "waiting" : "on track"; }

  function Overview(props) {
    var ov = props.overview || {};
    return h("div", { style: { marginBottom: "0.85rem" } },
      (ov.board_lights && ov.board_lights.length) ? h("div", { style: { display: "flex", flexWrap: "wrap", gap: "0.4rem", marginBottom: "0.6rem" } },
        ov.board_lights.map(function (b, i) {
          var c = lightColor(b.light);
          return h("span", { key: i, style: { display: "inline-flex", alignItems: "center", gap: "0.35rem", fontSize: "0.76rem", padding: "0.16rem 0.55rem", borderRadius: "999px", border: "1px solid var(--color-border)", background: "var(--color-card)" } },
            h("span", { style: { width: "8px", height: "8px", borderRadius: "999px", background: c, boxShadow: "0 0 6px " + c + "88" } }),
            b.board, h("span", { style: { color: MUTED } }, lightWord(b.light)));
        })) : null);
  }

  function fmtDur(s) { if (s == null) return "\u2014"; return s >= 60 ? (s / 60).toFixed(1) + " min" : Math.round(s) + " s"; }
  function fmtTok(n) { if (!n) return "\u2014"; return n >= 1000 ? (n / 1000).toFixed(1) + "k" : "" + n; }
  function ModelsTable(props) {
    var m = props.models || {}, rows = m.by_profile || [], av = m.available || {};
    if (!rows.length) return h("div", { style: { fontSize: "0.82rem", color: MUTED } }, "No run/timing data found for this range.");
    var headers = [["Profile", "left"], ["Model", "left"], ["Runs", "right"]];
    if (av.latency) headers.push(["\u00d8 Latency", "right"]);
    if (av.tokens) headers.push(["Tokens", "right"]);
    if (av.thinking) headers.push(["Thinking", "right"]);
    if (av.cost) headers.push(["Cost", "right"]);
    var thStyle = { padding: "0.4rem 0.6rem", fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.05em", color: MUTED, fontWeight: 700, borderBottom: "1px solid var(--color-border)" };
    var tdStyle = { padding: "0.4rem 0.6rem", fontSize: "0.82rem", borderBottom: "1px solid var(--color-border)", verticalAlign: "top" };
    return h("div", { style: { overflowX: "auto" } },
      h("table", { style: { borderCollapse: "collapse", width: "100%", minWidth: "460px" } },
        h("thead", null, h("tr", null, headers.map(function (hd, i) {
          return h("th", { key: i, style: Object.assign({}, thStyle, { textAlign: hd[1] }) }, hd[0]); }))),
        h("tbody", null, rows.map(function (r, i) {
          var avg = r.dur_n ? r.dur_sum / r.dur_n : null;
          var thinkVal = (r.thinking != null && r.thinking !== "") ? String(r.thinking)
            : (r.thinking_runs ? r.thinking_runs + "/" + r.runs : "\u2014");
          var cells = [
            h("td", { key: "p", style: Object.assign({}, tdStyle, { fontWeight: 600 }) }, r.profile),
            h("td", { key: "m", style: tdStyle }, r.model || "\u2014"),
            h("td", { key: "r", style: Object.assign({}, tdStyle, { textAlign: "right" }) }, r.runs)
          ];
          if (av.latency) cells.push(h("td", { key: "l", style: Object.assign({}, tdStyle, { textAlign: "right" }) }, fmtDur(avg)));
          if (av.tokens) cells.push(h("td", { key: "t", style: Object.assign({}, tdStyle, { textAlign: "right" }) }, fmtTok(r.in_tok + r.out_tok)));
          if (av.thinking) cells.push(h("td", { key: "th", style: Object.assign({}, tdStyle, { textAlign: "right" }) }, thinkVal));
          if (av.cost) cells.push(h("td", { key: "c", style: Object.assign({}, tdStyle, { textAlign: "right" }) }, "$" + (r.cost || 0).toFixed(2)));
          return h("tr", { key: i }, cells);
        }))));
  }

  function boardOf(taskId) { var i = (taskId || "").indexOf("::"); return i >= 0 ? taskId.slice(0, i) : ""; }

  function BudgetBar(props) {
    var used = Number(props.used) || 0, budget = Number(props.budget) || 0;
    var ratio = budget > 0 ? used / budget : 0;
    var pct = Math.max(0, Math.min(100, Math.round(ratio * 100)));
    var col = ratio >= 0.9 ? "#ef4444" : ratio >= 0.7 ? "#f59e0b" : "#22c55e";
    var col2 = ratio >= 0.9 ? "#f87171" : ratio >= 0.7 ? "#fbbf24" : "#4ade80";
    return h("div", { style: { marginBottom: "0.55rem" } },
      h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "0.25rem" } },
        h("span", { style: { fontSize: "0.72rem", color: MUTED, textTransform: "uppercase", letterSpacing: "0.05em" } }, props.label),
        props.editable
          ? h(BudgetLimitEditor, { apiBase: props.apiBase, field: props.field, label: props.label + " limit", used: used, value: budget, onSaved: props.onSaved })
          : h("span", { style: { fontSize: "0.78rem" } }, eur(used) + " / $" + budget.toFixed(0)),
        h("span", { style: { marginLeft: "0.45rem", fontWeight: 700, color: col } }, pct + "%")),
      h("div", { style: { height: "9px", borderRadius: "999px", background: "var(--color-muted, rgba(127,127,127,0.18))", overflow: "hidden" } },
        h("div", { style: { height: "100%", width: pct + "%", borderRadius: "999px",
          background: "linear-gradient(90deg," + col + "," + col2 + ")",
          boxShadow: "0 0 8px " + col + "66",
          transition: "width .6s cubic-bezier(.4,0,.2,1)" } })));
  }


  function BudgetLimitEditor(props) {
    var sOpen = useState(false), open = sOpen[0], setOpen = sOpen[1];
    var sValue = useState(String(props.value == null ? "" : props.value)), value = sValue[0], setValue = sValue[1];
    var sError = useState(""), error = sError[0], setError = sError[1];
    var sSaving = useState(false), saving = sSaving[0], setSaving = sSaving[1];
    useEffect(function () { if (!open) setValue(String(props.value == null ? "" : props.value)); }, [props.value, open]);
    function close() { if (!saving) { setOpen(false); setError(""); } }
    function save() {
      var limit = Number(value);
      if (!value.trim() || !Number.isFinite(limit) || limit < 0) {
        setError("Enter a non-negative numeric " + props.label.toLowerCase() + " in EUR."); return;
      }
      setSaving(true); setError("");
      var body = {}; body[props.field] = limit;
      fetch(props.apiBase + "/budget-limits", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (body) { if (!r.ok) throw new Error(body.detail || "Could not save budget limit."); return body; }); })
        .then(function (limits) { setOpen(false); props.onSaved(limits); })
        .catch(function (err) { setError(err && err.message ? err.message : "Could not save budget limit."); })
        .then(function () { setSaving(false); });
    }
    return open
      ? h("div", { style: { display: "flex", alignItems: "center", gap: "0.3rem" } },
          h("input", { className: "brf-budget-limit-input", type: "number", min: "0", step: "any", value: value, onChange: function (e) { setValue(e.target.value); }, "aria-label": props.label + " in EUR", style: { width: "5.5rem" } }),
          h("button", { className: "brf-budget-limit-save", type: "button", onClick: save, disabled: saving }, saving ? "Saving…" : "Save"),
          h("button", { className: "brf-budget-limit-cancel", type: "button", onClick: close, disabled: saving, "aria-label": "Cancel editing " + props.label.toLowerCase() }, "Cancel"),
          error ? h("span", { role: "alert", style: { color: "#d14a4a", fontSize: "0.76rem" } }, error) : null)
      : h("div", { style: { display: "flex", alignItems: "baseline", gap: "0.3rem" } },
          h("span", { style: { fontSize: "0.78rem" } }, eur(props.used) + " / $" + (Number(props.value) || 0).toFixed(0)),
          h("button", { type: "button", onClick: function () { setOpen(true); }, title: "Edit " + props.label.toLowerCase(), "aria-label": "Edit " + props.label.toLowerCase(), style: { border: "0", background: "transparent", color: "inherit", cursor: "pointer", padding: "0", fontSize: "0.82rem" } }, "✎"));
  }

  function priorityColor(priority) {
    var value = String(priority == null ? "" : priority).toLowerCase();
    if (value === "urgent" || value === "high" || Number(priority) >= 3) return "#ef4444";
    if (value === "normal" || value === "medium" || Number(priority) === 2) return "#f59e0b";
    return "#6b7280";
  }
  function priorityLabel(priority) {
    if (priority == null || priority === "") return "No priority";
    var value = String(priority).toLowerCase();
    if (value === "urgent" || value === "high" || Number(priority) >= 3) return "High";
    if (value === "normal" || value === "medium" || Number(priority) === 2) return "Normal";
    return "Low";
  }
  function taskDate(ts) {
    if (!ts) return "—";
    try { return new Date(ts * 1000).toLocaleDateString(); } catch (e) { return "—"; }
  }
  function taskStatusBucket(status) {
    var value = canonStatus(status);
    if (/(done|complete|finish|close|resolve|archiv)/.test(value)) return "done";
    if (/(block|fail|timeout|error|review|approv|wait)/.test(value)) return "blocked";
    return null;
  }
  function TaskListChart(props) {
    var lists = {};
    (props.tasks || []).forEach(function (task) {
      var bucket = taskStatusBucket(task.status); if (!bucket) return;
      // A TaskList list is distinct from the Hermes board that stores it.
      // No membership (or no installed TaskList DB) is truthfully No List.
      var name = task.list || "No List";
      if (!lists[name]) lists[name] = { name: name, done: 0, blocked: 0 };
      lists[name][bucket]++;
    });
    var rows = Object.keys(lists).map(function (name) { return lists[name]; });
    if (!rows.length) return null;
    var max = Math.max.apply(null, rows.map(function (row) { return row.done + row.blocked; }).concat([1]));
    return h("section", { className: "brf-task-chart", "aria-label": "Task transitions by list" },
      h("div", { className: "brf-task-chart-title" }, "Task transitions by list"),
      h("div", { className: "brf-task-chart-legend" }, h("span", null, h("i", { className: "brf-task-dot", style: { background: "#22c55e" } }), " Done"), h("span", null, h("i", { className: "brf-task-dot", style: { background: "#ef4444" } }), " Blocked"), h("span", null, "Y: count · X: list")),
      h("div", { className: "brf-task-chart-columns" }, rows.map(function (row) {
        var total = row.done + row.blocked;
        return h("div", { key: row.name, className: "brf-task-chart-column" },
          h("span", { className: "brf-task-chart-count" }, total),
          h("div", { className: "brf-task-chart-stack", title: total + " transitions", style: { height: (total / max * 100) + "%" } },
            row.done ? h("i", { style: { height: (row.done / total * 100) + "%", background: "#22c55e" } }) : null,
            row.blocked ? h("i", { style: { height: (row.blocked / total * 100) + "%", background: "#ef4444" } }) : null),
          h("span", { className: "brf-task-chart-label", title: row.name }, row.name));
      })));
  }
  function TaskListView(props) {
    var sQuery = useState(""); var query = sQuery[0], setQuery = sQuery[1];
    var sSort = useState("created"); var sort = sSort[0], setSort = sSort[1];
    var sGroups = useState({}); var groupsOpen = sGroups[0], setGroupsOpen = sGroups[1];
    var sChildren = useState({}); var childrenOpen = sChildren[0], setChildrenOpen = sChildren[1];
    var all = props.tasks || [], needle = query.trim().toLowerCase();
    var visible = all.filter(function (task) {
      return !needle || [task.title, task.status, task.assignee, task.board].join(" ").toLowerCase().indexOf(needle) >= 0;
    });
    var byId = {}; visible.forEach(function (task) { byId[task.id] = task; });
    var children = {};
    visible.forEach(function (task) { if (task.parent_id && byId[task.parent_id]) (children[task.parent_id] || (children[task.parent_id] = [])).push(task); });
    function sorted(rows) { return rows.slice().sort(function (a, b) {
      if (sort === "title") return String(a.title).localeCompare(String(b.title));
      return (b.created_at || 0) - (a.created_at || 0);
    }); }
    var listOrder = [], listGroups = {};
    visible.forEach(function (task) {
      var list = task.list || "No List";
      if (!listGroups[list]) { listGroups[list] = []; listOrder.push(list); }
      listGroups[list].push(task);
    });
    function row(task, depth) {
      var childRows = sorted(children[task.id] || []), hasChildren = childRows.length > 0;
      var childCount = Array.isArray(task.child_ids) ? task.child_ids.length : childRows.length;
      var isOpen = !!childrenOpen[task.id], target = ticketHref(task.id, props.target);
      return [h("div", { className: "brf-task-row", key: task.id, style: { "--task-depth": depth } },
        h("div", { className: "brf-task-title", "data-label": "Name" },
          hasChildren ? h("button", { className: "brf-task-disclosure", onClick: function () { setChildrenOpen(function (old) { var next = Object.assign({}, old); next[task.id] = !next[task.id]; return next; }); }, "aria-label": (isOpen ? "Collapse" : "Expand") + " child tasks" }, isOpen ? "⌄" : "›") : h("span", { className: "brf-task-disclosure brf-task-disclosure-empty" }, ""),
          h("span", { className: "brf-task-dot", title: priorityLabel(task.priority) + " priority", style: { background: priorityColor(task.priority) } }),
          h("a", { href: target.url, className: "brf-task-link" }, task.title || task.id),
          childCount ? h("span", { className: "brf-task-subtasks", title: childCount + " subtasks" }, "↳ " + childCount) : null,
          h("span", { className: "brf-task-comments", title: (task.comment_count || 0) + " comments" }, "💬 " + (task.comment_count || 0))),
        h("div", { className: "brf-task-status", "data-label": "Status" }, h("span", { className: "brf-task-dot", style: { background: resolveColor(task.status) } }), h("span", null, task.status || "Unknown")),
        h("div", { className: "brf-task-priority", "data-label": "Priority" }, priorityLabel(task.priority)),
        h("div", { className: "brf-task-assignee", "data-label": "Assignee" }, task.assignee || "—"),
        h("div", { className: "brf-task-board", "data-label": "List" }, task.list || "No List"),
        h("div", { className: "brf-task-age", "data-label": "Age" }, task.created_at ? timeAgo(task.created_at) : "—")),
        hasChildren && isOpen ? childRows.map(function (child) { return row(child, depth + 1); }) : []];
    }
    if (props.loading) return h(Skeleton);
    return h("div", { className: "brf-task-view" },
      h("div", { className: "brf-task-controls" },
        h("input", { type: "search", value: query, onChange: function (e) { setQuery(e.target.value); }, placeholder: "Search tasks…", "aria-label": "Search tasks" }),
        h("select", { value: sort, onChange: function (e) { setSort(e.target.value); }, "aria-label": "Sort tasks" },
          h("option", { value: "created" }, "Newest first"), h("option", { value: "title" }, "Title"))),
      h(TaskListChart, { tasks: all, target: props.target }),
      !visible.length ? h("div", { className: "brf-task-empty" }, "No tasks match this report.") : listOrder.map(function (list) {
        var listKey = "list::" + list, listOpen = groupsOpen[listKey] !== false, listTasks = listGroups[list];
        var statusOrder = [], statusGroups = {};
        listTasks.forEach(function (task) {
          var nested = task.parent_id && (children[task.parent_id] || []).some(function (child) { return child.id === task.id; });
          if (nested) return;
          var status = task.status || "unknown";
          if (!statusGroups[status]) { statusGroups[status] = []; statusOrder.push(status); }
          statusGroups[status].push(task);
        });
        return h("section", { className: "brf-task-list", key: list },
          h("button", { className: "brf-task-list-header", onClick: function () { setGroupsOpen(function (old) { var next = Object.assign({}, old); next[listKey] = !listOpen; return next; }); }, "aria-expanded": listOpen },
            h("span", { className: "brf-task-chevron" }, listOpen ? "⌄" : "›"), h("strong", null, "List: " + list), h("span", null, listTasks.length + " tasks")),
          listOpen ? statusOrder.map(function (status) {
            var statusKey = "status::" + list + "::" + status, isOpen = groupsOpen[statusKey] !== false, rows = statusGroups[status];
            return h("section", { className: "brf-task-group", key: statusKey },
              h("button", { className: "brf-task-group-header", onClick: function () { setGroupsOpen(function (old) { var next = Object.assign({}, old); next[statusKey] = !isOpen; return next; }); }, "aria-expanded": isOpen },
                h("span", { className: "brf-task-chevron" }, isOpen ? "⌄" : "›"), h("span", { className: "brf-task-dot", style: { background: resolveColor(status) } }), h("strong", null, status), h("span", null, rows.length)),
              isOpen ? h("div", { className: "brf-task-table" },
                h("div", { className: "brf-task-head" }, ["Name", "Status", "Priority", "Assignee", "List", "Age"].map(function (label) { return h("span", { key: label }, label); })),
                sorted(rows).map(function (task) { return row(task, 0); })) : null);
          }) : null);
      }));
  }

  function DigestView(props) {
    var digest = props.digest, building = props.building;
    var hd = digest.header || {}, cost = digest.cost || {}, sys = digest.system || {};
    return h("div", { key: digest.date, className: "brf-fade-in", style: { flex: 1, paddingLeft: "1rem", overflowY: "auto", maxHeight: "68vh" } },
      h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "1rem", flexWrap: "wrap", marginBottom: "0.85rem", paddingBottom: "0.75rem", borderBottom: "1px solid var(--color-border)" } },
        h("div", { style: { flex: "1 1 16rem", minWidth: 0 } },
          h("div", { style: { display: "flex", alignItems: "baseline", gap: "0.75rem", marginBottom: "0.35rem", flexWrap: "wrap" } },
            h("h3", { style: { margin: 0, fontSize: "1.05rem" } }, digest.date + " \u00b7 " + (hd.status || "")),
            h("span", { style: { fontSize: "0.85rem", color: MUTED } },
              (hd.open ? hd.open + " open" : "nothing open") + " \u00b7 " + eur(hd.cost_eur) + " / $" + (cost.budget_daily || 0).toFixed(0)),
            Button ? h(Button, { size: "sm", variant: "secondary", disabled: building, onClick: props.onRebuild },
              building ? h("span", null, h(Spinner, { style: { marginRight: "0.35rem" } }), "Building\u2026") : "Rebuild") : null,
            digest.generated_at ? h("span", { style: { fontSize: "0.72rem", color: MUTED } }, "built " + timeAgo(digest.generated_at)) : null),
          digest.overview ? h("div", { style: { fontSize: "0.74rem", color: MUTED } },
            digest.date + (digest.overview.phase ? " \u00b7 Phase " + digest.overview.phase : "") + " \u00b7 " + (digest.overview.mode || "day") + " \u00b7 " + (digest.overview.board === "all" ? "All boards" : digest.overview.board)) : null),
        digest.overview ? h(KpiGrid, { overview: digest.overview }) : null),

      digest.overview ? h(Overview, { overview: digest.overview, verification: digest.verification, date: digest.date }) : null,

      (digest.hand && digest.hand.length)
        ? h(Section, { title: "Needs your call" }, digest.hand.map(function (d) { return h(HandItem, { key: d.id, d: d, target: props.target }); }))
        : h(Section, { title: "Needs your call" }, h("div", { style: { fontSize: "0.82rem", color: MUTED } }, "Nothing open.")),

      Separator ? h(Separator, { style: { margin: "0.5rem 0" } }) : null,

      h(Section, { title: "Tasks (" + ((props.tasks || []).length) + ")", defaultCollapsed: true }, h(TaskListView, { tasks: props.tasks, loading: props.tasksLoading, target: props.target })),
      (digest.learned && digest.learned.length)
        ? h(Section, { title: "Insights (" + digest.learned.length + ")" }, h(LearnedCards, { items: digest.learned, target: props.target }))
        : null,
      h(Section, { title: "Cost" },
        h(BudgetBar, { label: "Today", editable: true, field: "daily_eur", used: cost.today_eur, budget: cost.budget_daily, apiBase: props.apiBase, onSaved: props.onBudgetSaved }),
        h(BudgetBar, { label: "This month", editable: true, field: "monthly_eur", used: cost.month_eur, budget: cost.budget_monthly, apiBase: props.apiBase, onSaved: props.onBudgetSaved }),
        h("div", { style: { fontSize: "0.76rem", color: MUTED } }, (cost.runs || 0) + " runs"),
        cost.caveat ? h("div", { style: { fontSize: "0.74rem", color: MUTED, marginTop: "0.2rem" } }, "⚠ " + cost.caveat) : null),
      (digest.models && digest.models.total_runs)
        ? h(Section, { title: "Models · " + (digest.models.by_profile.length) + " profiles", defaultCollapsed: true }, h(ModelsTable, { models: digest.models }))
        : null,
      h(Section, { title: "System", defaultCollapsed: true }, h("div", null,
        (sys.notes && sys.notes.length) ? h("div", { style: { fontSize: "0.8rem", color: "#d14a4a", marginBottom: "0.5rem", fontWeight: 600 } }, sys.notes.join(", ")) : null,
        h(InsightsBlock, { insights: sys.insights, stable: sys.stable }))));
  }

  function RangeView(props) {
    var r = props.roll, title = props.title, period = props.period || "week";
    var st = r.decision_stats || {};
    var periodBudget = period === "month"
      ? (r.budget_monthly || ((r.budget_daily || 0) * (r.num_days || 30)))
      : ((r.budget_daily || 0) * (r.num_days || 7));
    return h("div", { className: "brf-fade-in", style: { flex: 1, padding: "0 0.5rem", overflowY: "auto", maxHeight: "68vh" } },
      h("h3", { style: { margin: "0 0 0.1rem", fontSize: "1.05rem" } }, title),
      h("div", { style: { fontSize: "0.8rem", color: MUTED, marginBottom: "0.8rem" } }, r.from + " \u2013 " + r.to),
      h(BudgetBar, { label: period === "month" ? "Month budget" : "Week budget", used: r.cost_eur, budget: periodBudget }),
      h("div", { style: { display: "flex", gap: "1.5rem", flexWrap: "wrap", marginBottom: "0.4rem", marginTop: "0.4rem" } },
        stat("Done", (r.done ? r.done.length : 0) + " tasks"),
        stat("Still open", (r.hand ? r.hand.length : 0) + ""),
        stat("Decisions", (st.total || 0) + " \u00b7 " + (st.vetoed || 0) + " vetoed \u00b7 " + (st.open || 0) + " open")),
      (r.days && r.days.length) ? h("div", { style: { display: "flex", gap: "1.5rem", flexWrap: "wrap" } },
        h("div", { style: { flex: "1 1 220px" } }, h(Section, { title: "Done per day" }, h(MiniBars, { rows: r.days, field: "done" }))),
        h("div", { style: { flex: "1 1 220px" } }, h(Section, { title: "Daily cost" }, h(MiniBars, { rows: r.days, field: "cost" })))) : null,
      (r.hand && r.hand.length) ? h(Section, { title: "Still open (" + r.hand.length + ")" },
        h("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: "0.5rem" } },
          r.hand.map(function (d, i) {
            var t = ticketHref(d.task_id, props.target);
            return h("div", { key: i, className: "brf-card", style: { border: "1px solid var(--color-border)", borderRadius: "0.55rem", padding: "0.55rem 0.7rem", background: "var(--color-card)" } },
              h("div", { style: { fontSize: "0.85rem", fontWeight: 600, lineHeight: 1.35 } }, d.title),
              d.detail ? h("div", { style: { fontSize: "0.76rem", color: MUTED, lineHeight: 1.5, marginTop: "0.25rem", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" } }, d.detail) : null,
              h("a", { href: t.url, style: { display: "inline-block", marginTop: "0.4rem", fontSize: "0.76rem", fontWeight: 600, textDecoration: "none", color: "inherit", border: "1px solid var(--color-border)", borderRadius: "0.4rem", padding: "0.12rem 0.5rem" } }, "Open ticket \u2192")); }))) : null,
      h(Section, { title: "Task transitions (" + ((props.tasks || []).length) + ")" }, h(TaskListView, { tasks: props.tasks, loading: props.tasksLoading, target: props.target })),
      (r.models && r.models.total_runs) ? h(Section, { title: "Models \u00b7 " + (r.models.by_profile.length) + " profiles" }, h(ModelsTable, { models: r.models })) : null,
      (r.system && r.system.insights && r.system.insights.available) ? h(Section, { title: "System" }, h(InsightsBlock, { insights: r.system.insights, stable: true })) : null,
      (r.learned && r.learned.length) ? h(Section, { title: "Insights (" + r.learned.length + ")" }, h(LearnedCards, { items: r.learned, target: props.target })) : null);
    function stat(label, val) {
      return h("div", null,
        h("div", { style: { fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.06em", color: MUTED } }, label),
        h("div", { style: { fontSize: "0.95rem", fontWeight: 600 } }, val));
    }
  }

  function StatusBar(props) {
    var st = props.status; if (!st) return null;
    var b = st.build || {};
    if (b.running) {
      var total = b.total || 0, done = b.done || 0, pct = total ? Math.max(6, Math.round((done / total) * 100)) : 0;
      return h("div", { className: "brf-fade-in", style: { display: "flex", alignItems: "center", gap: "0.6rem", fontSize: "0.8rem", padding: "0.45rem 0.65rem", borderRadius: "0.45rem", marginBottom: "0.6rem", border: "1px solid var(--color-border)", background: "var(--color-card)" } },
        h(Spinner), h("span", { style: { whiteSpace: "nowrap" } }, "Building in background\u2026"),
        h("span", { style: { color: MUTED } }, b.current || ""),
        h("div", { style: { flex: 1, minWidth: "60px" } }, total
          ? h("div", { className: "brf-progress" }, h("div", { className: "brf-progress-fill", style: { width: pct + "%" } }))
          : h("div", { className: "brf-progress brf-progress-indeterminate" })),
        total ? h("span", { style: { color: MUTED } }, done + "/" + total) : null);
    }
    var bits = [];
    var nb = nextBuildLabel(st.next_run);
    if (nb) bits.push("Next build: " + nb);
    if (b.finished_at) bits.push("last built " + timeAgo(b.finished_at));
    if (b.error) bits.push("\u26a0 " + b.error);
    if (!bits.length) return null;
    return h("div", { className: "brf-fade-in", style: { fontSize: "0.76rem", color: MUTED, marginBottom: "0.6rem" } }, bits.join(" \u00b7 "));
  }

  function BriefingPage() {
    var sTab = useState("day"); var tab = sTab[0], setTab = sTab[1];
    var sTz = useState("Europe/Berlin"); var tz = sTz[0], setTz = sTz[1];
    var sStatus = useState(null); var status = sStatus[0], setStatus = sStatus[1];
    var sList = useState({}); var built = sList[0], setBuilt = sList[1];   // date -> {open,cost}
    var sDate = useState(null); var date = sDate[0], setDate = sDate[1];
    var sDig = useState(null); var digest = sDig[0], setDigest = sDig[1];
    var sDL = useState(false); var dayLoading = sDL[0], setDayLoading = sDL[1];
    var sRoll = useState(null); var roll = sRoll[0], setRoll = sRoll[1];
    var sRL = useState(false); var rangeLoading = sRL[0], setRangeLoading = sRL[1];
    var sTasks = useState([]); var tasks = sTasks[0], setTasks = sTasks[1];
    var sTL = useState(false); var tasksLoading = sTL[0], setTasksLoading = sTL[1];
    var sBusy = useState(""); var busyId = sBusy[0], setBusyId = sBusy[1];
    var sApi = useState(null); var apiBase = sApi[0], setApiBase = sApi[1];
    var apiRef = useRef(null); apiRef.current = apiBase;
    var sBoards = useState(["all"]); var boards = sBoards[0], setBoards = sBoards[1];
    var sBoard = useState("all"); var board = sBoard[0], setBoard = sBoard[1];
    var boardRef = useRef("all"); boardRef.current = board;
    var sTB = useState({ path: "/kanban", kind: "kanban" }); var ticketBase = sTB[0], setTicketBase = sTB[1];
    var sHist = useState(null); var historyFirst = sHist[0], setHistoryFirst = sHist[1];
    var sCV = useState(0); var colorsV = sCV[0], setColorsV = sCV[1];
    var sMonth = useState(null); var monthCursor = sMonth[0], setMonthCursor = sMonth[1];
    var monthRef = useRef(null); monthRef.current = monthCursor;
    var sWeek = useState(null); var weekCursor = sWeek[0], setWeekCursor = sWeek[1];
    var weekRef = useRef(null); weekRef.current = weekCursor;
    var tzRef = useRef(tz); tzRef.current = tz;
    var dateRef = useRef(null); dateRef.current = date;
    var building = !!(status && status.build && status.build.running);

    var loadList = useCallback(function () {
      return fetchJSON(addBoard(apiRef.current + "/digests?limit=60", boardRef.current)).then(function (r) {
        var m = {}; (r && r.digests || []).forEach(function (d) { m[d.date] = d; });
        setBuilt(m); return m;
      }).catch(function () { return {}; });
    }, []);

    var loadTasks = useCallback(function (from, to, b) {
      setTasksLoading(true);
      var query = "/tasks?from_=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to);
      return fetchJSON(addBoard(apiRef.current + query, b == null ? boardRef.current : b)).then(function (r) {
        setTasks((r && r.tasks) || []);
      }).catch(function () { setTasks([]); }).then(function () { setTasksLoading(false); });
    }, []);

    var loadDay = useCallback(function (d) {
      setDayLoading(true);
      return fetchJSON(addBoard(apiRef.current + "/digest/" + d, boardRef.current)).then(function (r) {
        setDigest(r);
        if (r && r.date) loadTasks(r.date, r.date);
        return r;
      }).catch(function () { setDigest(null); setTasks([]); }).then(function (r) { setDayLoading(false); return r; });
    }, []);

    var loadHistoryAndEnsure = useCallback(function (b) {
      return fetchJSON(addBoard(apiRef.current + "/history", b)).then(function (hr) {
        var first = hr && hr.first_date ? hr.first_date : null;
        setHistoryFirst(first);
        var span = 14;
        if (first) {
          var d0 = new Date(first + "T00:00:00"), now = new Date();
          span = Math.min(370, Math.max(7, Math.round((now - d0) / 86400000) + 1));
        }
        return fetchJSON(addBoard(apiRef.current + "/ensure?days=" + span, b))
          .then(function () { loadList(); }).catch(function () {});
      }).catch(function () {
        return fetchJSON(addBoard(apiRef.current + "/ensure?days=14", b))
          .then(function () { loadList(); }).catch(function () {});
      });
    }, []);

    function loadColors(b) {
      fetchJSON(addBoard(apiRef.current + "/colors", b)).then(function (r) {
        if (r && r.status_colors) { KANBAN_COLORS = r.status_colors; setColorsV(function (v) { return v + 1; }); }
      }).catch(function () {});
    }

    function init() {
      fetchJSON(apiRef.current + "/status").then(function (st) {
        setStatus(st); if (st && st.timezone) setTz(st.timezone);
      }).catch(function () {});
      loadColors(boardRef.current);
      loadKanbanCss(function () { setColorsV(function (v) { return v + 1; }); });
      loadDay("today").then(function (r) { if (r && r.date) setDate(r.date); });
      loadList();
      loadHistoryAndEnsure(boardRef.current);
    }

    // resolve which /api/plugins/<dir> base actually answers, THEN load
    useEffect(function () {
      resolveApi().then(function (base) {
        apiRef.current = base; setApiBase(base);
        fetchJSON(base + "/boards").then(function (r) { if (r && r.boards && r.boards.length) setBoards(r.boards); }).catch(function () {});
        detectTicketTarget(fetchJSON).then(setTicketBase);
        init();
      });
    }, []);

    // poll status for build progress + last built
    useEffect(function () {
      var iv = setInterval(function () {
        if (!apiRef.current) return;
        fetchJSON(apiRef.current + "/status").then(function (st) {
          setStatus(st);
          if (st && st.build && st.build.running) loadList();
        }).catch(function () {});
      }, 2500);
      return function () { clearInterval(iv); };
    }, []);

    useEffect(function () { if (tab === "day" && date) loadDay(date); }, [date]);

    function addDaysStr(s, n) {
      var d = new Date(s + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n);
      return d.toISOString().slice(0, 10);
    }

    var loadRange = useCallback(function (kind) {
      setRangeLoading(true); setRoll(null);
      var t = tzRef.current;
      var to = ymd(new Date(), t), from;
      if (kind === "month") {
        var mc = monthRef.current || (to.slice(0, 8) + "01");
        from = mc;
        var y = +mc.slice(0, 4), m = +mc.slice(5, 7);
        var lastDay = new Date(y, m, 0).getDate();
        var monthEnd = mc.slice(0, 8) + ("0" + lastDay).slice(-2);
        to = monthEnd > to ? to : monthEnd;   // cap current month at today
      } else {
        var wc = weekRef.current || ymd(new Date(), t);
        var today0 = ymd(new Date(), t);
        if (wc > today0) wc = today0;
        to = wc;
        from = addDaysStr(wc, -6);
      }
      return fetchJSON(addBoard(apiRef.current + "/range?from_=" + from + "&to=" + to, boardRef.current))
        .then(function (r) { setRoll(r); return loadTasks(from, to); }).catch(function () { setRoll(null); setTasks([]); })
        .then(function () { setRangeLoading(false); });
    }, []);

    function shiftMonth(delta) {
      var base = monthRef.current || (ymd(new Date(), tzRef.current).slice(0, 8) + "01");
      var y = +base.slice(0, 4), m = +base.slice(5, 7) + delta;
      while (m < 1) { m += 12; y--; }
      while (m > 12) { m -= 12; y++; }
      var nc = y + "-" + ("0" + m).slice(-2) + "-01";
      setMonthCursor(nc); monthRef.current = nc;
      loadRange("month");
    }

    function shiftWeek(delta) {
      var today0 = ymd(new Date(), tzRef.current);
      var base = weekRef.current || today0;
      var nc = addDaysStr(base, delta * 7);
      if (nc > today0) nc = today0;
      setWeekCursor(nc); weekRef.current = nc;
      loadRange("week");
    }

    function changeBoard(b) {
      setBoard(b); boardRef.current = b;
      setDigest(null); setRoll(null); setBuilt({}); setHistoryFirst(null);
      loadColors(b);
      loadHistoryAndEnsure(b);
      if (tab === "day") {
        loadDay(date || "today").then(function (r) { if (r && r.date) setDate(r.date); });
        loadList();
      } else {
        loadRange(tab);
      }
    }

    function switchTab(t) {
      setTab(t);
      if (t === "week") {
        if (!weekRef.current) { var cw = ymd(new Date(), tzRef.current); setWeekCursor(cw); weekRef.current = cw; }
        loadRange("week");
      } else if (t === "month") {
        if (!monthRef.current) {
          var cm = ymd(new Date(), tzRef.current).slice(0, 8) + "01";
          setMonthCursor(cm); monthRef.current = cm;
        }
        loadRange("month");
      } else if (t === "day" && !digest && date) loadDay(date);
    }

    function rebuild() {
      if (!date) return;
      setDayLoading(true);
      fetchJSON(addBoard(apiRef.current + "/digest/" + date + "?rebuild=true", boardRef.current)).then(function (r) { setDigest(r); loadList(); })
        .catch(function () {}).then(function () { setDayLoading(false); });
    }

    function budgetSaved() {
      rebuild();
    }

    function resolve(id, resolution) {
      setBusyId(id);
      fetchJSON(apiRef.current + "/decisions/" + id + "/resolve?resolution=" + resolution)
        .then(function () { return fetchJSON(addBoard(apiRef.current + "/digest/" + date + "?rebuild=true", boardRef.current)); })
        .then(function (r) { setDigest(r); }).catch(function () {}).then(function () { setBusyId(""); });
    }

    // ---- day sidebar: spans the full history (client-computed) ----
    var spanDays = 14;
    if (historyFirst) {
      var hd0 = new Date(historyFirst + "T00:00:00"), nowd = new Date();
      spanDays = Math.min(200, Math.max(14, Math.round((nowd - hd0) / 86400000) + 1));
    }
    var dayList = [];
    for (var i = 0; i < spanDays; i++) dayList.push(ymd(daysAgo(i), tz));

    var sidebar = h("div", { className: "brf-day-sidebar" },
      dayList.map(function (d, idx) {
        var active = d === date, b = built[d];
        var hasDone = b && (b.done || 0) > 0, hasOpen = b && (b.open || 0) > 0;
        return h("div", { key: d, onClick: function () { setDate(d); }, className: "brf-card",
          style: { cursor: "pointer", padding: "0.35rem 0.5rem", borderRadius: "0.4rem", marginBottom: "0.2rem", fontSize: "0.8rem",
            background: active ? "var(--color-accent)" : "transparent", color: active ? "var(--color-accent-foreground)" : "inherit" } },
          h("div", { style: { fontWeight: 600 } }, idx === 0 ? "Today" : idx === 1 ? "Yesterday" : d.slice(5)),
          h("div", { style: { fontSize: "0.68rem", color: active ? "inherit" : MUTED } },
            b ? ((b.done || 0) + " done" + (hasOpen ? " \u00b7 " + b.open + " open" : "")) : (building ? "building\u2026" : "\u2014")));
      }));

    var dayBody;
    if (dayLoading && !digest) dayBody = h("div", { className: "brf-day-body" }, h(Skeleton));
    else if (!digest) dayBody = h("div", { className: "brf-day-body brf-day-empty" }, "No briefing.");
    else dayBody = h("div", { className: "brf-day-body" },
      h(DigestView, { digest: digest, building: dayLoading || building, onRebuild: rebuild, onBudgetSaved: budgetSaved, apiBase: apiBase, target: ticketBase, tasks: tasks, tasksLoading: tasksLoading }));

    var content;
    if (tab === "day") {
      content = h("div", { className: "brf-day-layout" }, sidebar, dayBody);
    } else {
      var rangeBody = (rangeLoading || !roll) ? h(Skeleton)
        : h(RangeView, { roll: roll, title: tab === "month" ? "Month" : "Week", target: ticketBase, period: tab, tasks: tasks, tasksLoading: tasksLoading });
      var nav = null;
      if (tab === "month") {
        var curMonth = ymd(new Date(), tz).slice(0, 7);
        var shown = (monthCursor || (curMonth + "-01")).slice(0, 7);
        var atCurrent = shown >= curMonth;
        nav = h("div", { style: { display: "flex", alignItems: "center", gap: "0.6rem", marginBottom: "0.5rem" } },
          Button ? h(Button, { size: "sm", variant: "secondary", onClick: function () { shiftMonth(-1); } }, "\u2190 Prev") : null,
          h("strong", { style: { fontSize: "0.95rem", minWidth: "5.5rem", textAlign: "center" } }, shown),
          Button ? h(Button, { size: "sm", variant: "secondary", disabled: atCurrent, onClick: function () { if (!atCurrent) shiftMonth(1); } }, "Next \u2192") : null);
      } else if (tab === "week") {
        var today0 = ymd(new Date(), tz);
        var wc = weekCursor || today0;
        var wfrom = addDaysStr(wc, -6);
        var atNow = wc >= today0;
        nav = h("div", { style: { display: "flex", alignItems: "center", gap: "0.6rem", marginBottom: "0.5rem" } },
          Button ? h(Button, { size: "sm", variant: "secondary", onClick: function () { shiftWeek(-1); } }, "\u2190 Prev") : null,
          h("strong", { style: { fontSize: "0.9rem", minWidth: "9rem", textAlign: "center" } }, wfrom.slice(5) + " \u2013 " + wc.slice(5)),
          Button ? h(Button, { size: "sm", variant: "secondary", disabled: atNow, onClick: function () { if (!atNow) shiftWeek(1); } }, "Next \u2192") : null);
      }
      content = h("div", null, nav, rangeBody);
    }

    if (!apiBase) {
      return h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Briefing")),
        h(CardContent, null, h("div", { style: { display: "flex", alignItems: "center", gap: "0.5rem", color: MUTED, fontSize: "0.85rem" } }, h(Spinner), "Connecting\u2026")));
    }

    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Briefing")),
      h(CardContent, null,
        h("div", { style: { display: "flex", gap: "0.4rem", marginBottom: "0.7rem" } },
          h(TabButton, { label: "Day", active: tab === "day", onClick: function () { switchTab("day"); } }),
          h(TabButton, { label: "Week", active: tab === "week", onClick: function () { switchTab("week"); } }),
          h(TabButton, { label: "Month", active: tab === "month", onClick: function () { switchTab("month"); } }),
          boards.length > 1 ? h("select", {
            value: board, onChange: function (e) { changeBoard(e.target.value); },
            title: "Board",
            style: { marginLeft: "auto", border: "1px solid var(--color-border)", borderRadius: "0.4rem",
                     background: "var(--color-card)", color: "inherit", fontSize: "0.8rem", padding: "0.2rem 0.4rem" }
          }, boards.map(function (b) { return h("option", { key: b, value: b }, b === "all" ? "All boards" : b); })) : null),
        h(StatusBar, { status: status }),
        content));
  }

  window.__HERMES_PLUGINS__.register("briefing", BriefingPage);
})();
