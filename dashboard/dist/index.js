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
  // Palette is only a FALLBACK. Real colors are auto-discovered from the kanban
  // DB via /colors and stored in KANBAN_COLORS (canon status name -> color).
  var STATUS_COLORS = {
    approval: "#eab308", blocked: "#f97316", failed: "#ef4444", instability: "#ef4444",
    violation: "#a855f7", done: "#22c55e", active: "#3b82f6", running: "#3b82f6",
    todo: "#9ca3af", ready: "#9ca3af"
  };
  var KANBAN_COLORS = {};
  var KIND_TO_STATUS = {
    blocked: ["blocked"],
    approval: ["blocked", "ready", "approval", "review"],
    failed: ["failed", "error", "gave_up", "blocked"],
    instability: ["failed", "blocked"],
    violation: ["blocked", "failed"],
    done: ["done", "completed", "complete", "archived"],
    active: ["in_progress", "running", "claimed", "active", "doing"],
    todo: ["todo", "ready", "triage", "scheduled", "backlog", "new"]
  };
  function canonStatus(s) { return String(s || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, ""); }
  function kanbanColorFor(kind) {
    var k = (kind || "").toLowerCase();
    var cands = KIND_TO_STATUS[k] || [k];
    for (var i = 0; i < cands.length; i++) { if (KANBAN_COLORS[cands[i]]) return KANBAN_COLORS[cands[i]]; }
    return null;
  }
  // Prefer the task's REAL kanban status color; fall back to a kind mapping,
  // then to the built-in palette. Works for any custom status set.
  function resolveColor(status, kind) {
    if (status) { var c = KANBAN_COLORS[canonStatus(status)]; if (c) return c; }
    var kc = kanbanColorFor(kind || status); if (kc) return kc;
    if (status && STATUS_COLORS[canonStatus(status)]) return STATUS_COLORS[canonStatus(status)];
    return STATUS_COLORS[(kind || "").toLowerCase()] || "#9ca3af";
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
        h("span", { style: { fontSize: "0.78rem" } },
          eur(used) + " / $" + budget.toFixed(0),
          h("span", { style: { marginLeft: "0.45rem", fontWeight: 700, color: col } }, pct + "%"))),
      h("div", { style: { height: "9px", borderRadius: "999px", background: "var(--color-muted, rgba(127,127,127,0.18))", overflow: "hidden" } },
        h("div", { style: { height: "100%", width: pct + "%", borderRadius: "999px",
          background: "linear-gradient(90deg," + col + "," + col2 + ")",
          boxShadow: "0 0 8px " + col + "66",
          transition: "width .6s cubic-bezier(.4,0,.2,1)" } })));
  }

  // Readable Done view: cards in a responsive grid, grouped by board when more
  // than one board is present. Long "why" text is clamped to keep cards even.
  function DoneGrid(props) {
    var items = props.items || [];
    var groups = {}, order = [];
    items.forEach(function (it) { var b = boardOf(it.task_id) || "\u2014"; if (!groups[b]) { groups[b] = []; order.push(b); } groups[b].push(it); });
    var multi = order.length > 1;
    return h("div", null, order.map(function (b) {
      return h("div", { key: b, style: { marginBottom: "0.9rem" } },
        multi ? h("div", { style: { fontSize: "0.7rem", textTransform: "uppercase", letterSpacing: "0.06em", color: MUTED, margin: "0.1rem 0 0.45rem", fontWeight: 600 } }, b + " \u00b7 " + groups[b].length) : null,
        h("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: "0.55rem" } },
          groups[b].map(function (it, i) {
            var why = (it.bullets && it.bullets[0]) || it.why || "";
            var t = ticketHref(it.task_id, props.target);
            return h("div", { key: i, className: "brf-card", style: { display: "flex", flexDirection: "column", border: "1px solid var(--color-border)", borderLeft: "3px solid " + resolveColor(it.status, "done"), borderRadius: "0.55rem", padding: "0.6rem 0.75rem", background: "var(--color-card)" } },
              h("div", { style: { fontSize: "0.86rem", fontWeight: 600, lineHeight: 1.35, marginBottom: why ? "0.35rem" : "0.4rem" } }, it.title),
              why ? h("div", { style: { fontSize: "0.78rem", color: MUTED, lineHeight: 1.5, marginBottom: "0.5rem", display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" } }, why) : null,
              it.task_id ? h("a", { href: t.url, style: { marginTop: "auto", alignSelf: "flex-start", fontSize: "0.74rem", fontWeight: 600, textDecoration: "none", color: "inherit", border: "1px solid var(--color-border)", borderRadius: "0.4rem", padding: "0.1rem 0.5rem" } }, "Open ticket \u2192") : null);
          })));
    }));
  }

  function DigestView(props) {
    var digest = props.digest, building = props.building;
    var hd = digest.header || {}, cost = digest.cost || {}, sys = digest.system || {};
    return h("div", { key: digest.date, className: "brf-fade-in", style: { flex: 1, paddingLeft: "1rem", overflowY: "auto", maxHeight: "68vh" } },
      h("div", { style: { display: "flex", alignItems: "baseline", gap: "0.75rem", marginBottom: "0.35rem", flexWrap: "wrap" } },
        h("h3", { style: { margin: 0, fontSize: "1.05rem" } }, digest.date + " \u00b7 " + (hd.status || "")),
        h("span", { style: { fontSize: "0.85rem", color: MUTED } },
          (hd.open ? hd.open + " open" : "nothing open") + " \u00b7 " + eur(hd.cost_eur) + " / $" + (cost.budget_daily || 0).toFixed(0)),
        Button ? h(Button, { size: "sm", variant: "secondary", disabled: building, onClick: props.onRebuild },
          building ? h("span", null, h(Spinner, { style: { marginRight: "0.35rem" } }), "Building\u2026") : "Rebuild") : null,
        digest.generated_at ? h("span", { style: { fontSize: "0.72rem", color: MUTED } }, "built " + timeAgo(digest.generated_at)) : null),

      (digest.hand && digest.hand.length)
        ? h(Section, { title: "Needs your call" }, digest.hand.map(function (d) { return h(HandItem, { key: d.id, d: d, target: props.target }); }))
        : h(Section, { title: "Needs your call" }, h("div", { style: { fontSize: "0.82rem", color: MUTED } }, "Nothing open.")),

      Separator ? h(Separator, { style: { margin: "0.5rem 0" } }) : null,

      (digest.done && digest.done.length)
        ? h(Section, { title: "Done (" + digest.done.length + ")" }, h(DoneGrid, { items: digest.done, target: props.target }))
        : null,
      (digest.in_progress && digest.in_progress.length)
        ? h(Section, { title: "Active (" + digest.in_progress.length + ")" },
            h("div", { style: { display: "flex", flexWrap: "wrap", gap: "0.35rem" } },
              digest.in_progress.map(function (t, i) {
                var th = ticketHref(t.task_id, props.target);
                var ach = colorChrome(resolveColor(t.status, "active"));
                return h("a", { key: i, href: th.url, style: Object.assign({ fontSize: "0.78rem", textDecoration: "none", padding: "0.15rem 0.5rem", borderRadius: "999px" }, ach) }, t.title); })))
        : null,
      (digest.learned && digest.learned.length)
        ? h(Section, { title: "Insights (" + digest.learned.length + ")" }, h(LearnedCards, { items: digest.learned, target: props.target }))
        : null,
      h(Section, { title: "Cost" },
        h(BudgetBar, { label: "Today", used: cost.today_eur, budget: cost.budget_daily }),
        h(BudgetBar, { label: "This month", used: cost.month_eur, budget: cost.budget_monthly }),
        h("div", { style: { fontSize: "0.76rem", color: MUTED } }, (cost.runs || 0) + " runs"),
        cost.caveat ? h("div", { style: { fontSize: "0.74rem", color: MUTED, marginTop: "0.2rem" } }, "\u26a0 " + cost.caveat) : null),
      h(Section, { title: "System" }, h("div", { style: { fontSize: "0.84rem" } }, sys.stable ? "stable" : ((sys.notes || []).join(", ") || "\u2014"))));
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
      (r.done && r.done.length) ? h(Section, { title: "Done (" + r.done.length + ")" }, h(DoneGrid, { items: r.done, target: props.target }))
        : h(Section, { title: "Done" }, h("div", { style: { fontSize: "0.82rem", color: MUTED } }, "Nothing recorded in this range.")),
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

    var loadDay = useCallback(function (d) {
      setDayLoading(true);
      return fetchJSON(addBoard(apiRef.current + "/digest/" + d, boardRef.current)).then(function (r) { setDigest(r); return r; })
        .catch(function () { setDigest(null); }).then(function (r) { setDayLoading(false); return r; });
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
        .then(function (r) { setRoll(r); }).catch(function () { setRoll(null); })
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

    var sidebar = h("div", { style: { width: "150px", flex: "0 0 150px", borderRight: "1px solid var(--color-border)", paddingRight: "0.6rem", overflowY: "auto", maxHeight: "68vh" } },
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
    if (dayLoading && !digest) dayBody = h(Skeleton);
    else if (!digest) dayBody = h("div", { style: { paddingLeft: "1rem", color: MUTED, fontSize: "0.85rem" } }, "No briefing.");
    else dayBody = h(DigestView, { digest: digest, building: dayLoading || building, onRebuild: rebuild, target: ticketBase });

    var content;
    if (tab === "day") {
      content = h("div", { style: { display: "flex", gap: "0.5rem" } }, sidebar, dayBody);
    } else {
      var rangeBody = (rangeLoading || !roll) ? h(Skeleton)
        : h(RangeView, { roll: roll, title: tab === "month" ? "Month" : "Week", target: ticketBase, period: tab });
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
