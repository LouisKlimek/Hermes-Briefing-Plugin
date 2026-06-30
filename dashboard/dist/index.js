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

  var KIND_LABEL = {
    approval: "Needs approval", blocked: "Blocked",
    failed: "Gave up", instability: "Unstable"
  };
  var KIND_TONE = { approval: "default", blocked: "secondary", failed: "destructive", instability: "destructive" };

  function eur(n) { return "\u2248 " + (Number(n) || 0).toFixed(2) + " \u20ac"; }

  function nextBuildLabel(nextRun) {
    if (!nextRun || !nextRun.iso) return null;
    var d = new Date(nextRun.iso);
    if (isNaN(d.getTime())) return null;
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    var tomorrow = new Date(now.getTime() + 86400000);
    var isTomorrow = d.toDateString() === tomorrow.toDateString();
    var hhmm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    var when = sameDay ? "today" : isTomorrow ? "tomorrow" : d.toLocaleDateString();
    return when + " " + hhmm;
  }

  function Section(props) {
    return h("div", { style: { marginBottom: "1rem" } },
      h("div", {
        style: {
          fontSize: "0.7rem", letterSpacing: "0.08em", textTransform: "uppercase",
          color: "var(--color-muted-foreground)", marginBottom: "0.4rem"
        }
      }, props.title),
      props.children
    );
  }

  function HandItem(props) {
    var d = props.d, onResolve = props.onResolve, busy = props.busy;
    return h("div", {
      style: {
        border: "1px solid var(--color-border)", borderRadius: "var(--radius, 0.5rem)",
        padding: "0.6rem 0.75rem", marginBottom: "0.5rem", background: "var(--color-card)"
      }
    },
      h("div", { style: { display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.25rem" } },
        Badge ? h(Badge, { variant: KIND_TONE[d.kind] || "default" }, KIND_LABEL[d.kind] || d.kind) : null,
        h("strong", { style: { fontSize: "0.9rem" } }, d.title)
      ),
      d.detail ? h("div", { style: { fontSize: "0.8rem", color: "var(--color-muted-foreground)", marginBottom: "0.4rem" } }, d.detail) : null,
      h("div", { style: { display: "flex", gap: "0.4rem" } },
        Button
          ? [
              h(Button, { key: "ok", size: "sm", disabled: busy, onClick: function () { onResolve(d.id, "ok"); } }, "Give OK"),
              h(Button, { key: "veto", size: "sm", variant: "destructive", disabled: busy, onClick: function () { onResolve(d.id, "veto"); } }, "Veto")
            ]
          : null
      )
    );
  }

  function StatusBar(props) {
    var st = props.status;
    if (!st) return null;
    var build = st.build || {};
    if (build.running) {
      var pct = build.total ? Math.round((build.done / build.total) * 100) : null;
      return h("div", {
        style: {
          display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.8rem",
          padding: "0.4rem 0.6rem", borderRadius: "0.4rem", marginBottom: "0.6rem",
          background: "var(--color-accent)", color: "var(--color-accent-foreground)"
        }
      },
        h("span", { className: "animate-pulse" }, "\u23f3"),
        h("span", null, "Building in background\u2026 " + (build.current || "") +
          (pct !== null ? "  (" + build.done + "/" + build.total + ")" : ""))
      );
    }
    var bits = [];
    var nb = nextBuildLabel(st.next_run);
    if (nb) bits.push("Next build: " + nb);
    if (build.finished_at) bits.push("last built " + timeAgo(build.finished_at));
    if (build.error) bits.push("\u26a0 " + build.error);
    if (!bits.length) return null;
    return h("div", { style: { fontSize: "0.76rem", color: "var(--color-muted-foreground)", marginBottom: "0.6rem" } },
      bits.join(" \u00b7 "));
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
    var dateRef = useRef(null); dateRef.current = date;

    var loadDays = useCallback(function (autoSelect) {
      return fetchJSON(API + "/digests?limit=60").then(function (r) {
        var list = (r && r.digests) || [];
        setDays(list);
        if (autoSelect && !dateRef.current && list.length) setDate(list[0].date);
        return list;
      }).catch(function () { setDays([]); return []; });
    }, []);

    var loadDigest = useCallback(function (d) {
      if (!d) return;
      setLoading(true);
      fetchJSON(API + "/digest/" + d).then(function (r) { setDigest(r); })
        .catch(function () { setDigest(null); })
        .then(function () { setLoading(false); });
    }, []);

    var triggerBuild = useCallback(function (payload) {
      return fetchJSON(API + "/build", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      }).catch(function () {});
    }, []);

    // initial load + first-open bootstrap
    useEffect(function () {
      fetchJSON(API + "/status").then(function (st) {
        setStatus(st);
        wasRunning.current = !!(st && st.build && st.build.running);
        loadDays(true).then(function (list) {
          if (!list.length && !(st && st.build && st.build.running) && !bootstrapped.current) {
            bootstrapped.current = true;
            triggerBuild({ days: 7 });
          }
        });
      }).catch(function () { loadDays(true); });
    }, []);

    // poll status; on running->idle, refresh the list + current day
    useEffect(function () {
      var iv = setInterval(function () {
        fetchJSON(API + "/status").then(function (st) {
          setStatus(st);
          var running = !!(st && st.build && st.build.running);
          if (wasRunning.current && !running) {
            loadDays(true).then(function () { if (dateRef.current) loadDigest(dateRef.current); });
          }
          wasRunning.current = running;
        }).catch(function () {});
      }, 2000);
      return function () { clearInterval(iv); };
    }, []);

    useEffect(function () { if (date) loadDigest(date); }, [date]);

    function resolve(id, resolution) {
      setBusyId(id);
      fetchJSON(API + "/decisions/" + id + "/resolve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution: resolution })
      }).then(function () { triggerBuild({ date: date }); })
        .catch(function () {})
        .then(function () { setBusyId(""); });
    }

    var building = !!(status && status.build && status.build.running);

    // sidebar
    var sidebar = h("div", { style: { width: "190px", flex: "0 0 190px", borderRight: "1px solid var(--color-border)", paddingRight: "0.75rem", overflowY: "auto", maxHeight: "70vh" } },
      days.length === 0
        ? h("div", { style: { fontSize: "0.8rem", color: "var(--color-muted-foreground)" } },
            building ? "Setting up your briefings\u2026" : "No briefings yet. Building the last 7 days\u2026")
        : days.map(function (d) {
            var active = d.date === date;
            return h("div", {
              key: d.date, onClick: function () { setDate(d.date); },
              style: {
                cursor: "pointer", padding: "0.4rem 0.5rem", borderRadius: "0.4rem",
                marginBottom: "0.2rem", fontSize: "0.82rem",
                background: active ? "var(--color-accent)" : "transparent",
                color: active ? "var(--color-accent-foreground)" : "inherit"
              }
            },
              h("div", { style: { fontWeight: 600 } }, d.date),
              h("div", { style: { fontSize: "0.72rem", opacity: 0.8 } },
                (d.open || 0) + " open \u00b7 " + eur(d.cost_eur))
            );
          })
    );

    // main panel
    var body;
    if (loading && !digest) {
      body = h("div", { style: { color: "var(--color-muted-foreground)", fontSize: "0.85rem" } }, "Loading\u2026");
    } else if (!digest) {
      body = h("div", { style: { color: "var(--color-muted-foreground)", fontSize: "0.85rem" } },
        building ? "Building briefings\u2026 this only happens once." : "No briefing selected.");
    } else {
      var hd = digest.header || {}, cost = digest.cost || {}, sys = digest.system || {};
      body = h("div", { style: { flex: 1, paddingLeft: "1rem", overflowY: "auto", maxHeight: "70vh" } },
        h("div", { style: { display: "flex", alignItems: "baseline", gap: "0.75rem", marginBottom: "0.35rem", flexWrap: "wrap" } },
          h("h3", { style: { margin: 0, fontSize: "1.05rem" } }, digest.date + " \u00b7 " + (hd.status || "")),
          h("span", { style: { fontSize: "0.85rem", color: "var(--color-muted-foreground)" } },
            (hd.open || 0) + " open \u00b7 " + eur(hd.cost_eur) + " / " + (cost.budget_daily || 0).toFixed(0) + " \u20ac"),
          Button ? h(Button, { size: "sm", variant: "secondary", disabled: building,
            onClick: function () { triggerBuild({ date: date }); } }, building ? "Building\u2026" : "Rebuild") : null,
          digest.generated_at ? h("span", { style: { fontSize: "0.72rem", color: "var(--color-muted-foreground)" } },
            "built " + timeAgo(digest.generated_at)) : null
        ),

        (digest.hand && digest.hand.length)
          ? h(Section, { title: "Needs your call" },
              digest.hand.map(function (d) {
                return h(HandItem, { key: d.id, d: d, onResolve: resolve, busy: busyId === d.id });
              }))
          : h(Section, { title: "Needs your call" },
              h("div", { style: { fontSize: "0.82rem", color: "var(--color-muted-foreground)" } }, "Nothing open.")),

        Separator ? h(Separator, { style: { margin: "0.5rem 0" } }) : null,

        (digest.done && digest.done.length)
          ? h(Section, { title: "Done today" },
              digest.done.map(function (it, i) {
                var b = (it.bullets && it.bullets[0]) || it.why || "done";
                return h("div", { key: i, style: { fontSize: "0.84rem", marginBottom: "0.25rem" } },
                  h("strong", null, it.title), " \u2014 ", b);
              }))
          : null,

        (digest.in_progress && digest.in_progress.length)
          ? h(Section, { title: "Active" },
              h("div", { style: { fontSize: "0.84rem" } },
                digest.in_progress.map(function (t) { return t.title; }).join(", ")))
          : null,

        (digest.learned && digest.learned.length)
          ? h(Section, { title: "Noted" },
              digest.learned.map(function (l, i) {
                return h("div", { key: i, style: { fontSize: "0.82rem", marginBottom: "0.2rem" } }, "\u2022 " + l);
              }))
          : null,

        h(Section, { title: "Cost" },
          h("div", { style: { fontSize: "0.84rem" } },
            "Today " + eur(cost.today_eur) + " / " + (cost.budget_daily || 0).toFixed(0) + " \u20ac \u00b7 " +
            "Month " + eur(cost.month_eur) + " / " + (cost.budget_monthly || 0).toFixed(0) + " \u20ac \u00b7 " +
            (cost.runs || 0) + " runs"),
          cost.caveat ? h("div", { style: { fontSize: "0.74rem", color: "var(--color-muted-foreground)", marginTop: "0.2rem" } }, "\u26a0 " + cost.caveat) : null
        ),

        h(Section, { title: "System" },
          h("div", { style: { fontSize: "0.84rem" } },
            sys.stable ? "stable" : ((sys.notes || []).join(", ") || "\u2014")))
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
