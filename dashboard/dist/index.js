/**
 * Capability Router dashboard panel.
 *
 * Uses the official Hermes Plugin SDK (window.__HERMES_PLUGIN_SDK__).
 * API: /api/plugins/delegate-profile/
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const {
    Card, CardHeader, CardTitle, CardContent,
    Badge, Button, Input, Separator,
  } = SDK.components;
  const { fetchJSON } = SDK;

  const API = "/api/plugins/delegate-profile";

  const TIER_COLORS = {
    T1: "#3fb950", T2: "#d2991d", T3: "#a371f7", T4: "#f85149",
  };

  // ── API hooks ────────────────────────────────────────────────────

  function useEndpoint(path, deps) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);

    const load = useCallback(() => {
      setLoading(true);
      fetchJSON(API + path)
        .then((d) => { setData(d); setError(null); })
        .catch((e) => setError(e.message || String(e)))
        .finally(() => setLoading(false));
    }, deps || []);

    useEffect(() => { load(); }, [load]);
    return { data, error, loading, reload: load };
  }

  // ── Components ───────────────────────────────────────────────────

  function StatusPill({ enabled }) {
    return React.createElement(Badge, {
      variant: enabled ? "default" : "secondary",
      style: { fontSize: "0.75em" },
    }, enabled ? "ENABLED" : "DISABLED");
  }

  function TierBadge({ tier, model }) {
    const color = TIER_COLORS[tier] || "#999";
    return React.createElement("span", {
      style: {
        fontFamily: "monospace", padding: "1px 6px",
        borderRadius: "3px", margin: "0 4px 4px 0",
        color, border: `1px solid ${color}44`, fontSize: "0.78em",
      },
    }, `${tier}=${model}`);
  }

  function StatusCard() {
    const { data, loading } = useEndpoint("/status", []);
    if (loading || !data) return null;
    return React.createElement(Card, null,
      React.createElement(CardHeader, null,
        React.createElement("div", {
          style: { display: "flex", alignItems: "center", gap: "8px" },
        },
          React.createElement(CardTitle, null, "Capability Router"),
          React.createElement(StatusPill, { enabled: data.enabled }),
        ),
      ),
      React.createElement(CardContent, null,
        React.createElement("div", {
          style: { display: "flex", gap: "16px", fontSize: "0.85em",
                   color: "hsl(var(--muted-foreground))" },
        },
          React.createElement("span", null, `${data.rules_count} rules`),
          React.createElement("span", null, `Classifier: ${data.classifier_model}`),
          React.createElement("span", null, `Banned: ${data.banned_models.length}`),
        ),
      ),
    );
  }

  function RulesCard() {
    const { data, loading } = useEndpoint("/rules", []);
    if (loading || !data) return null;

    return React.createElement(Card, null,
      React.createElement(CardHeader, null,
        React.createElement(CardTitle, null, "📋 Rules"),
      ),
      React.createElement(CardContent, null,
        (data.rules || []).map((r, i) =>
          React.createElement("div", {
            key: r.id || i,
            style: { fontSize: "0.8em", padding: "4px 0",
                     borderBottom: "1px solid hsl(var(--border))" },
          },
            React.createElement("span", {
              style: { fontFamily: "monospace", color: "hsl(var(--primary))" },
            }, r.id),
            React.createElement(Badge, {
              variant: "outline",
              style: { marginLeft: "6px", fontSize: "0.65em",
                       color: r.status === "stable" ? "#3fb950" : "#d2991d" },
            }, r.status || "stable"),
            React.createElement("span", {
              style: { marginLeft: "6px", color: "hsl(var(--muted-foreground))" },
            }, JSON.stringify(r.when)),
            React.createElement("span", { style: { marginLeft: "4px" } },
              "→ " + JSON.stringify(r.then)),
          ),
        ),
        React.createElement("div", {
          style: { fontSize: "0.8em", padding: "4px 0" },
        },
          React.createElement("span", {
            style: { fontFamily: "monospace",
                     color: "hsl(var(--muted-foreground))" },
          }, "default"),
          React.createElement("span", { style: { marginLeft: "4px" } },
            "→ " + JSON.stringify(data.default)),
        ),
        React.createElement(Separator, { style: { margin: "8px 0" } }),
        React.createElement("div", {
          style: { marginTop: "4px", fontSize: "0.78em" },
        },
          "Tiers: ",
          Object.entries(data.tiers || {}).map(([k, v]) =>
            React.createElement(TierBadge, { key: k, tier: k, model: v.model }),
          ),
        ),
      ),
    );
  }

  function BlocklistCard() {
    const { data, loading } = useEndpoint("/blocklist", []);
    if (loading || !data) return null;

    const cooldowns = data.breaker_cooldowns || [];

    return React.createElement(Card, null,
      React.createElement(CardHeader, null,
        React.createElement(CardTitle, null, "⛔ Blocklist"),
      ),
      React.createElement(CardContent, null,
        React.createElement("div", {
          style: { fontWeight: 600, marginBottom: "4px", fontSize: "0.85em" },
        }, "Manual bans:"),
        (data.manual_bans || []).length === 0
          ? React.createElement("p", {
              style: { fontSize: "0.8em",
                       color: "hsl(var(--muted-foreground))" },
            }, "(none)")
          : data.manual_bans.map((b, i) =>
              React.createElement("div", {
                key: i,
                style: { fontFamily: "monospace", fontSize: "0.8em",
                         padding: "2px 0" },
              }, `🚫 ${b.model} @ ${b.provider || "*"} — ${b.reason || ""}`),
            ),
        cooldowns.length > 0 && React.createElement("div", null,
          React.createElement("div", {
            style: { fontWeight: 600, marginTop: "8px", marginBottom: "4px",
                     fontSize: "0.85em" },
          }, "Breaker cooldowns:"),
          cooldowns.map((c, i) =>
            React.createElement("div", {
              key: i,
              style: { fontFamily: "monospace", fontSize: "0.8em",
                       padding: "2px 0" },
            },
              React.createElement(Badge, {
                variant: c.state === "OPEN" ? "destructive" : "secondary",
                style: { fontSize: "0.65em", marginRight: "4px" },
              }, c.state),
              `${c.model_key} — ${Math.round(c.cooldown_remaining_s)}s left`,
              React.createElement("span", {
                style: { color: "hsl(var(--muted-foreground))",
                         marginLeft: "4px" },
              }, `(backoff ${c.backoff_seconds}s, last: ${c.last_failure_kind})`),
            ),
          ),
        ),
        React.createElement("div", {
          style: { marginTop: "6px", fontSize: "0.8em",
                   color: "hsl(var(--muted-foreground))" },
        }, `Fallback: ${(data.fallback_chain || []).join(" → ") || "(none)"}`),
      ),
    );
  }

  function ExplainCard() {
    const [task, setTask] = useState("");
    const [result, setResult] = useState(null);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(false);

    const trace = useCallback(() => {
      if (!task.trim()) return;
      setLoading(true);
      setError(null);
      fetchJSON(`${API}/explain?task=${encodeURIComponent(task)}`)
        .then((d) => setResult(d))
        .catch((e) => setError(e.message || String(e)))
        .finally(() => setLoading(false));
    }, [task]);

    return React.createElement(Card, null,
      React.createElement(CardHeader, null,
        React.createElement(CardTitle, null, "🔮 Explain Task"),
      ),
      React.createElement(CardContent, null,
        React.createElement(Input, {
          placeholder: "Paste a task description to trace its routing...",
          value: task,
          onChange: (e) => setTask(e.target.value),
          onKeyDown: (e) => { if (e.key === "Enter") trace(); },
          style: { marginBottom: "8px" },
        }),
        React.createElement(Button, {
          onClick: trace, disabled: loading || !task.trim(),
          style: { marginBottom: "8px" },
        }, loading ? "Tracing..." : "Trace Route"),
        result && React.createElement("pre", {
          style: {
            whiteSpace: "pre-wrap", padding: "10px",
            background: "hsl(var(--muted))",
            borderRadius: "6px", fontSize: "0.78em",
            maxHeight: "250px", overflow: "auto",
          },
        }, JSON.stringify(result, null, 2)),
        error && React.createElement("p", {
          style: { color: "#f85149", fontSize: "0.8em" },
        }, error),
      ),
    );
  }

  function LogCard() {
    const { data, loading, reload } = useEndpoint("/log?tail=30", []);
    const entries = (data && data.entries) ? data.entries.slice().reverse() : [];

    return React.createElement(Card, null,
      React.createElement(CardHeader, null,
        React.createElement("div", {
          style: { display: "flex", justifyContent: "space-between",
                   alignItems: "center" },
        },
          React.createElement(CardTitle, null, "📜 Decision Log"),
          React.createElement(Button, {
            variant: "outline", onClick: reload, style: { fontSize: "0.75em" },
          }, "↻ Refresh"),
        ),
      ),
      React.createElement(CardContent, null,
        React.createElement("div", {
          style: { maxHeight: "250px", overflow: "auto" },
        },
          loading
            ? React.createElement("p", { style: { fontSize: "0.8em" } }, "Loading...")
            : entries.length === 0
              ? React.createElement("p", {
                  style: { fontSize: "0.8em",
                           color: "hsl(var(--muted-foreground))" },
                }, "No routing decisions yet")
              : entries.map((e, i) => {
                  const time = new Date(e.ts * 1000).toLocaleTimeString();
                  return React.createElement("div", {
                    key: i,
                    style: { padding: "2px 4px",
                             borderBottom: "1px solid hsl(var(--border))",
                             fontSize: "0.75em", fontFamily: "monospace" },
                  },
                    React.createElement("span", {
                      style: { color: "hsl(var(--muted-foreground))" },
                    }, time),
                    React.createElement("span", {
                      style: { color: "hsl(var(--primary))", marginLeft: "6px" },
                    }, `cause=${e.cause}`),
                    ` rule=${e.rule_id || "-"} → ${e.output?.profile || ""}/${e.output?.model || ""}`,
                    React.createElement("span", {
                      style: { color: "hsl(var(--muted-foreground))",
                               marginLeft: "6px" },
                    }, e.task || ""),
                  );
                }),
        ),
      ),
    );
  }

  // ── Main page ────────────────────────────────────────────────────

  function RouterPage() {
    return React.createElement("div", null,
      React.createElement(StatusCard, null),
      React.createElement("div", {
        style: { display: "flex", gap: "12px", marginBottom: "12px" },
      },
        React.createElement("div", { style: { flex: 1 } },
          React.createElement(RulesCard, null)),
        React.createElement("div", { style: { flex: 1 } },
          React.createElement(BlocklistCard, null)),
      ),
      React.createElement(ExplainCard, null),
      React.createElement(LogCard, null),
    );
  }

  window.__HERMES_PLUGINS__.register("delegate-profile", RouterPage);
})();
