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
  var MUTED = "var(--color-muted-foreground)";

  function eur(n) { return "\u2248 " + (Number(n) || 0).toFixed(2) + " \u20ac"; }
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
    return h("div", { className: "brf-fade-in", style: { marginBottom: "1rem" } },
      h("div", { style: { fontSize: "0.7rem", letterSpacing: "0.08em", textTransform: "uppercase", color: MUTED, marginBottom: "0.4rem" } }, props.title),
      props.children);
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

  function HandItem(props) {
    var d = props.d, onResolve = props.onResolve, busy = props.busy;
    return h("div", { className: "brf-card brf-fade-in", style: {
        border: "1px solid var(--color-border)", borderRadius: "var(--radius, 0.5rem)",
        padding: "0.6rem 0.75rem", marginBottom: "0.5rem", background: "var(--color-card)" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.25rem" } },
        Badge ? h(Badge, { variant: KIND_TONE[d.kind] || "default" }, KIND_LABEL[d.kind] || d.kind) : null,
        h("strong", { style: { fontSize: "0.9rem" } }, d.title)),
      d.detail ? h("div", { style: { fontSize: "0.8rem", color: MUTED, marginBottom: "0.4rem" } }, d.detail) : null,
      h("div", { style: { display: "flex", gap: "0.4rem", alignItems: "center" } },
        Button ? [
          h(Button, { key: "ok", size: "sm", disabled: busy, onClick: function () { onResolve(d.id, "ok"); } }, "Give OK"),
          h(Button, { key: "veto", size: "sm", variant: "destructive", disabled: busy, onClick: function () { onResolve(d.id, "veto"); } }, "Veto")
        ] : null,
        busy ? h(Spinner) : null));
  }

  function MiniBars(props) {
    var rows = props.rows || [], field = props.field || "cost";
    var max = Math.max.apply(null, rows.map(function (r) { return r[field] || 0; }).concat([0.0001]));
    return h("div", { style: { display: "flex", alignItems: "flex-end", gap: "4px", height: "60px", marginTop: "0.4rem" } },
      rows.map(function (r, i) {
        var pct = Math.round(((r[field] || 0) / max) * 100);
        return h("div", { key: r.date, title: r.date + ": " + (r[field] || 0), style: { flex: 1, display: "flex", flexDirection: "column", justifyContent: "flex-end", alignItems: "center" } },
          h("div", { className: "brf-progress-fill", style: { width: "100%", height: Math.max(3, pct) + "%", borderRadius: "3px", transition: "height .5s ease", transitionDelay: (i * 30) + "ms" } }),
          h("div", { style: { fontSize: "0.6rem", color: MUTED, marginTop: "2px" } }, r.date.slice(8)));
      }));
  }

  function DigestView(props) {
    var digest = props.digest, building = props.building;
    var hd = digest.header || {}, cost = digest.cost || {}, sys = digest.system || {};
    return h("div", { key: digest.date, className: "brf-fade-in", style: { flex: 1, paddingLeft: "1rem", overflowY: "auto", maxHeight: "68vh" } },
      h("div", { style: { display: "flex", alignItems: "baseline", gap: "0.75rem", marginBottom: "0.35rem", flexWrap: "wrap" } },
        h("h3", { style: { margin: 0, fontSize: "1.05rem" } }, digest.date + " \u00b7 " + (hd.status || "")),
        h("span", { style: { fontSize: "0.85rem", color: MUTED } },
          (hd.open ? hd.open + " open" : "nothing open") + " \u00b7 " + eur(hd.cost_eur) + " / " + (cost.budget_daily || 0).toFixed(0) + " \u20ac"),
        Button ? h(Button, { size: "sm", variant: "secondary", disabled: building, onClick: props.onRebuild },
          building ? h("span", null, h(Spinner, { style: { marginRight: "0.35rem" } }), "Building\u2026") : "Rebuild") : null,
        digest.generated_at ? h("span", { style: { fontSize: "0.72rem", color: MUTED } }, "built " + timeAgo(digest.generated_at)) : null),

      (digest.hand && digest.hand.length)
        ? h(Section, { title: "Needs your call" }, digest.hand.map(function (d) { return h(HandItem, { key: d.id, d: d, onResolve: props.onResolve, busy: props.busyId === d.id }); }))
        : h(Section, { title: "Needs your call" }, h("div", { style: { fontSize: "0.82rem", color: MUTED } }, "Nothing open.")),

      Separator ? h(Separator, { style: { margin: "0.5rem 0" } }) : null,

      (digest.done && digest.done.length)
        ? h(Section, { title: "Done today" }, digest.done.map(function (it, i) {
            var b = (it.bullets && it.bullets[0]) || it.why || "done";
            return h("div", { key: i, style: { fontSize: "0.84rem", marginBottom: "0.25rem" } }, h("strong", null, it.title), " \u2014 ", b); }))
        : null,
      (digest.in_progress && digest.in_progress.length)
        ? h(Section, { title: "Active" }, h("div", { style: { fontSize: "0.84rem" } }, digest.in_progress.map(function (t) { return t.title; }).join(", ")))
        : null,
      (digest.learned && digest.learned.length)
        ? h(Section, { title: "Noted" }, digest.learned.map(function (l, i) { return h("div", { key: i, style: { fontSize: "0.82rem", marginBottom: "0.2rem" } }, "\u2022 " + l); }))
        : null,
      h(Section, { title: "Cost" },
        h("div", { style: { fontSize: "0.84rem" } },
          "Today " + eur(cost.today_eur) + " / " + (cost.budget_daily || 0).toFixed(0) + " \u20ac \u00b7 Month " + eur(cost.month_eur) + " / " + (cost.budget_monthly || 0).toFixed(0) + " \u20ac \u00b7 " + (cost.runs || 0) + " runs"),
        cost.caveat ? h("div", { style: { fontSize: "0.74rem", color: MUTED, marginTop: "0.2rem" } }, "\u26a0 " + cost.caveat) : null),
      h(Section, { title: "System" }, h("div", { style: { fontSize: "0.84rem" } }, sys.stable ? "stable" : ((sys.notes || []).join(", ") || "\u2014"))));
  }

  function RangeView(props) {
    var r = props.roll, title = props.title;
    var st = r.decision_stats || {};
    return h("div", { className: "brf-fade-in", style: { flex: 1, padding: "0 0.5rem", overflowY: "auto", maxHeight: "68vh" } },
      h("h3", { style: { margin: "0 0 0.1rem", fontSize: "1.05rem" } }, title),
      h("div", { style: { fontSize: "0.8rem", color: MUTED, marginBottom: "0.8rem" } }, r.from + " \u2013 " + r.to),
      h("div", { style: { display: "flex", gap: "1.5rem", flexWrap: "wrap", marginBottom: "0.4rem" } },
        stat("Cost", eur(r.cost_eur)),
        stat("Done", (r.done ? r.done.length : 0) + " tasks"),
        stat("Decisions", (st.total || 0) + " \u00b7 " + (st.vetoed || 0) + " vetoed \u00b7 " + (st.open || 0) + " open")),
      (r.days && r.days.length) ? h(Section, { title: "Daily cost" }, h(MiniBars, { rows: r.days, field: "cost" })) : null,
      (r.hand && r.hand.length) ? h(Section, { title: "Still open" }, r.hand.map(function (d, i) {
          return h("div", { key: i, style: { fontSize: "0.84rem", marginBottom: "0.2rem" } }, "\u2022 " + d.title); })) : null,
      (r.done && r.done.length) ? h(Section, { title: "Done" }, r.done.slice(0, 25).map(function (it, i) {
          var b = (it.bullets && it.bullets[0]) || it.why || "done";
          return h("div", { key: i, style: { fontSize: "0.84rem", marginBottom: "0.2rem" } }, h("strong", null, it.title), " \u2014 ", b); })) : null,
      (r.learned && r.learned.length) ? h(Section, { title: "Learned" }, r.learned.map(function (l, i) {
          return h("div", { key: i, style: { fontSize: "0.82rem", marginBottom: "0.2rem" } }, "\u2022 " + l); })) : null);
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

    function init() {
      fetchJSON(apiRef.current + "/status").then(function (st) {
        setStatus(st); if (st && st.timezone) setTz(st.timezone);
      }).catch(function () {});
      loadDay("today").then(function (r) { if (r && r.date) setDate(r.date); });
      loadList();
      fetchJSON(addBoard(apiRef.current + "/ensure?days=7", boardRef.current)).then(function () { loadList(); }).catch(function () {});
    }

    // resolve which /api/plugins/<dir> base actually answers, THEN load
    useEffect(function () {
      resolveApi().then(function (base) {
        apiRef.current = base; setApiBase(base);
        fetchJSON(base + "/boards").then(function (r) { if (r && r.boards && r.boards.length) setBoards(r.boards); }).catch(function () {});
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

    var loadRange = useCallback(function (kind) {
      setRangeLoading(true); setRoll(null);
      var t = tzRef.current;
      var to = ymd(new Date(), t);
      var from = kind === "month" ? to.slice(0, 8) + "01" : ymd(daysAgo(6), t);
      return fetchJSON(addBoard(apiRef.current + "/range?from_=" + from + "&to=" + to, boardRef.current))
        .then(function (r) { setRoll(r); }).catch(function () { setRoll(null); })
        .then(function () { setRangeLoading(false); });
    }, []);

    function changeBoard(b) {
      setBoard(b); boardRef.current = b;
      setDigest(null); setRoll(null); setBuilt({});
      if (tab === "day") {
        loadDay(date || "today").then(function (r) { if (r && r.date) setDate(r.date); });
        loadList();
        fetchJSON(addBoard(apiRef.current + "/ensure?days=7", b)).then(function () { loadList(); }).catch(function () {});
      } else {
        loadRange(tab);
      }
    }

    function switchTab(t) {
      setTab(t);
      if (t === "week") loadRange("week");
      else if (t === "month") loadRange("month");
      else if (t === "day" && !digest && date) loadDay(date);
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

    // ---- day sidebar: last 14 days (client-computed), badges from built map ----
    var dayList = [];
    for (var i = 0; i < 14; i++) dayList.push(ymd(daysAgo(i), tz));

    var sidebar = h("div", { style: { width: "150px", flex: "0 0 150px", borderRight: "1px solid var(--color-border)", paddingRight: "0.6rem", overflowY: "auto", maxHeight: "68vh" } },
      dayList.map(function (d, idx) {
        var active = d === date, b = built[d];
        return h("div", { key: d, onClick: function () { setDate(d); }, className: "brf-card",
          style: { cursor: "pointer", padding: "0.35rem 0.5rem", borderRadius: "0.4rem", marginBottom: "0.2rem", fontSize: "0.8rem",
            background: active ? "var(--color-accent)" : "transparent", color: active ? "var(--color-accent-foreground)" : "inherit" } },
          h("div", { style: { fontWeight: 600 } }, idx === 0 ? "Today" : idx === 1 ? "Yesterday" : d.slice(5)),
          h("div", { style: { fontSize: "0.68rem", color: active ? "inherit" : MUTED } },
            b ? ((b.open || 0) + " open \u00b7 " + eur(b.cost_eur)) : (building ? "building\u2026" : "\u2014")));
      }));

    var dayBody;
    if (dayLoading && !digest) dayBody = h(Skeleton);
    else if (!digest) dayBody = h("div", { style: { paddingLeft: "1rem", color: MUTED, fontSize: "0.85rem" } }, "No briefing.");
    else dayBody = h(DigestView, { digest: digest, building: dayLoading || building, onRebuild: rebuild, onResolve: resolve, busyId: busyId });

    var content;
    if (tab === "day") {
      content = h("div", { style: { display: "flex", gap: "0.5rem" } }, sidebar, dayBody);
    } else {
      if (rangeLoading || !roll) content = h(Skeleton);
      else content = h(RangeView, { roll: roll, title: tab === "month" ? "This month" : "Last 7 days" });
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
