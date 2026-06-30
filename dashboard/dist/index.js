(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var hooks = SDK.hooks || {};
  var useState = hooks.useState, useEffect = hooks.useEffect,
      useCallback = hooks.useCallback, useRef = hooks.useRef;
  var C = SDK.components || {};
  var Card = C.Card, CardHeader = C.CardHeader, CardTitle = C.CardTitle, CardContent = C.CardContent;
  var Badge = C.Badge, Button = C.Button, Separator = C.Separator;
  var fetchJSON = SDK.fetchJSON;
  var timeAgo = (SDK.utils && SDK.utils.timeAgo) || function (t) { return new Date(t * 1000).toLocaleString(); };
  var API = "/api/plugins/briefing";

  var KIND_LABEL = { approval: "Needs approval", blocked: "Blocked", failed: "Gave up", instability: "Unstable" };
  var KIND_TONE = { approval: "default", blocked: "secondary", failed: "destructive", instability: "destructive" };
  var MUTED = "var(--color-muted-foreground)";

  function eur(n) { return "\u2248 " + (Number(n) || 0).toFixed(2) + " \u20ac"; }

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

  function Generating(props) {
    var b = props.build || {};
    var total = b.total || 0, done = b.done || 0;
    var pct = total ? Math.max(6, Math.round((done / total) * 100)) : 0;
    return h("div", { className: "brf-fade-in", style: { padding: "2.5rem 1rem", textAlign: "center" } },
      h("div", { className: "brf-spinner brf-spinner-lg", style: { margin: "0 auto 1rem" } }),
      h("div", { style: { fontSize: "0.98rem", marginBottom: "0.4rem" } }, "Generating your briefings\u2026"),
      h("div", { style: { fontSize: "0.8rem", color: MUTED, marginBottom: "1rem" } },
        b.current ? ("Working on " + b.current) : "Reading the board"),
      total
        ? h("div", { className: "brf-progress", style: { maxWidth: "320px", margin: "0 auto" } },
            h("div", { className: "brf-progress-fill", style: { width: pct + "%" } }))
        : h("div", { className: "brf-progress brf-progress-indeterminate", style: { maxWidth: "320px", margin: "0 auto" } }),
      total ? h("div", { style: { fontSize: "0.72rem", color: MUTED, marginTop: "0.55rem" } }, done + " / " + total + " days") : null
    );
  }

  function Skeleton() {
    function bar(w, hgt, mb) {
      return h("div", { className: "brf-skel", style: { height: (hgt || 12) + "px", width: w, marginBottom: (mb == null ? 8 : mb) + "px" } });
    }
    return h("div", { className: "brf-fade-in", style: { paddingLeft: "1rem", flex: 1 } },
      bar("38%", 18, 16), bar("88%"), bar("64%"), bar("72%"),
      h("div", { style: { height: "12px" } }), bar("80%"), bar("52%"),
      h("div", { style: { height: "12px" } }), bar("46%", 12, 8), bar("60%"));
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
        busy ? h("span", { className: "brf-spinner" }) : null)
    );
  }

  function StatusBar(props) {
    var st = props.status; if (!st) return null;
    var b = st.build || {};
    if (b.running) {
      var total = b.total || 0, done = b.done || 0;
      var pct = total ? Math.max(6, Math.round((done / total) * 100)) : 0;
      return h("div", { className: "brf-fade-in", style: {
          display: "flex", alignItems: "center", gap: "0.6rem", fontSize: "0.8rem",
          padding: "0.45rem 0.65rem", borderRadius: "0.45rem", marginBottom: "0.6rem",
          border: "1px solid var(--color-border)", background: "var(--color-card)" } },
        h("span", { className: "brf-spinner" }),
        h("span", { style: { whiteSpace: "nowrap" } }, "Building in background\u2026"),
        h("span", { style: { color: MUTED, whiteSpace: "nowrap" } }, b.current || ""),
        h("div", { style: { flex: 1, minWidth: "60px" } },
          total
            ? h("div", { className: "brf-progress" }, h("div", { className: "brf-progress-fill", style: { width: pct + "%" } }))
            : h("div", { className: "brf-progress brf-progress-indeterminate" })),
        total ? h("span", { style: { color: MUTED, whiteSpace: "nowrap" } }, done + "/" + total) : null
      );
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
    var s1 = useState([]);    var days = s1[0], setDays = s1[1];
    var s2 = useState(null);  var date = s2[0], setDate = s2[1];
    var s3 = useState(null);  var digest = s3[0], setDigest = s3[1];
    var s4 = useState(false); var loading = s4[0], setLoading = s4[1];
    var s5 = useState("");    var busyId = s5[0], setBusyId = s5[1];
    var s6 = useState(null);  var status = s6[0], setStatus = s6[1];
    var bootstrapped = useRef(false);
    var wasRunning = useRef(false);
    var seenDays = useRef({});
    var dateRef = useRef(null); dateRef.current = date;

    var loadDays = useCallback(function (autoSelect) {
      return fetchJSON(API + "/digests?limit=60").then(function (r) {
        var list = (r && r.digests) || [];
        setDays(list);
        if (autoSelect && !dateRef.current && list.length) setDate(list[0].date);
        return list;
      }).catch(function () { return []; });
    }, []);

    var loadDigest = useCallback(function (d) {
      if (!d) return;
      setLoading(true);
      fetchJSON(API + "/digest/" + d).then(function (r) { setDigest(r); })
        .catch(function () { setDigest(null); })
        .then(function () { setLoading(false); });
    }, []);

    var triggerBuild = useCallback(function (payload) {
      return fetchJSON(API + "/build", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}) }).catch(function () {});
    }, []);

    useEffect(function () {
      fetchJSON(API + "/status").then(function (st) {
        setStatus(st);
        wasRunning.current = !!(st && st.build && st.build.running);
        loadDays(true).then(function (list) {
          if (!list.length && !(st && st.build && st.build.running) && !bootstrapped.current) {
            bootstrapped.current = true;
            triggerBuild({ days: 7 });   // today is built first and streams in within ~1s
          }
        });
      }).catch(function () { loadDays(true); });
    }, []);

    useEffect(function () {
      var iv = setInterval(function () {
        fetchJSON(API + "/status").then(function (st) {
          setStatus(st);
          var running = !!(st && st.build && st.build.running);
          if (running) {
            // stream finished days into the list as they land
            loadDays(true);
          } else if (wasRunning.current) {
            loadDays(true).then(function () { if (dateRef.current) loadDigest(dateRef.current); });
          }
          wasRunning.current = running;
        }).catch(function () {});
      }, 1200);
      return function () { clearInterval(iv); };
    }, []);

    useEffect(function () { if (date) loadDigest(date); }, [date]);

    function resolve(id, resolution) {
      setBusyId(id);
      fetchJSON(API + "/decisions/" + id + "/resolve", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ resolution: resolution }) })
        .then(function () { triggerBuild({ date: date }); })
        .catch(function () {}).then(function () { setBusyId(""); });
    }

    var building = !!(status && status.build && status.build.running);

    // sidebar (streams in)
    var sidebar = h("div", { style: { width: "190px", flex: "0 0 190px", borderRight: "1px solid var(--color-border)", paddingRight: "0.75rem", overflowY: "auto", maxHeight: "70vh" } },
      days.length === 0
        ? (building
            ? h("div", { style: { display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.8rem", color: MUTED } },
                h("span", { className: "brf-spinner" }), "Setting up\u2026")
            : h("div", { style: { fontSize: "0.8rem", color: MUTED } }, "No briefings yet."))
        : days.map(function (d) {
            var active = d.date === date;
            var isNew = !seenDays.current[d.date];
            seenDays.current[d.date] = true;
            return h("div", { key: d.date, onClick: function () { setDate(d.date); },
              className: isNew ? "brf-day-new" : "",
              style: { cursor: "pointer", padding: "0.4rem 0.5rem", borderRadius: "0.4rem",
                marginBottom: "0.2rem", fontSize: "0.82rem",
                background: active ? "var(--color-accent)" : "transparent",
                color: active ? "var(--color-accent-foreground)" : "inherit" } },
              h("div", { style: { fontWeight: 600 } }, d.date),
              h("div", { style: { fontSize: "0.72rem", opacity: 0.8 } }, (d.open || 0) + " open \u00b7 " + eur(d.cost_eur)));
          })
    );

    // main panel
    var body;
    if (!digest && building) {
      body = h(Generating, { build: status.build });
    } else if (loading && !digest) {
      body = h(Skeleton, null);
    } else if (!digest) {
      body = h("div", { style: { color: MUTED, fontSize: "0.85rem", paddingLeft: "1rem" } }, "No briefing selected.");
    } else {
      var hd = digest.header || {}, cost = digest.cost || {}, sys = digest.system || {};
      body = h("div", { key: digest.date, className: "brf-fade-in", style: { flex: 1, paddingLeft: "1rem", overflowY: "auto", maxHeight: "70vh" } },
        h("div", { style: { display: "flex", alignItems: "baseline", gap: "0.75rem", marginBottom: "0.35rem", flexWrap: "wrap" } },
          h("h3", { style: { margin: 0, fontSize: "1.05rem" } }, digest.date + " \u00b7 " + (hd.status || "")),
          h("span", { style: { fontSize: "0.85rem", color: MUTED } },
            (hd.open ? hd.open + " open" : "nothing open") + " \u00b7 " + eur(hd.cost_eur) + " / " + (cost.budget_daily || 0).toFixed(0) + " \u20ac"),
          Button ? h(Button, { size: "sm", variant: "secondary", disabled: building,
            onClick: function () { triggerBuild({ date: date }); } },
            building ? h("span", null, h("span", { className: "brf-spinner", style: { marginRight: "0.35rem" } }), "Building\u2026") : "Rebuild") : null,
          digest.generated_at ? h("span", { style: { fontSize: "0.72rem", color: MUTED } }, "built " + timeAgo(digest.generated_at)) : null),

        (digest.hand && digest.hand.length)
          ? h(Section, { title: "Needs your call" },
              digest.hand.map(function (d) { return h(HandItem, { key: d.id, d: d, onResolve: resolve, busy: busyId === d.id }); }))
          : h(Section, { title: "Needs your call" }, h("div", { style: { fontSize: "0.82rem", color: MUTED } }, "Nothing open.")),

        Separator ? h(Separator, { style: { margin: "0.5rem 0" } }) : null,

        (digest.done && digest.done.length)
          ? h(Section, { title: "Done today" }, digest.done.map(function (it, i) {
              var b = (it.bullets && it.bullets[0]) || it.why || "done";
              return h("div", { key: i, style: { fontSize: "0.84rem", marginBottom: "0.25rem" } }, h("strong", null, it.title), " \u2014 ", b); }))
          : null,

        (digest.in_progress && digest.in_progress.length)
          ? h(Section, { title: "Active" }, h("div", { style: { fontSize: "0.84rem" } },
              digest.in_progress.map(function (t) { return t.title; }).join(", ")))
          : null,

        (digest.learned && digest.learned.length)
          ? h(Section, { title: "Noted" }, digest.learned.map(function (l, i) {
              return h("div", { key: i, style: { fontSize: "0.82rem", marginBottom: "0.2rem" } }, "\u2022 " + l); }))
          : null,

        h(Section, { title: "Cost" },
          h("div", { style: { fontSize: "0.84rem" } },
            "Today " + eur(cost.today_eur) + " / " + (cost.budget_daily || 0).toFixed(0) + " \u20ac \u00b7 " +
            "Month " + eur(cost.month_eur) + " / " + (cost.budget_monthly || 0).toFixed(0) + " \u20ac \u00b7 " + (cost.runs || 0) + " runs"),
          cost.caveat ? h("div", { style: { fontSize: "0.74rem", color: MUTED, marginTop: "0.2rem" } }, "\u26a0 " + cost.caveat) : null),

        h(Section, { title: "System" },
          h("div", { style: { fontSize: "0.84rem" } }, sys.stable ? "stable" : ((sys.notes || []).join(", ") || "\u2014")))
      );
    }

    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Briefing")),
      h(CardContent, null,
        h(StatusBar, { status: status }),
        h("div", { style: { display: "flex", gap: "0.5rem" } }, sidebar, body))
    );
  }

  window.__HERMES_PLUGINS__.register("briefing", BriefingPage);
})();
