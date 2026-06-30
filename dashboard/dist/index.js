(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var hooks = SDK.hooks || {};
  var useState = hooks.useState, useEffect = hooks.useEffect, useCallback = hooks.useCallback;
  var C = SDK.components || {};
  var Card = C.Card, CardHeader = C.CardHeader, CardTitle = C.CardTitle, CardContent = C.CardContent;
  var Badge = C.Badge, Button = C.Button, Separator = C.Separator;
  var fetchJSON = SDK.fetchJSON;
  var API = "/api/plugins/briefing";

  var KIND_LABEL = {
    approval: "Freigabe nötig", blocked: "Blockiert",
    failed: "Aufgegeben", instability: "Instabil"
  };
  var KIND_TONE = { approval: "default", blocked: "secondary", failed: "destructive", instability: "destructive" };

  function eur(n) { return "≈ " + (Number(n) || 0).toFixed(2) + " €"; }

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
              h(Button, { key: "ok", size: "sm", disabled: busy, onClick: function () { onResolve(d.id, "ok"); } }, "OK geben"),
              h(Button, { key: "veto", size: "sm", variant: "destructive", disabled: busy, onClick: function () { onResolve(d.id, "veto"); } }, "Veto")
            ]
          : null
      )
    );
  }

  function BriefingPage() {
    var s1 = useState([]); var days = s1[0], setDays = s1[1];
    var s2 = useState(null); var date = s2[0], setDate = s2[1];
    var s3 = useState(null); var digest = s3[0], setDigest = s3[1];
    var s4 = useState(false); var loading = s4[0], setLoading = s4[1];
    var s5 = useState(""); var busyId = s5[0], setBusyId = s5[1];

    var loadDays = useCallback(function () {
      fetchJSON(API + "/digests?limit=60").then(function (r) {
        var list = (r && r.digests) || [];
        setDays(list);
        if (!date && list.length) setDate(list[0].date);
      }).catch(function () { setDays([]); });
    }, [date]);

    var loadDigest = useCallback(function (d, rebuild) {
      if (!d) return;
      setLoading(true);
      var url = API + "/digest/" + d + (rebuild ? "?rebuild=true" : "");
      fetchJSON(url).then(function (r) { setDigest(r); }).catch(function () { setDigest(null); })
        .then(function () { setLoading(false); });
    }, []);

    useEffect(function () { loadDays(); }, []);
    useEffect(function () { if (date) loadDigest(date, false); }, [date]);

    function resolve(id, resolution) {
      setBusyId(id);
      fetchJSON(API + "/decisions/" + id + "/resolve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution: resolution })
      }).then(function () { loadDigest(date, true); loadDays(); })
        .catch(function () {})
        .then(function () { setBusyId(""); });
    }

    // sidebar list
    var sidebar = h("div", { style: { width: "190px", flex: "0 0 190px", borderRight: "1px solid var(--color-border)", paddingRight: "0.75rem", overflowY: "auto", maxHeight: "70vh" } },
      days.length === 0
        ? h("div", { style: { fontSize: "0.8rem", color: "var(--color-muted-foreground)" } }, "Noch keine Reports. Baue einen mit der CLI oder warte auf den Timer.")
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
                (d.open || 0) + " offen · " + eur(d.cost_eur))
            );
          })
    );

    var body;
    if (loading && !digest) {
      body = h("div", { style: { color: "var(--color-muted-foreground)", fontSize: "0.85rem" } }, "Lädt…");
    } else if (!digest) {
      body = h("div", { style: { color: "var(--color-muted-foreground)", fontSize: "0.85rem" } }, "Kein Report ausgewählt.");
    } else {
      var hd = digest.header || {}, cost = digest.cost || {}, sys = digest.system || {};
      body = h("div", { style: { flex: 1, paddingLeft: "1rem", overflowY: "auto", maxHeight: "70vh" } },
        h("div", { style: { display: "flex", alignItems: "baseline", gap: "0.75rem", marginBottom: "0.75rem", flexWrap: "wrap" } },
          h("h3", { style: { margin: 0, fontSize: "1.05rem" } }, digest.date + " · " + (hd.status || "")),
          h("span", { style: { fontSize: "0.85rem", color: "var(--color-muted-foreground)" } },
            (hd.open || 0) + " offen · " + eur(hd.cost_eur) + " / " + (cost.budget_daily || 0).toFixed(0) + " €"),
          Button ? h(Button, { size: "sm", variant: "secondary", onClick: function () { loadDigest(date, true); } }, "Neu bauen") : null
        ),

        (digest.hand && digest.hand.length)
          ? h(Section, { title: "Was deine Hand braucht" },
              digest.hand.map(function (d) {
                return h(HandItem, { key: d.id, d: d, onResolve: resolve, busy: busyId === d.id });
              }))
          : h(Section, { title: "Was deine Hand braucht" },
              h("div", { style: { fontSize: "0.82rem", color: "var(--color-muted-foreground)" } }, "Nichts offen.")),

        Separator ? h(Separator, { style: { margin: "0.5rem 0" } }) : null,

        (digest.done && digest.done.length)
          ? h(Section, { title: "Heute fertig" },
              digest.done.map(function (it, i) {
                var b = (it.bullets && it.bullets[0]) || it.why || "fertig";
                return h("div", { key: i, style: { fontSize: "0.84rem", marginBottom: "0.25rem" } },
                  h("strong", null, it.title), " — ", b);
              }))
          : null,

        (digest.in_progress && digest.in_progress.length)
          ? h(Section, { title: "In Arbeit" },
              h("div", { style: { fontSize: "0.84rem" } },
                digest.in_progress.map(function (t) { return t.title; }).join(", ")))
          : null,

        (digest.learned && digest.learned.length)
          ? h(Section, { title: "Notiert" },
              digest.learned.map(function (l, i) {
                return h("div", { key: i, style: { fontSize: "0.82rem", marginBottom: "0.2rem" } }, "• " + l);
              }))
          : null,

        h(Section, { title: "Kosten" },
          h("div", { style: { fontSize: "0.84rem" } },
            "Heute " + eur(cost.today_eur) + " / " + (cost.budget_daily || 0).toFixed(0) + " € · " +
            "Monat " + eur(cost.month_eur) + " / " + (cost.budget_monthly || 0).toFixed(0) + " € · " +
            (cost.runs || 0) + " Runs"),
          cost.caveat ? h("div", { style: { fontSize: "0.74rem", color: "var(--color-muted-foreground)", marginTop: "0.2rem" } }, "⚠ " + cost.caveat) : null
        ),

        h(Section, { title: "System" },
          h("div", { style: { fontSize: "0.84rem" } },
            sys.stable ? "stabil" : ((sys.notes || []).join(", ") || "—")))
      );
    }

    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Briefing")),
      h(CardContent, null,
        h("div", { style: { display: "flex", gap: "0.5rem" } }, sidebar, body))
    );
  }

  window.__HERMES_PLUGINS__.register("briefing", BriefingPage);
})();
