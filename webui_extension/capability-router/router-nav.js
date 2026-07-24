(() => {
  'use strict';

  /*
  THESIS: Make a process-isolation router legible without making it look editable.
  OWN-WORLD: Inherit Hermes One tokens; compact diagnostic cards, not a dashboard clone.
  STORY: An operator sees the live policy, traces a Stage-0 decision, then returns to work.
  FIRST VIEWPORT: Status and a single trace input lead; policy and breaker follow below.
  FORM: Existing Hermes One rail/sidebar panel extension; no new visual world or route.
  */

  // Capability Router — Hermes One extension.
  //
  // Adds a 'Router' button to the rail + sidebar (same pattern as the
  // Office 3D launcher) that toggles an extension-owned panel. The panel
  // is READ-ONLY (V1): it talks only to the consented per-extension sidecar
  // proxy at /api/extensions/capability-router/sidecar/*, which is guarded
  // by the WebUI session + CSRF + token-v1. No second policy source, no
  // fabricated telemetry — every number comes from the same router.yaml and
  // core modules the CLI and gateway use.

  const EXT_ID = 'capability-router';
  const SIDE = `/api/extensions/${EXT_ID}/sidecar`;
  const PANEL_ID = 'capability-router-panel';

  const icon =
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="12" r="3"/><path d="M9 6h4a2 2 0 0 1 2 2v1"/><path d="M9 18h4a2 2 0 0 0 2-2v-1"/></svg>';

  // ---- safe DOM helpers (no innerHTML for dynamic data) -------------------
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }

  async function getJSON(path) {
    const r = await fetch(SIDE + path, {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Object({ status: r.status, body });
    return body;
  }

  function formatCooldowns(cooldowns) {
    if (!Array.isArray(cooldowns) || !cooldowns.length) return 'none';
    return cooldowns.map((entry) => {
      const seconds = Math.max(0, Math.floor(Number(entry.cooldown_remaining_s) || 0));
      const remaining = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
      return `${entry.model_key || '—'} ${entry.state || '—'} ${remaining}`;
    }).join(', ');
  }

  // ---- panel rendering ----------------------------------------------------
  function showPanel() {
    document.querySelectorAll('main > .main-view').forEach((view) => {
      view.hidden = view.id !== PANEL_ID;
    });
  }

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    panel = el('section', 'main-view capability-router-panel');
    panel.id = PANEL_ID;
    panel.hidden = true;
    const head = el('div', 'cr-head');
    head.appendChild(el('h2', 'cr-title', 'Capability Router'));
    const refresh = el('button', 'cr-refresh', 'Refresh');
    refresh.type = 'button';
    refresh.addEventListener('click', () => load(panel));
    head.appendChild(refresh);
    panel.appendChild(head);
    panel.appendChild(el('div', 'cr-body'));
    document.querySelector('main')?.appendChild(panel);
    return panel;
  }

  function card(title) {
    const c = el('div', 'cr-card');
    c.appendChild(el('div', 'cr-card-title', title));
    const b = el('div', 'cr-card-body');
    c.appendChild(b);
    return { card: c, body: b };
  }

  function kv(body, k, v) {
    const row = el('div', 'cr-kv');
    row.appendChild(el('span', 'cr-k', k));
    row.appendChild(el('span', 'cr-v', v));
    body.appendChild(row);
  }

  async function load(panel) {
    const body = panel.querySelector('.cr-body');
    body.textContent = '';
    body.appendChild(el('div', 'cr-loading', 'Loading router state…'));
    let status, policy, bl, lint;
    try {
      [status, policy, bl, lint] = await Promise.all([
        getJSON('/status'),
        getJSON('/policy'),
        getJSON('/blocklist'),
        getJSON('/lint'),
      ]);
    } catch (e) {
      body.textContent = '';
      const err = el('div', 'cr-error');
      err.setAttribute('role', 'alert');
      err.setAttribute('aria-live', 'assertive');
      const code = e && e.status ? e.status : '?';
      if (code === 403) {
        err.textContent =
          'Sidecar proxy not consented. Approve it in Settings → Extensions, then Refresh.';
      } else if (code === 503) {
        err.textContent =
          'Sidecar token file missing (503). Start the router-sidecar service, then Refresh.';
      } else {
        err.textContent = `Could not reach the router sidecar (HTTP ${code}).`;
      }
      body.appendChild(err);
      return;
    }
    body.textContent = '';

    // Status
    const s = card('Status');
    kv(s.body, 'Enabled', status.enabled ? 'yes' : 'no');
    kv(s.body, 'Rules', status.rules_count);
    kv(s.body, 'Tiers', (status.tiers || []).join(', '));
    const classifier = status.classifier || {};
    kv(s.body, 'Classifier', `${classifier.model || '—'} (${classifier.provider || '—'})`);
    kv(s.body, 'Auto-breaker', status.breaker_enabled ? 'on' : 'off');
    const fs = policy.fail_safe || {};
    kv(s.body, 'Fail-safe', `${fs.profile || '—'} · ${fs.model || '—'} (${fs.provider || '—'})`);
    const validationErrors = status.validation_errors || lint.errors || [];
    kv(s.body, 'Config valid', status.valid && lint.valid ? 'yes' : `no (${validationErrors.length} errors)`);
    body.appendChild(s.card);

    // Trace Route (deterministic dry-run — no LLM)
    const t = card('Trace Route (Stage 0 dry-run)');
    const form = el('div', 'cr-trace-form');
    const input = el('input', 'cr-trace-input');
    input.type = 'text';
    input.placeholder = 'Describe a task…';
    const run = el('button', 'cr-trace-run', 'Trace');
    run.type = 'button';
    const out = el('div', 'cr-trace-out');
    async function trace() {
      out.textContent = '';
      const task = input.value.trim();
      if (!task) return;
      try {
        const r = await getJSON('/explain?task=' + encodeURIComponent(task));
        const decision = r.decision || {};
        const o = decision.output || {};
        kv(out, 'Matched rule', decision.matched_rule_id || '(default)');
        kv(out, 'Cause', decision.cause || '—');
        if (r.requires_classifier) {
          kv(out, 'Decision', 'classifier needed (Stage 1)');
        } else {
          kv(out, 'Profile', o.profile || '—');
          kv(out, 'Model', o.model || '—');
          kv(out, 'Provider', o.provider || '—');
        }
      } catch (e) {
        const code = e && e.status ? e.status : '?';
        out.appendChild(el('div', 'cr-error', `Trace failed (HTTP ${code}).`));
      }
    }
    run.addEventListener('click', trace);
    input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') trace(); });
    form.appendChild(input);
    form.appendChild(run);
    t.body.appendChild(form);
    t.body.appendChild(out);
    body.appendChild(t.card);

    // Policy (rules + tiers)
    const p = card('Policy');
    (policy.rules || []).forEach((rule) => {
      const row = el('div', 'cr-rule');
      row.appendChild(el('span', 'cr-rule-id', rule.id || '(rule)'));
      const then = rule.then || {};
      const target = then.deny
        ? 'deny'
        : then.action === 'classify'
          ? `${then.profile || ''} → classify`
          : `${then.profile || '—'} · ${then.model || '—'}`;
      row.appendChild(el('span', 'cr-rule-then', target));
      p.body.appendChild(row);
    });
    const tiers = policy.tiers || {};
    Object.keys(tiers).forEach((name) => {
      const tc = tiers[name] || {};
      kv(p.body, name, `${tc.model || '—'} (${tc.provider || '—'})`);
    });
    body.appendChild(p.card);

    // Blocklist / breaker (real persisted state)
    const b = card('Blocklist & Breaker');
    const bans = bl.manual_bans || [];
    kv(b.body, 'Manual bans', bans.length ? bans.map((x) => x.model || x).join(', ') : 'none');
    kv(b.body, 'Fallback chain', (bl.fallback_chain || []).join(' → ') || '—');
    kv(b.body, 'Breaker', bl.breaker_enabled ? 'enabled' : 'disabled');
    kv(b.body, 'Active cooldowns', formatCooldowns(bl.breaker_cooldowns));
    body.appendChild(b.card);
  }

  function onOpen() {
    const panel = ensurePanel();
    showPanel();
    load(panel);
  }

  function installRailButton() {
    const rail = document.querySelector('.rail');
    if (!rail) return false;
    if (rail.querySelector('[data-capability-router]')) return true;
    const button = el('button', 'rail-btn nav-tab has-tooltip capability-router-nav');
    button.type = 'button';
    button.setAttribute('data-capability-router', 'true');
    button.dataset.tooltip = 'Capability Router';
    button.setAttribute('aria-label', 'Capability Router');
    // Trusted hard-coded SVG, no dynamic data.
    button.innerHTML = icon;
    button.addEventListener('click', onOpen);
    const spacer = rail.querySelector('.rail-spacer');
    rail.insertBefore(button, spacer || null);
    return true;
  }

  function installSidebarButton() {
    const nav = document.querySelector('.sidebar-nav');
    if (!nav) return false;
    if (nav.querySelector('[data-capability-router]')) return true;
    const button = el('button', 'nav-tab has-tooltip has-tooltip--bottom capability-router-nav');
    button.type = 'button';
    button.setAttribute('data-capability-router', 'true');
    button.dataset.label = 'Router';
    button.dataset.tooltip = 'Capability Router';
    button.setAttribute('aria-label', 'Capability Router');
    // Trusted hard-coded SVG + literal label; no user/router data enters this
    // markup. All dynamic router values render via el()/textContent below.
    button.innerHTML = `${icon}<span class="capability-router-nav-label">Router</span>`;
    button.addEventListener('click', onOpen);
    const kanban = nav.querySelector('[data-panel="kanban"]');
    if (kanban?.nextSibling) nav.insertBefore(button, kanban.nextSibling);
    else nav.appendChild(button);
    return true;
  }

  function install() {
    const railReady = installRailButton();
    const sidebarReady = installSidebarButton();
    return railReady && sidebarReady;
  }

  function bootstrap() {
    // The WebUI shell can mount its rail/sidebar after deferred extension scripts
    // run. Observe only until both targets exist; then disconnect to avoid a
    // permanent observer on ordinary application updates.
    if (install()) return;
    const observer = new MutationObserver(() => {
      if (install()) observer.disconnect();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
  } else {
    bootstrap();
  }
})();
