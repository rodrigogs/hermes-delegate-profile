(() => {
  'use strict';

  /*
   * THESIS: Make a process-isolation router legible without making it editable.
   * OWN-WORLD: Inherit Hermes One tokens; compact diagnostic cards, not a dashboard clone.
   * STORY: An operator sees live policy, traces a Stage-0 decision, then returns to work.
   * FIRST VIEWPORT: Status and liveness lead; policy and breaker remain one tab away.
   * FORM: Existing Hermes One rail/sidebar extension, not a new application route.
   */
  // Capability Router — Hermes One extension. All data travels through the
  // consented same-origin sidecar proxy; this panel has no write path.
  const EXT_ID = 'capability-router';
  const SIDE = `/api/extensions/${EXT_ID}/sidecar`;
  const PANEL_ID = 'capability-router-panel';
  const TABS = ['status', 'policy', 'blocklist', 'compaction', 'summarizer'];
  const icon =
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="12" r="3"/><path d="M9 6h4a2 2 0 0 1 2 2v1"/><path d="M9 18h4a2 2 0 0 0 2-2v-1"/></svg>';

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  async function getJSON(path) {
    const response = await fetch(SIDE + path, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status, body });
    return body;
  }

  // Write path — mirrors getJSON but POSTs a JSON body. A non-2xx (including a
  // 409 optimistic-concurrency conflict) throws with status+body attached so
  // the caller can surface a precise message.
  async function postJSON(path, payload) {
    const response = await fetch(SIDE + path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {}),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status, body });
    return body;
  }

  // Edit staging buffer. The panel is read-only until the operator switches to
  // Edit; `plan` holds the last server-computed plan (its base_hash pins the
  // on-disk state Apply is allowed to overwrite).
  const state = { draft: null, plan: null, mode: 'read' };

  // Pure: the human-facing text for a plan result — the server diff when present,
  // otherwise a formatted preview. Exposed for the test seam.
  function formatPlanDiff(planResult) {
    if (!planResult || typeof planResult !== 'object') return '';
    if (planResult.diff) return String(planResult.diff);
    if (planResult.preview !== undefined) {
      try { return JSON.stringify(planResult.preview, null, 2); } catch (_e) { return ''; }
    }
    return '';
  }

  function formatCooldowns(cooldowns) {
    if (!Array.isArray(cooldowns) || !cooldowns.length) return 'none';
    return cooldowns.map((entry) => {
      const seconds = Math.max(0, Math.floor(Number(entry.cooldown_remaining_s) || 0));
      const remaining = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
      return `${entry.model_key || '—'} ${entry.state || '—'} ${remaining}`;
    }).join(', ');
  }

  function rollupHealth(entries) {
    const rank = { alive: 0, degraded: 1, quota_exhausted: 2, dead: 3 };
    return (Array.isArray(entries) ? entries : []).reduce((worst, entry) => {
      const state = entry && rank[entry.state] !== undefined ? entry.state : 'degraded';
      return rank[state] > rank[worst] ? state : worst;
    }, 'alive');
  }

  function showTab(root, name) {
    root.querySelectorAll('[data-tab-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.tabPanel !== name;
    });
    root.querySelectorAll('[data-tab]').forEach((tab) => {
      tab.setAttribute('aria-selected', String(tab.dataset.tab === name));
    });
  }

  function card(title, subtitle) {
    const shell = el('section', 'cr-card');
    const header = el('div', 'cr-card-head');
    const titleWrap = el('div');
    titleWrap.append(el('h3', 'cr-card-title', title));
    if (subtitle) titleWrap.append(el('p', 'cr-card-subtitle', subtitle));
    header.append(titleWrap);
    const body = el('div', 'cr-card-body');
    shell.append(header, body);
    return { shell, body };
  }

  function kv(body, key, value) {
    const row = el('div', 'cr-kv');
    row.append(el('span', 'cr-k', key), el('span', 'cr-v', value));
    body.append(row);
  }

  // Editable sibling of kv(): a labelled <input> seeded with `value`. `onInput`
  // fires with the current string on every keystroke. Returns the input node so
  // callers can read it back. Exposed for the test seam.
  function field(body, key, value, onInput) {
    const row = el('div', 'cr-kv cr-field');
    const input = el('input', 'cr-field-input');
    input.type = 'text';
    input.value = value === undefined || value === null ? '' : String(value);
    input.setAttribute('aria-label', key);
    if (typeof onInput === 'function') {
      input.addEventListener('input', () => onInput(input.value));
    }
    row.append(el('span', 'cr-k', key), input);
    body.append(row);
    return input;
  }

  // Toggle the panel between read and edit. Edit reveals the apply bar; read
  // hides it and discards any un-applied plan so a stale base_hash can't linger.
  function setMode(panel, mode) {
    state.mode = mode === 'edit' ? 'edit' : 'read';
    const label = panel.querySelector('.cr-mode');
    if (label) {
      label.textContent = state.mode === 'edit' ? 'Editing' : 'Read / Edit';
      label.setAttribute('aria-label', state.mode === 'edit'
        ? 'Edit mode; plan and apply are available'
        : 'Read-only mode; switch to edit to change policy');
    }
    const bar = panel.querySelector('.cr-apply-bar');
    if (bar) bar.hidden = state.mode !== 'edit';
    if (state.mode === 'read') { state.draft = null; state.plan = null; }
  }

  // Write gate: no POST may run unless the operator is explicitly in edit mode.
  function canWrite() {
    return state.mode === 'edit';
  }

  function empty(text) {
    return el('div', 'cr-empty', text);
  }

  function renderStatusTab(container, status, policy, lint) {
    container.textContent = '';
    const snapshot = card('Status', 'Current policy posture from the authenticated sidecar.');
    const classifier = status.classifier || {};
    const failSafe = policy.fail_safe || {};
    const validationErrors = status.validation_errors || lint.errors || [];
    kv(snapshot.body, 'Enabled', status.enabled ? 'yes' : 'no');
    kv(snapshot.body, 'Rules', status.rules_count ?? '—');
    kv(snapshot.body, 'Tiers', Array.isArray(status.tiers) ? status.tiers.join(', ') : '—');
    kv(snapshot.body, 'Classifier', `${classifier.model || '—'} (${classifier.provider || '—'})`);
    kv(snapshot.body, 'Fail-safe', `${failSafe.model || '—'} (${failSafe.provider || '—'})`);
    kv(snapshot.body, 'Config valid', status.valid && lint.valid ? 'yes' : `no (${validationErrors.length} errors)`);
    container.append(snapshot.shell);
  }

  function renderPolicyTab(container, policy, panel) {
    container.textContent = '';
    const policyCard = card('Policy / Tiers', 'Read-only policy material returned by the sidecar.');
    const rules = Array.isArray(policy.rules) ? policy.rules : [];
    if (!rules.length) policyCard.body.append(empty('No policy rules reported.'));
    rules.forEach((rule) => {
      const row = el('div', 'cr-rule');
      const then = rule.then || {};
      const target = then.deny ? 'deny' : then.action === 'classify'
        ? `${then.profile || '—'} → classify`
        : `${then.profile || '—'} · ${then.model || '—'}`;
      row.append(el('span', 'cr-rule-id', rule.id || '(rule)'), el('span', 'cr-rule-then', target));
      policyCard.body.append(row);
    });
    const tiers = policy.tiers && typeof policy.tiers === 'object' ? policy.tiers : {};
    Object.entries(tiers).forEach(([name, tier]) => kv(
      policyCard.body,
      name,
      `${tier?.model || '—'} (${tier?.provider || '—'})`,
    ));
    container.append(policyCard.shell);

    if (panel && canWrite()) renderPolicyEditor(container, policy, panel);
  }

  // The edit surface. A single JSON editor seeded with the exact policy shape
  // the sidecar returns — so bans/fallback are edited where they truly live
  // (blocklist.manual_ban / blocklist.fallback_chain), never as invented
  // top-level keys the write allowlist would silently drop. Plan → inspect the
  // server diff → Apply (hash-checked) → Revert.
  function renderPolicyEditor(container, policy, panel) {
    const editCard = card('Edit policy', 'Changes are staged, validated by the sidecar, then applied atomically.');
    const editable = {
      enabled: policy.enabled,
      default: policy.default,
      classifier: policy.classifier,
      tiers: policy.tiers,
      fail_safe: policy.fail_safe,
      rules: policy.rules,
      blocklist: policy.blocklist,
    };
    const editor = el('textarea', 'cr-editor');
    editor.value = JSON.stringify(editable, null, 2);
    editor.setAttribute('aria-label', 'Policy JSON');
    editor.addEventListener('input', () => { state.draft = editor.value; });
    state.draft = editor.value;
    const preview = el('pre', 'cr-plan-preview');
    const message = el('div', 'cr-plan-msg'); message.setAttribute('role', 'status'); message.setAttribute('aria-live', 'polite');
    const bar = el('div', 'cr-editor-actions');
    const planBtn = el('button', 'cr-button', 'Plan'); planBtn.type = 'button';
    const applyBtn = el('button', 'cr-button cr-button-primary', 'Apply'); applyBtn.type = 'button';
    const revertBtn = el('button', 'cr-button', 'Revert'); revertBtn.type = 'button';
    bar.append(planBtn, applyBtn, revertBtn);

    planBtn.addEventListener('click', () => planPolicy(panel, message, preview));
    applyBtn.addEventListener('click', () => applyPolicy(panel, message));
    revertBtn.addEventListener('click', () => revertPolicy(panel, message));

    editCard.body.append(editor, bar, message, preview);
    container.append(editCard.shell);
  }

  async function planPolicy(panel, message, preview) {
    if (!canWrite()) { message.textContent = 'Switch to Edit mode first.'; return; }
    let draft;
    try { draft = JSON.parse(state.draft); } catch (error) {
      message.textContent = `Not valid JSON: ${error.message}`; return;
    }
    message.textContent = 'Requesting server-side plan…';
    try {
      const result = await postJSON('/plan', { policy: draft });
      state.plan = result;
      preview.textContent = formatPlanDiff(result);
      message.textContent = result.valid
        ? 'Plan is valid. Inspect the diff, then Apply.'
        : `Plan is INVALID: ${(result.errors || []).join('; ')}`;
    } catch (error) {
      message.textContent = `Plan failed (HTTP ${error?.status || '?'}).`;
    }
  }

  async function applyPolicy(panel, message) {
    if (!canWrite()) { message.textContent = 'Switch to Edit mode first.'; return; }
    if (!state.plan) { message.textContent = 'Plan first, then Apply.'; return; }
    if (!state.plan.valid) { message.textContent = 'Refusing to apply an invalid plan.'; return; }
    try {
      await postJSON('/apply', { plan: state.plan, policy: state.plan.policy });
      message.textContent = 'Applied. Reloading state…';
      state.plan = null;
      await load(panel);
    } catch (error) {
      message.textContent = error?.status === 409
        ? 'Conflict: router.yaml changed since the plan. Re-plan against fresh state.'
        : `Apply failed (HTTP ${error?.status || '?'}).`;
    }
  }

  async function revertPolicy(panel, message) {
    if (!canWrite()) { message.textContent = 'Switch to Edit mode first.'; return; }
    try {
      const result = await postJSON('/apply/revert', {});
      message.textContent = result.reverted ? 'Reverted to the last snapshot. Reloading…' : 'No snapshot to revert.';
      state.plan = null;
      await load(panel);
    } catch (error) {
      message.textContent = `Revert failed (HTTP ${error?.status || '?'}).`;
    }
  }

  function renderBlocklistTab(container, blocklist) {
    container.textContent = '';
    const blockCard = card('Blocklist / Breaker', 'Manual bans and persisted breaker cooldowns.');
    const bans = Array.isArray(blocklist.manual_bans) ? blocklist.manual_bans : [];
    kv(blockCard.body, 'Manual bans', bans.length ? bans.map((ban) => ban.model || String(ban)).join(', ') : 'none');
    kv(blockCard.body, 'Fallback chain', (blocklist.fallback_chain || []).join(' → ') || '—');
    kv(blockCard.body, 'Breaker', blocklist.breaker_enabled ? 'enabled' : 'disabled');
    kv(blockCard.body, 'Active cooldowns', formatCooldowns(blocklist.breaker_cooldowns));
    container.append(blockCard.shell);
  }

  function renderLivenessTab(container, liveness) {
    const healthCard = card('Liveness', 'Worst-of policy targets; no rate-limit state is inferred.');
    const models = Array.isArray(liveness.models) ? liveness.models : [];
    if (!models.length) healthCard.body.append(empty('No liveness targets reported.'));
    models.forEach((model) => {
      const row = el('div', 'cr-live-row');
      const status = el('span', `cr-live-state cr-live-${model.state || 'degraded'}`, model.state || 'degraded');
      row.append(el('span', 'cr-live-model', model.model_key || '—'), status);
      healthCard.body.append(row);
    });
    container.append(healthCard.shell);
  }

  function renderCompactionTab(container, compaction) {
    container.textContent = '';
    const compactionCard = card('Compaction', 'Read-only calibration from the sidecar.');
    if (!compaction || !compaction.model_thresholds) {
      compactionCard.body.append(empty('Compaction telemetry unavailable.'));
    } else {
      kv(compactionCard.body, 'Aggressiveness', compaction.aggressiveness);
      kv(compactionCard.body, 'Summarizer window', compaction.summarizer_window);
      kv(compactionCard.body, 'Threshold tokens', compaction.threshold_tokens);
      kv(compactionCard.body, 'Warning', compaction.warning ? 'yes' : 'no');
      Object.entries(compaction.model_thresholds).forEach(([model, threshold]) => kv(compactionCard.body, model, threshold));
    }
    container.append(compactionCard.shell);
  }

  function renderSummarizerTab(container, compaction) {
    container.textContent = '';
    const summaryCard = card('Summarizer', 'The window and source threshold are intentionally shown in tokens.');
    if (!compaction) summaryCard.body.append(empty('Summarizer telemetry unavailable.'));
    else {
      kv(summaryCard.body, 'Window', compaction.summarizer_window);
      kv(summaryCard.body, 'Source threshold', compaction.threshold_tokens);
      kv(summaryCard.body, 'Within window', compaction.threshold_tokens <= compaction.summarizer_window ? 'yes' : 'no');
    }
    container.append(summaryCard.shell);
  }

  function renderError(panel, error) {
    const content = panel.querySelector('.cr-tab-content');
    content.textContent = '';
    const message = el('div', 'cr-error');
    message.setAttribute('role', 'alert');
    message.setAttribute('aria-live', 'assertive');
    const code = error?.status || '?';
    message.textContent = code === 403
      ? 'Sidecar proxy not consented. Approve it in Settings → Extensions, then refresh.'
      : code === 503
        ? 'Sidecar token file missing (503). Start the router-sidecar service, then refresh.'
        : `Could not reach the router sidecar (HTTP ${code}).`;
    content.append(message);
  }

  function renderChrome(panel, health, liveness) {
    const reachability = panel.querySelector('[data-reachability]');
    const rollup = panel.querySelector('[data-rollup]');
    const online = Boolean(health && health.ok);
    const worst = rollupHealth(liveness?.models);
    reachability.textContent = online ? 'sidecar reachable' : 'sidecar unreachable';
    reachability.className = `cr-chip ${online ? 'cr-chip-ok' : 'cr-chip-bad'}`;
    rollup.textContent = `worst-of-N: ${worst}`;
    rollup.className = `cr-badge cr-live-${worst}`;
  }

  function wireTrace(panel) {
    const drawer = panel.querySelector('.cr-trace-drawer');
    const output = panel.querySelector('.cr-trace-out');
    panel.querySelector('[data-trace-open]').addEventListener('click', () => { drawer.hidden = false; });
    panel.querySelector('[data-trace-close]').addEventListener('click', () => { drawer.hidden = true; });
    panel.querySelector('[data-trace-run]').addEventListener('click', async () => {
      const input = panel.querySelector('[data-trace-input]');
      const task = input.value.trim();
      output.textContent = '';
      if (!task) return;
      try {
        const result = await getJSON(`/explain?task=${encodeURIComponent(task)}`);
        const decision = result.decision || {};
        const route = decision.output || {};
        kv(output, 'Matched rule', decision.matched_rule_id || '(default)');
        kv(output, 'Cause', decision.cause || '—');
        kv(output, 'Decision', result.requires_classifier ? 'classifier needed (Stage 1)' : `${route.model || '—'} (${route.provider || '—'})`);
      } catch (error) {
        output.append(el('div', 'cr-error', `Trace failed (HTTP ${error?.status || '?'}).`));
      }
    });
  }

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    panel = el('section', 'main-view capability-router-panel');
    panel.id = PANEL_ID;
    panel.hidden = true;

    const head = el('div', 'cr-head');
    const title = el('div');
    title.append(el('p', 'cr-eyebrow', 'Hermes One / observability'), el('h2', 'cr-title', 'Capability Router'));
    const controls = el('div', 'cr-controls');
    const rollup = el('span', 'cr-badge', 'worst-of-N: checking'); rollup.dataset.rollup = 'true';
    const reachability = el('span', 'cr-chip', 'sidecar checking'); reachability.dataset.reachability = 'true';
    const trace = el('button', 'cr-button', 'Trace Route'); trace.type = 'button'; trace.dataset.traceOpen = 'true';
    const refresh = el('button', 'cr-button', 'Refresh'); refresh.type = 'button'; refresh.addEventListener('click', () => load(panel));
    const mode = el('button', 'cr-mode', 'Read / Edit'); mode.type = 'button';
    mode.setAttribute('aria-label', 'Read-only mode; switch to edit to change policy');
    mode.addEventListener('click', () => {
      setMode(panel, state.mode === 'edit' ? 'read' : 'edit');
      showTab(panel, 'policy');
      load(panel);
    });
    controls.append(rollup, reachability, trace, mode, refresh);
    head.append(title, controls);

    const tabs = el('div', 'cr-tabs'); tabs.setAttribute('role', 'tablist');
    TABS.forEach((name) => {
      const tab = el('button', 'cr-tab', name === 'policy' ? 'Policy / Tiers' : name === 'blocklist' ? 'Blocklist / Breaker' : name[0].toUpperCase() + name.slice(1));
      tab.type = 'button'; tab.dataset.tab = name; tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(name === 'status'));
      tab.addEventListener('click', () => showTab(panel, name));
      tabs.append(tab);
    });
    const body = el('div', 'cr-body');
    TABS.forEach((name) => {
      const section = el('section', 'cr-tab-content'); section.dataset.tabPanel = name; section.hidden = name !== 'status'; body.append(section);
    });
    const applyBar = el('div', 'cr-apply-bar'); applyBar.hidden = true; applyBar.textContent = 'No write action is available.';
    const drawer = el('aside', 'cr-trace-drawer'); drawer.hidden = true;
    drawer.append(el('h3', 'cr-drawer-title', 'Trace route'));
    const close = el('button', 'cr-button', 'Close'); close.type = 'button'; close.dataset.traceClose = 'true'; drawer.append(close);
    const input = el('input', 'cr-trace-input'); input.dataset.traceInput = 'true'; input.placeholder = 'Describe a task…'; drawer.append(input);
    const run = el('button', 'cr-button cr-button-primary', 'Trace'); run.type = 'button'; run.dataset.traceRun = 'true'; drawer.append(run);
    const output = el('div', 'cr-trace-out'); drawer.append(output);
    panel.append(head, tabs, body, applyBar, drawer);
    document.querySelector('main')?.append(panel);
    wireTrace(panel);
    return panel;
  }

  async function load(panel) {
    const content = panel.querySelector('.cr-tab-content');
    content.textContent = '';
    content.append(el('div', 'cr-loading', 'Loading router state…'));
    try {
      const [health, status, policy, blocklist, lint, liveness, compaction] = await Promise.all([
        getJSON('/health'), getJSON('/status'), getJSON('/policy'), getJSON('/blocklist'), getJSON('/lint'), getJSON('/liveness'), getJSON('/compaction?aggr=50'),
      ]);
      renderChrome(panel, health, liveness);
      renderStatusTab(panel.querySelector('[data-tab-panel="status"]'), status, policy, lint);
      // Compose the full editable shape: /policy omits enabled/classifier/
      // blocklist, so graft them from the status + blocklist reads. bans and
      // fallback stay nested under blocklist — never invented top-level keys.
      const editablePolicy = Object.assign({}, policy, {
        enabled: status.enabled,
        classifier: status.classifier,
        blocklist: {
          manual_ban: blocklist.manual_bans || [],
          fallback_chain: blocklist.fallback_chain || [],
        },
      });
      renderPolicyTab(panel.querySelector('[data-tab-panel="policy"]'), editablePolicy, panel);
      renderBlocklistTab(panel.querySelector('[data-tab-panel="blocklist"]'), blocklist);
      renderLivenessTab(panel.querySelector('[data-tab-panel="status"]'), liveness);
      renderCompactionTab(panel.querySelector('[data-tab-panel="compaction"]'), compaction);
      renderSummarizerTab(panel.querySelector('[data-tab-panel="summarizer"]'), compaction);
    } catch (error) {
      renderChrome(panel, null, { models: [{ state: 'dead' }] });
      renderError(panel, error);
    }
  }

  function showPanel() {
    document.querySelectorAll('main > .main-view').forEach((view) => { view.hidden = view.id !== PANEL_ID; });
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
    button.type = 'button'; button.dataset.capabilityRouter = 'true'; button.dataset.tooltip = 'Capability Router';
    button.setAttribute('aria-label', 'Capability Router');
    button.innerHTML = icon; // Trusted static icon only.
    button.addEventListener('click', onOpen);
    rail.insertBefore(button, rail.querySelector('.rail-spacer') || null);
    return true;
  }

  function installSidebarButton() {
    const nav = document.querySelector('.sidebar-nav');
    if (!nav) return false;
    if (nav.querySelector('[data-capability-router]')) return true;
    const button = el('button', 'nav-tab has-tooltip has-tooltip--bottom capability-router-nav');
    button.type = 'button'; button.dataset.capabilityRouter = 'true'; button.dataset.label = 'Router'; button.dataset.tooltip = 'Capability Router';
    button.setAttribute('aria-label', 'Capability Router');
    button.innerHTML = `${icon}<span class="capability-router-nav-label">Router</span>`; // Trusted static markup only.
    button.addEventListener('click', onOpen);
    const kanban = nav.querySelector('[data-panel="kanban"]');
    if (kanban?.nextSibling) nav.insertBefore(button, kanban.nextSibling); else nav.append(button);
    return true;
  }

  function bootstrap() {
    if (installRailButton() && installSidebarButton()) return;
    const observer = new MutationObserver(() => { if (installRailButton() && installSidebarButton()) observer.disconnect(); });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
  else bootstrap();
})();
