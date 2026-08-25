// Behavioural tests for the console's decision logic — the part a static scan
// cannot check. The console is one self-contained IIFE that publishes its
// internals on globalThis.__router, so we run it in a VM over a DOM stub.
//
// Each assertion pins a rule that would otherwise rot silently: which node a
// replay step lights, that a health rollup reports the WORST target rather than
// the first, that values are translated instead of dumped, that the graph grows
// into the space it is given, and that writing stays behind the lock.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

// The VM context stubs setTimeout to a no-op so the console's own timers do not
// fire during tests; the concurrency test needs a real one to interleave with.
const setTimeoutReal = setTimeout;

const sourcePath = 'webui_extension/hermes-one-capability-router/console.html';

// A DOM stub good enough for the console's init path.
function fakeDom() {
  const nodes = new Map();
  const make = (id) => {
    const node = {
      id, className: '', textContent: '', value: '', title: '',
      hidden: false, readOnly: false, max: '0',
      style: {}, dataset: {}, attrs: {}, children: [],
      // Listeners are recorded so a test can dispatch the exact click a user
      // would — a button that exists but does nothing is the defect under test.
      _listeners: {},
      classList: {
        _set: new Set(),
        add(c) { this._set.add(c); },
        remove(c) { this._set.delete(c); },
        toggle(c, on) { if (on === undefined) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); } else if (on) this._set.add(c); else this._set.delete(c); },
        contains(c) { return this._set.has(c); },
      },
      append(...k) { node.children.push(...k); },
      appendChild(k) { node.children.push(k); return k; },
      removeChild(k) { node.children = node.children.filter((x) => x !== k); },
      addEventListener(type, fn) { node._listeners[type] = fn; },
      setAttribute(n, v) { node.attrs[n] = String(v); },
      getAttribute(n) { return node.attrs[n]; },
      querySelector(sel) { return get(`${id}${sel}`); },
      querySelectorAll() { return []; },
      getBoundingClientRect() { return { width: 900, height: 300, left: 0, right: 900 }; },
      clientWidth: 900,
      scrollIntoView(opts) { node._scrolledTo = opts || null; },
    };
    Object.defineProperty(node, 'firstChild', { get: () => node.children[0] || null });
    return node;
  };
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, make(id)); return nodes.get(id); };
  return {
    nodes,
    get,
    document: {
      documentElement: make('html'),
      getElementById: get,
      createElement: (tag) => Object.assign(make(`el:${tag}`), { tagName: tag }),
      createElementNS: (_ns, tag) => Object.assign(make(`svg:${tag}`), { tagName: tag }),
      querySelector: (sel) => get(`sel:${sel}`),
      querySelectorAll: () => [],
      addEventListener() {},
      readyState: 'complete',
    },
  };
}

function loadConsole({ width = 1440, embedded = false, csrfToken, fetch: fetchStub } = {}) {
  const html = fs.readFileSync(sourcePath, 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
    // Skip the init calls that need a live browser; keep everything else intact.
    .replace(/\n      wire\(\);[\s\S]*?load\(\);\n/, '\n');
  const dom = fakeDom();
  const top = {};
  const win = { innerWidth: width, addEventListener() {}, top };
  win.self = embedded ? win : top;
  if (csrfToken !== undefined) win.__HERMES_CONFIG__ = { csrfToken };
  const context = {
    console, window: win, document: dom.document, globalThis: {},
    fetch: fetchStub || (() => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') })),
    setTimeout() {}, Math, JSON, Number, Object, Array, String, Set, Map, Date, encodeURIComponent,
  };
  vm.runInNewContext(script, context, { filename: sourcePath });
  return { api: context.globalThis.__router, dom };
}

test('a replay step lights the node that actually made the decision', () => {
  const { api } = loadConsole();
  // A rules step names the rule that fired; replay must highlight THAT rule, or
  // the trace misrepresents which row matched.
  assert.equal(api.stepNode({ stage: 'rules', out: { rule_id: 'hard-verbs' } }), 'rule:hard-verbs');
  assert.equal(api.stepNode({ stage: 'rules', out: {} }), 'default');
  // A veto is its own stage but belongs to the blocklist node.
  assert.equal(api.stepNode({ stage: 'veto' }), 'blocklist');
  assert.equal(api.stepNode({ stage: 'classifier' }), 'classifier');
});

test('health reports the worst model, never the first or an average', () => {
  const { api } = loadConsole();
  const worst = (...states) => api.worstOf(states.map((state) => ({ state })));
  assert.equal(worst('alive', 'alive'), 'alive');
  assert.equal(worst('alive', 'degraded', 'alive'), 'degraded');
  assert.equal(worst('degraded', 'quota_exhausted'), 'quota');
  assert.equal(worst('quota_exhausted', 'dead'), 'dead');
  // Order must not change the verdict.
  assert.equal(worst('dead', 'alive'), worst('alive', 'dead'));
});

test('router states map to the five operator meanings', () => {
  const { api } = loadConsole();
  assert.equal(api.modelState({ state: 'OPEN' }), '');            // unknown breaker word → no claim
  assert.equal(api.modelState({ state: 'alive' }), 'alive');
  assert.equal(api.modelState({ state: 'HALF_OPEN' }), 'degraded');
  assert.equal(api.modelState({ state: 'quota_exhausted' }), 'quota');
  assert.equal(api.modelState({ state: 'dead' }), 'dead');
});

test('a refusal and a routine decision never read the same', () => {
  const { api } = loadConsole();
  const red = api.causeColor('blocklist_veto');
  assert.equal(api.causeColor('fail_safe_strong'), red, 'both refusals share one alarm colour');
  assert.notEqual(api.causeColor('hard_rule'), red);
  assert.notEqual(api.causeColor('classifier'), api.causeColor('hard_rule'),
    'inferred and deterministic causes stay distinguishable');
});

test('values are translated for an operator, not dumped', () => {
  const { api } = loadConsole();
  // DESIGN.md §2.5: `true` is not a metric.
  assert.equal(api.say(true), 'yes');
  assert.equal(api.say(false), 'no');
  assert.equal(api.say(''), '—');
  assert.equal(api.say(null), '—');
  assert.equal(api.say(['a', 'b']), '2', 'a list reports its size, not its JSON');
  // Timestamps become elapsed time; an operator cares how stale, not the epoch.
  const now = Math.floor(Date.now() / 1000);
  assert.match(api.ago(now - 5), /^\d+s ago$/);
  assert.match(api.ago(now - 600), /^\d+m ago$/);
  assert.equal(api.ago(null), '—');
});

test('replay renders the recorded path, not a map of the whole policy', () => {
  const { api, dom } = loadConsole();
  // The console once drew all fifteen policy nodes to highlight the three a
  // trace actually walked, which made the reader hunt for the answer. A trace
  // is a short sequence, so replay must draw exactly that sequence.
  api.state.replay = {
    id: 't1', at: 1,
    steps: [
      { stage: 'blocklist', out: { blocked: false } },
      { stage: 'rules', cause: 'hard_rule', out: { rule_id: 'hard-verbs' } },
      { stage: 'classifier', out: { model: 'strong' } },
    ],
  };
  api.drawPath();
  const drawn = dom.get('replayPath').children;
  assert.equal(drawn.length, 3, 'one line per recorded step — no policy nodes the trace never touched');
});

// ── replay answers "what did the router actually do" ──────────────────────
// One recorded decision exactly as GET /routes?id= answers it: one_sidecar returns
// service.route's result, which is the trace entry the decision log WROTE. So the
// reply carries `steps`, the `output` the executor was handed — including
// `attempted_model`, which decision_log.record copies off `chain_plan.chain[0]`
// because `output.model` stays the DECLARED tier primary — and `chain_plan` itself
// for anything recorded since plans were persisted.
//
// 03:20 UTC on the Monday: deliberately NOT the hour the console is read at, because
// a decision's prices belong to the hour it happened.
const TRACE_AT = Date.UTC(2026, 7, 17, 3, 20) / 1000;
function tracedDecision(extra) {
  const plan = chainPlan(Object.assign({
    requirements: { vision: true },
    chain: [
      { model: 'gpt-5.6-luna', provider: 'openai-codex', billing_mode: 'subscription' },
      { model: 'mimo-v2.5', provider: 'xiaomi', billing_mode: 'metered' },
    ],
    rejected: [{ model: 'glm-5.3', provider: 'zai', reject_reason: 'no_vision' }],
    utc_hour: 3, utc_weekday: 0, time_agnostic: false,
  }, extra || {}));
  return {
    ts: TRACE_AT,
    cause: 'keyword_match',
    rule_id: 'image-attached',
    task: 'describe this screenshot',
    output: {
      model: 'glm-5.3', provider: 'zai',
      attempted_model: plan.chain[0].model,
      attempted_provider: plan.chain[0].provider,
    },
    steps: [
      { stage: 'blocklist', out: { blocked: false } },
      { stage: 'signals', out: { needs_vision: true } },
      { stage: 'rules', cause: 'keyword_match', out: { rule_id: 'image-attached', tier: 'T2' } },
    ],
    chain_plan: plan,
  };
}

test('replay renders the chain plan the router persisted with the decision', async () => {
  // `<div id="replayPlan">` shipped with a comment promising the plan the router
  // persisted and nothing ever filled it: the one surface that answers "what did it
  // actually do" showed a four-step path and two blocks of JSON, while the model the
  // executor really dispatched was in the reply the whole time.
  const entry = tracedDecision();
  const { api, dom } = loadConsole({
    fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(entry)) }),
  });
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;   // 07:14 UTC — the hour it is READ at, four hours later
  api.state.routes = [{
    id: 'r1', cause: entry.cause, model: entry.output.model, task: entry.task, ts: entry.ts,
  }];
  await api.pickRoute('r1');

  const box = dom.get('replayPlan');
  const text = flat(box);
  assert.notEqual(text, '', 'the element existed and nothing ever filled it');
  // The trace itself is untouched: the plan is an addition, not a replacement.
  assert.equal(dom.get('replayPath').children.length, entry.steps.length);

  // AGREEMENT, and it is the whole point of rendering this at all: hop 1 of the panel
  // is the head the executor dispatched, which is the one the log recorded on the
  // entry — never output.model, the declared primary the row on the left shows.
  const hops = findAll(findAll(box, 'hops')[0], 'hop-model').map((n) => n.textContent);
  assert.deepEqual(hops, entry.chain_plan.chain.map((hop) => hop.model));
  assert.equal(hops[0], entry.output.attempted_model);
  assert.notEqual(entry.output.attempted_model, entry.output.model,
    'the fixture is only worth asserting on because the two differ');

  // What it dropped and why, in the words the Explain panel uses for the same reason.
  assert.match(text, /glm-5\.3/);
  assert.match(text, /não lê imagem/);

  // THE HOUR IS THE DECISION'S. Reading a 03:00 decision at 07:14 must not price it
  // at 07:00: every multiplier under this panel is the one the router planned with.
  assert.match(text, /03:00 UTC/);
  assert.doesNotMatch(text, /07:00 UTC/, 'the console\'s own hour is not this decision\'s');

  // ONE PRESENTATION, NOT TWO. The same plan through the Explain panel yields the
  // same chain and the same rejections — same classes, same words — because both
  // surfaces go through renderChainPlan.
  api.renderChainPlan(entry.chain_plan);
  const shape = (id) => findAll(dom.get(id), 'hop-model').map((n) => n.textContent).join(' ')
    + ' | ' + findAll(dom.get(id), 'reject-why').map((n) => n.textContent).join(' ');
  assert.equal(shape('replayPlan'), shape('chainPlan'),
    'a second chain vocabulary is a second answer to the same question');

  // A decision recorded before plans were persisted carries none, and then the panel
  // is ABSENT rather than a framed void (DESIGN.md §2.1).
  api.state.replay.plan = null;
  api.renderStep();
  assert.equal(dom.get('replayPlan').children.length, 0);
});

test('a replayed plan with no hour of its own is priced at the hour it was recorded', async () => {
  // The raw entry is whatever was written to disk, so `utc_hour` can be absent —
  // and then the fallback must be the trace's own timestamp. Falling back to the
  // BROWSER's hour would put this morning's multipliers on last night's decision,
  // which is the same class of error as reading the clock inside a rule.
  const entry = tracedDecision();
  delete entry.chain_plan.utc_hour;
  delete entry.chain_plan.utc_weekday;
  delete entry.chain_plan.time_agnostic;
  const { api, dom } = loadConsole({
    fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(entry)) }),
  });
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;
  api.state.routes = [{ id: 'r1', cause: entry.cause, model: entry.output.model, task: entry.task, ts: entry.ts }];
  await api.pickRoute('r1');

  const text = flat(dom.get('replayPlan'));
  assert.match(text, /preço às 03:00 UTC, a hora em que esta decisão foi gravada/,
    'the hour is named AND its source, because the clock line above reports now');
  assert.doesNotMatch(text, /planned at/, 'the router reported no hour, so nothing may claim it did');
  assert.doesNotMatch(text, /07:00 UTC/);
});

test('a step says what it concluded, so the JSON is optional', () => {
  const { api } = loadConsole();
  assert.equal(api.stepOutcome({ out: { rule_id: 'hard-verbs' } }), 'hard-verbs');
  assert.equal(api.stepOutcome({ out: { deny: true } }), 'recusou');
  assert.equal(api.stepOutcome({ out: { blocked: false } }), 'liberado');
  assert.equal(api.stepOutcome({ out: { model: 'gpt-5.6-terra' } }), 'gpt-5.6-terra');
  // Nothing to report must render nothing, never a placeholder.
  assert.equal(api.stepOutcome({ out: {} }), '');
  assert.equal(api.stepOutcome({}), '');
});

test('the rail carries each destination\'s live state', () => {
  const { api, dom } = loadConsole();
  api.state.policy = { rules: [{ id: 'a' }, { id: 'b' }, { id: 'c' }], tiers: { T1: {} } };
  api.state.routes = [{ id: '1' }, { id: '2' }];
  api.state.liveness = { models: [{ state: 'alive' }, { state: 'degraded' }] };
  api.renderRail();

  // The Pipeline count is GONE: the sheet's numbered rule list is its own counter.
  assert.equal(dom.get('countPipeline').hidden, true,
    'pipeline shows no count — the numbered list is the counter');
  assert.equal(dom.get('countRoutes').textContent, '2', 'routes counts recorded decisions');
  // The Health badge counts EXCEPTIONS, not elos: two models with no bans or
  // breaker cooldowns show nothing, not "2".
  assert.equal(dom.get('countHealth').hidden, true,
    'no exceptions → no health count, however many elos');
  // One degraded target must surface, not be averaged into "fine".
  assert.match(dom.get('stateHealth').className, /is-degraded/);
});

test('the health badge counts bans and breaker cooldowns, in amber', () => {
  const { api, dom } = loadConsole();
  api.state.policy = { rules: [] };
  api.state.liveness = { models: [{ state: 'alive' }, { state: 'alive' }, { state: 'alive' }] };
  api.state.blocklist = {
    manual_bans: [{ model: 'glm-5.3' }],
    breaker_cooldowns: [{ model_key: 'deepseek-v4-pro', cooldown_remaining_s: 300 }],
  };
  api.renderRail();
  // The badge is bans + breakers, NOT elos — the review's 8→1 was the badge
  // counting inventory and the inventory shrinking at the moment of the problem.
  assert.equal(dom.get('countHealth').textContent, '2', 'bans + breakers, not elos');
  assert.equal(dom.get('countHealth').hidden, false);
  assert.equal(dom.get('countHealth').classList.contains('is-warn'), true,
    'an exception count wears amber, the attention colour');

  // Exceptions cleared → hidden again (zero is not drawn, §2.1).
  api.state.blocklist = { manual_bans: [], breaker_cooldowns: [] };
  api.renderRail();
  assert.equal(dom.get('countHealth').hidden, true);
  assert.equal(dom.get('countHealth').classList.contains('is-warn'), false);
});

test('the rail survives being rendered before any data arrives', () => {
  const { api, dom } = loadConsole();
  // setMode() renders the rail at init, before the first poll. An unguarded read
  // here kills the whole IIFE and the operator gets a blank page.
  assert.doesNotThrow(() => api.renderRail());
  assert.equal(dom.get('countPipeline').hidden, true, 'no policy yet → no count shown');
});

test('the header reports three ages, and a stale sidecar says so', () => {
  const { api, dom } = loadConsole();
  // The console's own clock is injectable through state.clock; the provenance
  // ages are read relative to it, so the test pins the header without racing now.
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.unreachable = false;
  api.state.status = {
    process_started_at: new Date(T - 2 * 3600 * 1000).toISOString(),
    code_mtime: new Date(T - 1 * 3600 * 1000).toISOString(),
    config_mtime: new Date(T - 5 * 60 * 1000).toISOString(),
  };
  api.renderRail();

  const text = dom.get('reachText').textContent;
  assert.match(text, /serviço no ar há 2h/);
  assert.match(text, /código carregado há 1h/);
  assert.match(text, /arquivo mudou há 5m/);
  assert.doesNotMatch(text, /checked/, 'the single checked clock is gone');

  // Code (T-1h) is newer than the process (T-2h): the ROUTER banner must say so
  // and carry the exact restart command.
  assert.equal(dom.get('staleBanner').hidden, false);
  assert.match(dom.get('reach').className, /is-stale/);
  const banner = flat(dom.get('staleBanner'));
  assert.match(banner, /1 dia atrás/);
  assert.match(banner, /o que você vê pode não ser o que roda/);
  assert.match(banner, /systemctl --user restart hermes-router-sidecar/);
});

test('a fresh sidecar shows no stale banner; checking and dead keep their words', () => {
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.unreachable = false;
  api.state.status = {
    process_started_at: new Date(T - 2 * 3600 * 1000).toISOString(),
    code_mtime: new Date(T - 3 * 3600 * 1000).toISOString(),  // older than the process
    config_mtime: new Date(T - 5 * 60 * 1000).toISOString(),
  };
  api.renderRail();
  assert.equal(dom.get('staleBanner').hidden, true);
  assert.doesNotMatch(dom.get('reach').className, /is-stale/);

  // Before the first status the console is still checking; a dead sidecar names
  // the failure — neither renders three dashes as if the ages existed.
  api.state.status = undefined;
  api.renderRail();
  assert.equal(dom.get('reachText').textContent, 'Ainda não li nada do roteador.');
  api.state.status = { process_started_at: 'x' };
  api.state.unreachable = true;
  api.renderRail();
  assert.equal(dom.get('reachText').textContent, 'Não consegui falar com o roteador.');
});

// This replaces a test asserting that "the lock is the single authority on whether
// writing is possible". That claim was false and the test was enforcing it: the
// sidecar has never heard of the console's mode, and it already requires a
// per-extension token-v1 secret, the host's CSRF token, a loopback bind and a
// matching base_hash — with confirm=="COMPACT" checked server-side for compaction.
// A client-side gate is the one an attacker skips and the operator cannot, so it
// was costing a click on every write path and buying nothing.
//
// What the mode really is: the read-only DEFAULT that stops a stray tap on the
// Pipeline from editing the live routing policy. That is what is pinned here.
test('the console opens read-only, so a stray tap cannot change routing', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  // Not merely the initial value of a variable — the editor must actually be
  // read-only after the console has finished setting itself up.
  api.setMode('reading');
  assert.equal(dom.get('policyEditor').readOnly, true);
  assert.equal(dom.get('editMode').attrs['aria-pressed'], 'false');
});

test('the edit control says what it will do, not what state it is in', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const button = dom.get('editMode');

  api.setMode('reading');
  assert.equal(dom.get('editLabel').textContent, 'Edit',
    'reading mode offers the next action');
  assert.match(button.title, /Editar a política de roteamento/,
    'and the title says what gets edited');

  api.setMode('editing');
  assert.equal(dom.get('editLabel').textContent, 'Done');
  assert.equal(button.attrs['aria-pressed'], 'true', 'a mode toggle reports pressed');
  assert.equal(dom.get('policyEditor').readOnly, false, 'editing arms the editor');
});

test('closing the editor is not a write permission', () => {
  // The mode must not gate writes. An apply that is otherwise valid has to be
  // possible without the operator first arming a UI toggle, because the toggle
  // never protected anything.
  const { api } = loadConsole({ csrfToken: 'tok' });
  api.setMode('reading');
  const msg = { textContent: '', className: '' };
  assert.equal(api.writable(msg, 'Apply'), true,
    'the read-only mode is a default, not a lock');
  assert.equal(msg.textContent, '', 'and it produces no refusal message');
});

test('editing does not report the router as degraded', () => {
  // The Pipeline dot used to go amber whenever the editor was open, so the console
  // claimed a machine problem because someone had clicked a button — and amber is
  // the colour that means "this needs your attention".
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = { rules: [{ id: 'r1' }] };
  api.state.status = { validation_errors: [] };

  // renderRail assigns className outright rather than touching classList, so the
  // string is what has to be read — asserting through the stub's classList would
  // silently pass no matter what the console did.
  const pipelineState = () => dom.get('statePipeline').className;

  api.setMode('editing');
  api.renderRail();
  assert.doesNotMatch(pipelineState(), /is-degraded/,
    `an open editor is not a degradation, got "${pipelineState()}"`);
  assert.match(pipelineState(), /is-alive/, 'a valid policy is alive while being edited');

  // A policy the router cannot parse IS one, and must still be reported.
  api.state.status = { validation_errors: ['rule r1: unknown field'] };
  api.renderRail();
  assert.match(pipelineState(), /is-degraded/, 'an invalid policy must still show amber');
});

test('without a session write token, editing is refused with the reason', () => {
  // Measured against the live proxy: unsafe methods without X-Hermes-CSRF-Token
  // come back 403 "Session expired". The token exists only on pages the WebUI
  // renders, so a standalone console must say where editing does work.
  const { api } = loadConsole({ csrfToken: '' });
  api.setMode('unlocked');
  const msg = { textContent: '', className: '' };
  assert.equal(api.writable(msg, 'Apply'), false);
  assert.match(msg.textContent, /standalone|Hermes One/i);
});

test('writes carry the session token; reads do not need it', async () => {
  const calls = [];
  const { api } = loadConsole({
    csrfToken: 'tok-42',
    fetch: (url, opts) => { calls.push({ url, opts }); return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') }); },
  });
  await api.call('/plan', { method: 'POST', body: { policy: {} } });
  assert.equal(calls[0].opts.headers['X-Hermes-CSRF-Token'], 'tok-42');
  calls.length = 0;
  await api.call('/status');
  assert.ok(!calls[0].opts.headers, 'a read sends no write headers at all');
});

test('a host that owns the left edge gets the horizontal layout', () => {
  // Embedded in the Hermes One panel there is already a rail and a sidebar; a
  // third vertical navigation is the clutter this rule prevents.
  assert.equal(loadConsole({ width: 1440 }).api.isNarrow(), false);
  assert.equal(loadConsole({ width: 1000 }).api.isNarrow(), true);
  assert.equal(loadConsole({ width: 1600, embedded: true }).api.isNarrow(), true,
    'being framed forces the horizontal layout at any width');
});

test('an empty screen distinguishes "not asked yet" from "genuinely nothing"', () => {
  const { api } = loadConsole();
  // Before the first response, claiming "no models" is a guess presented as a
  // fact — the operator cannot tell a healthy-but-empty router from a broken one.
  assert.equal(api.state.loading, true, 'the console starts out not knowing');
  assert.equal(api.absence('Nenhum modelo roteável informado.'), 'Loading…');

  api.state.loading = false;
  api.state.unreachable = true;
  assert.match(api.absence('Nenhum modelo roteável informado.'), /não for possível falar com o roteador/);

  api.state.unreachable = false;
  assert.equal(api.absence('Nenhum modelo roteável informado.'), 'Nenhum modelo roteável informado.');
});

test('filtering decisions searches what an operator actually remembers', () => {
  const { api } = loadConsole();
  const route = { task: 'Rename getCwd in src/utils.py', model: 'glm-5.2-fast', cause: 'has_code_rule', rule_id: 'trivial-mechanical-edit' };
  // Any of the four recalled facts must find it, case-insensitively.
  for (const q of ['rename', 'GLM-5.2', 'has_code', 'trivial']) {
    assert.ok(api.matchesQuery(route, q.toLowerCase()), `"${q}" should find the decision`);
  }
  assert.equal(api.matchesQuery(route, 'classifier'), false, 'an unrelated term must not match');
  assert.ok(api.matchesQuery(route, ''), 'an empty filter hides nothing');
  // A trace with missing fields must not throw the whole list away.
  assert.doesNotThrow(() => api.matchesQuery({}, 'x'));
});

// The write spine. These four assertions used to live in
// tests/test_router_nav_writespine.js against an equivalent path in
// router-nav.js; that file is now only a mount, so they move here rather than
// disappear. This is the only code in the console that changes how the agent
// routes real work, so every refusal it makes has to be exact.
// ── the write spine ──────────────────────────────────────────────────────
//
// The only code here that changes how the agent routes real work, so every
// refusal it makes has to be exact.
//
// These replace four tests written against a two-step ritual: Validate, read
// "Valid — apply to write it", then Apply. /plan IS required — it returns the
// base_hash /apply refuses to write without (router/service.py:357) — but that is
// the machine's bookkeeping, not a decision, and one of the four old tests
// asserted the ritual itself ("an apply is refused unless a valid plan was
// validated first"). Apply now plans for itself; what must still hold is that an
// invalid draft never reaches /apply.

test('applying plans first, so an invalid draft never reaches the write', async () => {
  const posted = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push(url);
      // /plan says no; /apply must never be called.
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: false, errors: ['fail_safe missing'] })),
      });
    },
  });
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { rules: [] });

  assert.equal(posted.length, 1, 'exactly one request: the plan');
  assert.match(posted[0], /\/plan$/);
  assert.match(msg.textContent, /fail_safe missing/, 'the reason comes from the plan');
  assert.match(msg.className, /bad/);
});

test('a valid draft is planned and written in one action', async () => {
  const posted = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      posted.push(url);
      const body = url.endsWith('/plan')
        ? { valid: true, policy: { rules: [] }, base_hash: 'abc' }
        : { ok: true };
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) });
    },
  });
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { rules: [] });

  // The operator pressed one button; the console did the bookkeeping. A refresh
  // follows the write (load() re-reads every screen), so assert the ORDER of the
  // two that matter rather than the whole traffic — pinning the full list would
  // make this test fail the next time a screen is added.
  const paths = posted.map((u) => u.replace(/^.*sidecar/, ''));
  assert.equal(paths[0], '/plan', 'the plan comes first, unasked');
  assert.equal(paths[1], '/apply', 'and the write follows it immediately');
  assert.match(msg.textContent, /Written/);
});

test('a conflict tells the operator to try again instead of overwriting', async () => {
  // 409 means the policy on disk changed after the plan was computed. Writing
  // anyway would commit a decision based on a policy that no longer exists.
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => url.endsWith('/plan')
      ? Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, base_hash: 'stale' })) })
      : Promise.resolve({ ok: false, status: 409, text: () => Promise.resolve('{}') }),
  });
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, {});
  assert.match(msg.textContent, /mudou por fora/);
  assert.match(msg.className, /bad/);
  assert.equal(api.state.plan, null, 'the stale plan must not survive to be applied again');
});

test('nothing to apply is said, not silently ignored', async () => {
  let called = 0;
  const { api } = loadConsole({ csrfToken: 'tok', fetch: () => { called += 1; return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') }); } });
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg);
  assert.match(msg.textContent, /Não há o que salvar/);
  assert.equal(called, 0, 'a click with no draft must not reach the network');
});

test('a rejected plan reports the status instead of claiming success', async () => {
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: () => Promise.resolve({ ok: false, status: 400, text: () => Promise.resolve('{}') }),
  });
  const msg = { textContent: '', className: '' };
  const diff = { hidden: true, textContent: '' };
  await api.doPreview({}, msg, diff);
  // This is the bug the compaction screen shipped with: a 400 rendered as a bare
  // "Refused" with no code, so there was nothing to act on.
  assert.match(msg.textContent, /400/);
  assert.match(msg.className, /bad/);
  assert.equal(diff.hidden, true, 'no diff is shown for a plan that was never made');
});

test('a missing endpoint is distinguished from a rejected write', async () => {
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: () => Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve('{}') }),
  });
  const msg = { textContent: '', className: '' };
  await api.doPreview({}, msg, null);
  // "This sidecar cannot do that" and "this sidecar refused that" call for
  // different fixes: upgrade versus correct the input.
  assert.match(msg.textContent, /não sabe simular uma gravação/);
});

test('preview shows the diff without writing anything', async () => {
  const posted = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      posted.push(url);
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '- old\n+ new' })),
      });
    },
  });
  const msg = { textContent: '', className: '' };
  const diff = { hidden: true, textContent: '' };
  await api.doPreview({ rules: [] }, msg, diff);

  assert.equal(posted.length, 1, 'preview must not write');
  assert.match(posted[0], /\/plan$/);
  assert.equal(diff.hidden, false);
  assert.match(diff.textContent, /\+ new/, 'the diff is the point of previewing');
  assert.match(msg.className, /ok/);
});

// This behaviour used to be pinned by tests/test_router_nav_cooldowns.js, which
// tested a formatCooldowns() in router-nav.js. That file is now a mount and the
// rendering lives here, so the test moved with the code rather than being
// dropped: a breaker cooldown is the difference between "this model is gone" and
// "this model is back in 30 seconds", and an operator who cannot tell those
// apart reaches for the wrong fix.
test('a model on a breaker cooldown says how long it will be out', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.liveness = { models: [
    { model: 'glm-5.2', provider: 'zai', state: 'HALF_OPEN', breaker: { cooldown_remaining_s: 12.3 } },
    { model: 'deepseek-v3.2', provider: 'deepseek', state: 'alive' },
  ] };
  api.renderHealth();
  const rows = dom.get('models').children;
  const text = (row) => JSON.stringify(row).match(/"textContent":"[^"]*"/g).join(' ');

  // Rounded UP: reporting "12s" for 12.3 tells the operator it is over before it is.
  assert.match(text(rows[0]), /13s de bloqueio automático/);
  assert.match(text(rows[0]), /instável/, 'a cooling model is degraded, not dead');
  // A healthy model must not grow a phantom timer.
  assert.doesNotMatch(text(rows[1]), /cooldown/);

  // An expired or absent cooldown is not rendered as "0s", which reads as a
  // live countdown that never moves.
  api.state.liveness = { models: [{ model: 'x', state: 'alive', breaker: { cooldown_remaining_s: 0 } }] };
  api.renderHealth();
  assert.doesNotMatch(text(dom.get('models').children[0]), /cooldown/);
});

test('recorded decisions are reachable by keyboard, not only by mouse', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [{ id: 'r1', cause: 'hard_rule', model: 'strong', task: 'Debug it', ts: 1 }];
  api.renderRoutes();
  const row = dom.get('routesTable').children[0];
  // A div cannot be focused or activated with a keyboard; choosing a trace is an
  // action and must be a real control.
  assert.equal(row.tagName, 'button');
  assert.equal(row.type, 'button', 'type=button, so it never submits a surrounding form');
});

test('the model count never claims health the console cannot know', () => {
  // state.liveness holds the LAST successful read, so when the sidecar stops
  // answering the summary would keep asserting "all 5 reachable" over a dead
  // measurement — at exactly the moment an operator most needs to distrust it.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.liveness = { models: [
    { model: 'a', state: 'alive' }, { model: 'b', state: 'alive' },
  ] };

  api.renderHealth();
  assert.match(dom.get('modelsNote').textContent, /todos os 2 alcançáveis/);

  api.state.unreachable = true;
  api.renderHealth();
  assert.doesNotMatch(dom.get('modelsNote').textContent, /reachable/,
    'a dead sidecar must not yield a reachability claim');
  assert.match(dom.get('modelsNote').textContent, /último valor conhecido/);
});

test('the model count names the exception, not the total, when something is wrong', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.liveness = { models: [
    { model: 'a', state: 'alive' },
    { model: 'b', state: 'dead' },
    { model: 'c', state: 'HALF_OPEN' },
  ] };
  api.renderHealth();
  // "1 of 3 reachable" would make the operator do the subtraction to find the
  // number they care about.
  assert.match(dom.get('modelsNote').textContent, /2 de 3 não estão roteáveis/);
});

test('the summary facts exist nowhere else on the screen', () => {
  // The review's four Health facts were two echoes: 'rules' repeated the
  // sheet's numbered list (and its badge), 'invalid' repeated the lint banner's
  // "Policy invalid — N errors" 72px above. Only ROUTING and the CLASSIFIER
  // model say something no other surface says, so only they stay.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [{ id: 'a' }], classifier: { model: 'glm-4.7' } };
  api.state.status = { enabled: true, validation_errors: ['rule a: unknown field'] };
  api.renderHealth();
  const labels = findAll(dom.get('healthFacts'), 'fact-label').map((n) => n.textContent);
  assert.deepEqual(labels, ['routing', 'classifier'],
    `only the two facts that exist nowhere else, got ${JSON.stringify(labels)}`);
  const text = flat(dom.get('healthFacts'));
  assert.match(text, /glm-4\.7/);
  assert.doesNotMatch(text, /error/, 'the lint banner owns the invalid count');
  assert.doesNotMatch(text, /rules/, 'the sheet owns the rules count');
});

// ── the role an elo plays in the policy ──────────────────────────────────
// Health answers "o que quebra se este elo morrer?" — which needs the POLICY,
// not just liveness. The tiers arrive in the same Promise.all, so the role of
// every elo is a local fact with zero extra reads.

function rolePolicy() {
  // The shipped router.yaml's chains, so the positions are the real ones:
  // glm-5.3 is T2's primary AND the third hop of T3 and T4.
  return {
    rules: [], default: {},
    tiers: {
      T1: { model: 'glm-4.7', provider: 'zai', fallback: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }, { model: 'mimo-v2.5', provider: 'xiaomi' }] },
      T2: { model: 'glm-5.3', provider: 'zai', fallback: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }, { model: 'deepseek-v4-flash', provider: 'deepseek' }] },
      T3: { model: 'gpt-5.6-terra', provider: 'openai-codex', fallback: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }, { model: 'glm-5.3', provider: 'zai' }] },
      T4: { model: 'gpt-5.5', provider: 'openai-codex', fallback: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }, { model: 'glm-5.3', provider: 'zai' }] },
    },
  };
}

test('tierRoles names every position an elo occupies in the policy', () => {
  const { api } = loadConsole();
  const roles = plain(api.tierRoles(rolePolicy()));
  // The shipped fact that made the review: glm-5.3 looks like a one-tier elo
  // but three tiers depend on it — T2 as primary, T3 and T4 as hop 3.
  assert.deepEqual(roles['glm-5.3'], ['T2 · 1ª tentativa', 'T3 · 3ª tentativa', 'T4 · 3ª tentativa']);
  assert.deepEqual(roles['deepseek-v4-pro'], ['T3 · 2ª tentativa', 'T4 · 2ª tentativa']);
  assert.deepEqual(roles['gpt-5.6-luna'], ['T1 · 2ª tentativa', 'T2 · 2ª tentativa']);
  assert.deepEqual(roles['glm-4.7'], ['T1 · 1ª tentativa']);
  assert.deepEqual(roles['mimo-v2.5'], ['T1 · 3ª tentativa']);
  // A model in no tier gets no entry: nothing to say is not a finding.
  assert.equal(roles['us.anthropic.claude-opus-5'], undefined);
  assert.deepEqual(plain(api.tierRoles({})), {});
  assert.deepEqual(plain(api.tierRoles(null)), {});
});

test('a Health row says which tiers depend on the elo, and where', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = rolePolicy();
  api.state.liveness = { models: [
    { model: 'glm-5.3', provider: 'zai', state: 'alive' },
    { model: 'us.anthropic.claude-opus-5', provider: 'copilot-acp', state: 'alive' },
  ] };
  api.renderHealth();
  const roleLines = findAll(dom.get('models'), 'row-role');
  assert.equal(roleLines.length, 1,
    'only an elo the policy uses gets the role line — a retired elo cannot break a tier');
  assert.equal(roleLines[0].textContent, 'T2 · 1ª tentativa, T3 · 3ª tentativa, T4 · 3ª tentativa');
  const row = dom.get('models').children.find((c) => flat(c).includes('glm-5.3'));
  assert.match(flat(row), /T2 · 1ª tentativa, T3 · 3ª tentativa, T4 · 3ª tentativa/,
    'the answer to THIS tab\'s question sits on the elo\'s own row');
});

test('the compaction sentence states when it fires, not where usage is', () => {
  // /compaction reports no current usage at all, so a sentence that reads as
  // progress would be inventing a number — on a screen whose action restarts the
  // agent. Verified against router/threshold.py: p_eff(272000, 50) == 0.766.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.compaction = {
    aggressiveness: 50, summarizer_window: 272000,
    threshold_tokens: 208352, threshold_fraction: 0.766,
  };
  api.renderCompaction();
  const note = dom.get('compactionNote').textContent;
  assert.match(note, /fires once/, 'it must read as a trigger, not a level');
  assert.match(note, /77%/);
  assert.match(note, /272,000/, 'the window is separated for comparison');
  assert.doesNotMatch(note, /^at \d/, 'a bare "at 77%" reads as current usage');
  assert.equal(dom.get('compactionGroup').hidden, false,
    'the group is a PIPELINE fact now (DESIGN.md §1) — with data it is shown');
});

test('the aggressiveness dial says which way it points', () => {
  // p_eff subtracts 0.002 per point (threshold.py:28), so a HIGHER dial gives a
  // LOWER threshold — it compacts sooner. An operator raising it to "do less" gets
  // the opposite, so the direction is on screen.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.compaction = {
    aggressiveness: 50, summarizer_window: 272000,
    threshold_tokens: 208352, threshold_fraction: 0.766,
  };
  api.renderCompaction();
  const text = JSON.stringify(dom.get('compaction'));
  assert.match(text, /balanced/, 'the server has names for these presets');
  assert.match(text, /compacts sooner/, 'and the direction must be stated');
});

test('compaction is hidden when the sidecar reports no compaction data', () => {
  // DESIGN.md §1: Compaction lives in Pipeline and, like the Blocked group,
  // shows only when it carries an active condition. A sidecar without /compaction
  // data gets no group at all — the old "not implemented by this sidecar" note
  // was the console reporting on itself (§2.7) and is gone.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.compaction = null;
  api.renderCompaction();
  assert.equal(dom.get('compactionGroup').hidden, true,
    'no data, no group — Render nothing for nothing (§2.1)');
  assert.equal(dom.get('compactionNote').textContent, '',
    'and no console-self-report note survives');
  assert.equal(dom.get('compaction').children.length, 0,
    'and no fact is painted for a state that does not exist');
});

// ── the decision log's own honesty ───────────────────────────────────────
// Numbers on this screen are read as facts about the router. Two ways they lied:
// the scope pill said "Refused" for decisions that successfully chose a model, and
// every count was computed over the page the console happened to receive while
// presenting it as the whole record.
//
// The fixtures below are the REAL distribution, measured from the live log via
// RouterService.routes(): at limit 50 the sidecar returned 19 has_code_rule,
// 17 fail_safe_strong, 10 hard_rule, 4 blocklist_veto out of count=71. All 17
// fail_safe rows named a model; all 4 veto rows named none.

function decisionLog(api, { total = 71 } = {}) {
  const rows = [];
  const push = (n, cause, model) => {
    for (let i = 0; i < n; i += 1) rows.push({ id: `r${rows.length}`, cause, model, task: 't', ts: 1 });
  };
  push(19, 'has_code_rule', 'glm-5.2-fast');
  push(17, 'fail_safe_strong', 'claude-opus');   // a model WAS chosen
  push(10, 'hard_rule', 'gpt-5.6-terra');
  push(4, 'blocklist_veto', '');                  // nothing was chosen
  api.state.loading = false;
  api.state.routes = rows;
  api.state.routesTotal = total;
  return rows;
}

test('the scope pill does not call a successful route a refusal', () => {
  const { api, dom } = loadConsole();
  decisionLog(api);
  api.renderRoutes();

  const pill = JSON.stringify(dom.get('scopeOffRule'));
  // "Refused 21" was false for 17 of the 21: they each named a model they routed
  // to. The word has to be true of everything it counts.
  assert.doesNotMatch(pill, /Refused/, 'the pill must not claim a refusal it cannot support');
  assert.match(pill, /Sem regra associada/);
  assert.match(pill, /21/, 'it still counts everything that left the rule path');
});

test('the note distinguishes a refusal from a fail-safe catch', () => {
  const { api, dom } = loadConsole();
  decisionLog(api);
  api.state.scope = 'off-rule';
  api.renderRoutes();

  const note = dom.get('routesNote').textContent;
  // The distinction is the actionable part: a veto refused outright, while the
  // fail-safe DID route — because nothing else would take the task.
  assert.match(note, /4 recusada/);
  assert.match(note, /17 capturada\(s\) pelo último recurso/);
  assert.match(note, /21 de 50/, 'and the subset is scoped to what is on screen');
});

test('the "Sem regra associada" pill defines the subset it offers, before it is chosen', () => {
  // The pill names a subset the console knows how to define; the definition
  // rides on the pill so it is answerable before the operator commits to the
  // filter — the same hover idiom as the "fora da política" mark.
  const { api, dom } = loadConsole();
  decisionLog(api);
  api.renderRoutes();
  assert.match(dom.get('scopeOffRule').title,
    /decisões que não vieram de uma regra — um bloqueio recusou ou o último recurso capturou/);
});

test('an off-rule cause carries the definition of what it names', () => {
  const { api } = loadConsole();
  assert.match(api.causeTitle('fail_safe_strong'), /rede de segurança/,
    '"fail safe strong" is defined as a routing event, not a condition');
  assert.match(api.causeTitle('blocklist_veto'), /recusou/);
  assert.equal(api.causeTitle('has_code_rule'), '',
    'a cause the console has not learned gets nothing invented');
  assert.equal(api.causeTitle(''), '');
  assert.equal(api.causeTitle(null), '');

  // And the definition rides the row the term appears on.
  const { api: api2, dom } = loadConsole();
  decisionLog(api2);
  api2.renderRoutes();
  const rows = findAll(dom.get('routesTable'), 'cause');
  const failSafe = rows.find((c) => /fail safe strong/.test(c.textContent));
  assert.ok(failSafe, 'a fail-safe row is rendered');
  assert.match(failSafe.title, /rede de segurança/);
  const veto = rows.find((c) => /blocklist veto/.test(c.textContent));
  assert.ok(veto, 'a veto row is rendered');
  assert.match(veto.title, /recusou/);
});

test('a truncated log says so, and an untruncated one stays quiet', () => {
  const { api, dom } = loadConsole();
  decisionLog(api, { total: 71 });
  api.renderRoutes();
  // 50 rows presented as the record understated every hit count by 27%, and a rule
  // that fired only in the dropped 21 would have rendered "never fired".
  assert.match(dom.get('routesNote').textContent, /50 mais recentes de 71 gravadas/);

  // When the console has everything, the disclosure must disappear rather than
  // becoming permanent furniture.
  api.state.routesTotal = 50;
  api.renderRoutes();
  assert.doesNotMatch(dom.get('routesNote').textContent, /most recent/);
});

test('the scope note survives a search and still scopes it', () => {
  const { api, dom } = loadConsole();
  decisionLog(api);
  api.state.query = 'glm';
  api.renderRoutes();
  const note = dom.get('routesNote').textContent;
  assert.match(note, /19 de 50 casam com/);
  assert.match(note, /50 mais recentes de 71/, 'the window applies to a search too');
});

test('an empty off-rule set disables the pill instead of showing nothing', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [
    { id: 'a', cause: 'has_code_rule', model: 'm', ts: 1 },
    { id: 'b', cause: 'hard_rule', model: 'm', ts: 1 },
  ];
  api.state.routesTotal = 2;
  api.renderRoutes();
  // Nothing off the rule path is good news; a pill that filters to an empty list
  // reads as a broken control.
  assert.equal(dom.get('scopeOffRule').disabled, true);
  assert.match(JSON.stringify(dom.get('scopeOffRule')), /"0"/);
});

test('the record size is read from the response, not inferred from what arrived', async () => {
  // The envelope reports `count` — how many decisions EXIST — separately from the
  // page of `routes` it returns. Inferring the total from the page received makes
  // truncation invisible by construction: the console would always believe it had
  // everything. Verified against the live sidecar: routes(limit=50) returns
  // count=71 with 50 rows.
  const { api } = loadConsole({
    fetch: (url) => Promise.resolve({
      ok: true, status: 200,
      text: () => Promise.resolve(JSON.stringify(
        url.includes('/routes')
          ? { count: 71, routes: Array.from({ length: 50 }, (_, i) => ({ id: `r${i}`, cause: 'hard_rule', model: 'm', ts: 1 })) }
          : {},
      )),
    }),
  });
  await api.load();
  assert.equal(api.state.routes.length, 50, 'the page it received');
  assert.equal(api.state.routesTotal, 71, 'and the record it did not');
});

test('the log is fetched wider than the sidecar default', () => {
  // The default page is 50 and the record already holds 71, so the console asked
  // for a sample and reported it as the whole. Raising the limit does not remove
  // the need for the disclosure above — truncation drops the OLDEST rows, so the
  // cliff moves rather than disappearing — but it stops the common case from
  // being wrong.
  const fs = require('node:fs');
  const source = fs.readFileSync(sourcePath, 'utf8');
  assert.match(source, /call\('\/routes\?limit=\d+'\)/,
    'the routes fetch must name a limit above the 50 default');
  const limit = Number(source.match(/call\('\/routes\?limit=(\d+)'\)/)[1]);
  assert.ok(limit >= 200, `the limit must leave real headroom, got ${limit}`);
});

// ── the decision row: what it decided, when, and against what ─────────────
// The review's defect in one sentence: the operator read "FAIL SAFE STRONG →
// us.anthropic.claude-opus-5" on 17 of 40 lines and concluded the fail-safe
// burns the most expensive model — when today's fail_safe is glm-4.7 @ zai.
// The row now carries the RAIL, the HOUR, and the POLICY verdict, so that
// reading has the facts it was missing.

test('a decision row names the rail it ran on, not just the model', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [{
    id: 'r1', cause: 'fail_safe_strong', model: 'us.anthropic.claude-opus-5',
    provider: 'copilot-acp', task: 'debug the cache', ts: 1,
  }];
  api.renderRoutes();
  const row = dom.get('routesTable').children[0];
  const name = findAll(row, 'row-name')[0];
  assert.match(flat(name), /us\.anthropic\.claude-opus-5 @ copilot-acp/,
    'the rail is what makes a retired ACP destination readable');
});

test('a decision without a rail shows the model alone', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [{ id: 'r1', cause: 'hard_rule', model: 'gpt-5.6-terra', task: 't', ts: 1 }];
  api.renderRoutes();
  const name = findAll(dom.get('routesTable').children[0], 'row-name')[0];
  assert.equal(flat(name), 'gpt-5.6-terra');
});

test('a decision row carries the hour it happened, in UTC', () => {
  // The row used to say only "17d ago" — an age, never a clock. The hour is the
  // fact that decides what the decision COST, and it survives however long ago
  // the decision was: 03:20 UTC stays 03:20 UTC.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [{ id: 'r1', cause: 'hard_rule', model: 'gpt-5.6-terra', task: 't', ts: TRACE_AT }];
  api.renderRoutes();
  const value = dom.get('routesTable').children[0].children[2];
  assert.match(value.textContent, /ago · 03:20 UTC$/,
    'the age and the hour ride the same column, in the unit windows are declared in');
});

test('on Routes the price strip shows the selected decision hour, not now', () => {
  // The chain plan already prices a replay at recordedAt (planWhen); the strip
  // above the screens kept reporting NOW over a selected decision, so the rails
  // described the wrong hour for the decision being inspected.
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;                  // now is 07:14 UTC, four hours later
  api.state.tab = 'routes';
  api.state.replay = {
    id: 'r1', at: 0, steps: [], plan: null,
    recordedAt: new Date(TRACE_AT * 1000), // 03:20 UTC
  };
  api.renderClock();
  assert.equal(dom.get('clockNow').textContent, '03:20 UTC');
  assert.match(dom.get('clockLocal').textContent, /hora da decisão/,
    'the repriced hour is named, with its source');
  // Leaving Routes hands the strip back to the present.
  api.state.tab = 'health';
  api.renderClock();
  assert.equal(dom.get('clockNow').textContent, '07:14 UTC');
  assert.doesNotMatch(dom.get('clockLocal').textContent, /hora da decisão/);
});

test('a model the current policy cannot dispatch is marked, naming the source', () => {
  // glm-5.2 IS known to the registry (capabilities.py) and only absent from the
  // policy; us.anthropic.claude-opus-5 is known to neither. One fixed phrase
  // would say the same thing about both — the popover must resolve by id AND
  // by source.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.capabilities = null;
  api.state.routes = [
    { id: 'r1', cause: 'fail_safe_strong', model: 'us.anthropic.claude-opus-5', provider: 'copilot-acp', task: 't', ts: 1 },
    { id: 'r2', cause: 'hard_rule', model: 'glm-4.7', provider: 'zai', task: 't', ts: 1 },
  ];
  api.renderRoutes();
  const rows = dom.get('routesTable').children;
  const flags = findAll(rows[0], 'route-flag');
  assert.equal(flags.length, 1, 'the retired model is marked');
  assert.match(flags[0].title, /us\.anthropic\.claude-opus-5/);
  assert.match(flags[0].title, /desconhecido do registro/);
  assert.equal(findAll(rows[1], 'route-flag').length, 0,
    'a model the policy dispatches today carries no mark');
});

test('the mark says when the REGISTRY still knows the model', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.capabilities = { 'glm-5.2': { context_window: 128000 } };
  api.state.routes = [{ id: 'r1', cause: 'fail_safe_strong', model: 'glm-5.2', provider: 'zai', task: 't', ts: 1 }];
  api.renderRoutes();
  const flag = findAll(dom.get('routesTable').children[0], 'route-flag')[0];
  assert.match(flag.title, /glm-5\.2/);
  assert.match(flag.title, /o registro o conhece/,
    'known to the registry but not dispatched by the policy is a different fact');
});

test('the top line reconciles the log against the policy on screen', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.liveness = {
    models: [{ model: 'glm-4.7', state: 'alive' }, { model: 'gpt-5.6-luna', state: 'alive' }],
  };
  api.state.routes = [
    { id: 'r1', cause: 'hard_rule', model: 'glm-4.7', provider: 'zai', task: 't', ts: 1 },
    { id: 'r2', cause: 'hard_rule', model: 'gpt-5.6-luna', provider: 'openai-codex', task: 't', ts: 1 },
    { id: 'r3', cause: 'fail_safe_strong', model: 'us.anthropic.claude-opus-5', provider: 'copilot-acp', task: 't', ts: 1 },
  ];
  api.renderRoutes();
  assert.equal(dom.get('routesRecon').hidden, false);
  assert.equal(
    dom.get('routesRecon').textContent,
    '3 modelos no log · 2 entre os monitorados · 1 fora da política atual',
  );
});

test('the reconciliation line stays quiet without a policy to check against', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = null;
  api.state.routes = [{ id: 'r1', cause: 'hard_rule', model: 'glm-4.7', task: 't', ts: 1 }];
  api.renderRoutes();
  assert.equal(dom.get('routesRecon').hidden, true,
    'no policy loaded means no claim, not "everything is out"');
});

test('adjacent identical decisions collapse to one line with the count', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  // Newest first, as /routes serves them: three identical fail-safe decisions
  // to the same retired rail, then one unrelated hard-rule decision.
  api.state.routes = [
    { id: 'r4', cause: 'fail_safe_strong', model: 'opus', provider: 'acp', task: 'same task', ts: 4 },
    { id: 'r3', cause: 'fail_safe_strong', model: 'opus', provider: 'acp', task: 'same task', ts: 3 },
    { id: 'r2', cause: 'fail_safe_strong', model: 'opus', provider: 'acp', task: 'same task', ts: 2 },
    { id: 'r1', cause: 'hard_rule', model: 'terra', provider: 'codex', task: 'other task', ts: 1 },
  ];
  api.renderRoutes();
  const rows = dom.get('routesTable').children;
  assert.equal(rows.length, 2, 'three identical + one different = two lines');
  // The run keeps the MOST RECENT id — routes arrive newest-first, so r4.
  assert.equal(rows[0].dataset.routeId, 'r4');
  assert.match(findAll(rows[0], 'run-count')[0].textContent, /3×/);
  assert.equal(rows[1].dataset.routeId, 'r1');
  assert.equal(findAll(rows[1], 'run-count').length, 0,
    'a single decision carries no count');
});

test('a different decision between them breaks the run', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [
    { id: 'r3', cause: 'fail_safe_strong', model: 'opus', provider: 'acp', task: 'same', ts: 3 },
    { id: 'r2', cause: 'hard_rule', model: 'terra', provider: 'codex', task: 'other', ts: 2 },
    { id: 'r1', cause: 'fail_safe_strong', model: 'opus', provider: 'acp', task: 'same', ts: 1 },
  ];
  api.renderRoutes();
  assert.equal(dom.get('routesTable').children.length, 3,
    'only ADJACENT identical decisions collapse');
});

test('a different task breaks the run', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [
    { id: 'r2', cause: 'fail_safe_strong', model: 'opus', provider: 'acp', task: 'task A', ts: 2 },
    { id: 'r1', cause: 'fail_safe_strong', model: 'opus', provider: 'acp', task: 'task B', ts: 1 },
  ];
  api.renderRoutes();
  assert.equal(dom.get('routesTable').children.length, 2,
    'same destination, different task = different decision');
});

test('a different rail breaks the run', () => {
  // The rail is the most useful fact a decision row carries (the review's
  // finding: copilot-acp appears in no current provider:). Two adjacent rows
  // with the same model on DIFFERENT rails are different destinations and must
  // not collapse into one.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [
    { id: 'r2', cause: 'fail_safe_strong', model: 'opus', provider: 'acp', task: 'same', ts: 2 },
    { id: 'r1', cause: 'fail_safe_strong', model: 'opus', provider: 'anthropic', task: 'same', ts: 1 },
  ];
  api.renderRoutes();
  assert.equal(dom.get('routesTable').children.length, 2,
    'same model, different rail = different destination');
});

test('collapsing the measured corpus gives the measured reduction', () => {
  // The review measured 40 lines → 29 (-27.5%) on the live log. The retired
  // corpus collapses the same way under the same identity: cause, model,
  // provider, task. Pin the PURE function on a compact stand-in for that
  // distribution so a future identity change fails here before it confuses an
  // operator.
  const { api } = loadConsole();
  const row = (cause, model, provider, task) => ({ cause, model, provider, task });
  const routes = [
    row('fail_safe_strong', 'opus', 'acp', 'a'), row('fail_safe_strong', 'opus', 'acp', 'a'),
    row('fail_safe_strong', 'opus', 'acp', 'b'), row('fail_safe_strong', 'opus', 'acp', 'b'),
    row('blocklist_veto', '', '', 'c'), row('blocklist_veto', '', '', 'c'),
    row('hard_rule', 'terra', 'codex', 'd'),
  ];
  const runs = api.collapseRuns(routes);
  assert.equal(runs.length, 4);
  assert.deepEqual(plain(runs.map((run) => run.count)), [2, 2, 2, 1]);
  assert.equal(runs[0].head, routes[0], 'the run keeps the most recent (first) entry');
});

// ── what one click must never do ─────────────────────────────────────────
// Folding Validate into Apply removed three protections that the two-step ritual
// had provided as side effects. Each is restored deliberately here, and each is
// pinned, because the failure modes are silent: the screen says "Written." in all
// three cases.

test('a no-op apply is refused, because writing it destroys the undo', async () => {
  // RouterService.apply snapshots the current file to .bak before EVERY write
  // (router/service.py:415), with no comparison of merged against current. So an
  // apply that changes nothing still overwrites the one snapshot Revert restores.
  // The JSON twisty makes this one click away: renderAll() keeps that textarea
  // filled with the live policy whenever the console is not editing, so its Apply
  // is a guaranteed no-op write on a freshly loaded panel.
  const posted = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      posted.push(url);
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '', base_hash: 'h' })),
      });
    },
  });
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { rules: [] });

  assert.equal(posted.length, 1, 'the plan happens; the write must not');
  assert.match(posted[0], /\/plan$/);
  assert.match(msg.textContent, /não há o que salvar/);
  assert.match(msg.className, /ok/, 'nothing to do is not an error');
});

test('applying shows what it is about to change', async () => {
  // /plan deep-merges the draft over whatever is on disk NOW and replaces lists
  // wholesale (service.py:50), so a rule added by a CLI edit or another tab since
  // page load is deleted by the next Apply. The diff contains that deletion. The
  // old two-click path made it unavoidable — Validate rendered it and Apply was
  // unreachable until it had. One-click Apply was computing it and throwing it away.
  const diff = { hidden: true, textContent: '' };
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => Promise.resolve({
      ok: true, status: 200,
      text: () => Promise.resolve(JSON.stringify(
        url.endsWith('/plan')
          ? { valid: true, policy: {}, base_hash: 'h', diff: '-  - id: URGENT-block-prod\n' }
          : { ok: true },
      )),
    }),
  });
  await api.doApply('/apply', { textContent: '', className: '' }, { rules: [] }, diff);

  assert.equal(diff.hidden, false, 'the operator must see the diff they authorised');
  assert.match(diff.textContent, /URGENT-block-prod/,
    'including a concurrent edit this write would remove');
});

test('a double-clicked apply does not race itself', async () => {
  // Two overlapping plan+apply pairs share a base_hash: the first writes, the
  // second 409s, and the operator is told "Policy changed underneath" about their
  // own second click.
  // Count only the writes. A single doApply legitimately makes two sequential
  // requests (/plan then /apply), and the load() refresh that follows a successful
  // write fires seven reads in parallel — so counting all traffic measures neither
  // the guard nor a race.
  const writes = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (/\/apply$/.test(url)) writes.push(url);
      return new Promise((resolve) => {
        setTimeoutReal(() => resolve({
          ok: true, status: 200,
          text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, base_hash: 'h', diff: '+x' })),
        }), 5);
      });
    },
  });
  const msg = { textContent: '', className: '' };
  const second = { textContent: '', className: '' };
  const first = api.doApply('/apply', msg, { rules: [] });
  await api.doApply('/apply', second, { rules: [] });
  await first;

  assert.match(second.textContent, /gravação já está em andamento/, 'the second click is told to wait');
  assert.equal(writes.length, 1, 'the second click must not produce a second write');
});

test('the JSON twisty cannot write while the console is read-only', () => {
  // Expanding "Edit the whole policy as JSON" to READ it used to put a live green
  // Apply and a live Revert one tap away — and Revert takes no plan, shows no diff,
  // and restores whatever the .bak holds.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });

  api.setMode('reading');
  assert.equal(dom.get('jsonApply').disabled, true, 'Apply follows the read-only default');
  assert.equal(dom.get('jsonRevert').disabled, true, 'and so does the destructive one');

  api.setMode('editing');
  assert.equal(dom.get('jsonApply').disabled, false);
  assert.equal(dom.get('jsonRevert').disabled, false);
});

// ── it belongs to the shell ───────────────────────────────────────────────
// The operator's verdict on the previous look was that the three panels read as
// guests: their own near-black palette, their own 13px/10.5px type steps, and a
// "ROUTER · HERMES ONE" wordmark on each. These assertions pin the redesign's
// intent — the console's visual world is now the HOST'S — and they replace the
// ones that used to pin the wordmark and the guest palette.
//
// They are static scans because that is where the failure would be: a colour
// re-hardcoded in CSS cannot be caught by driving the DOM stub.

// Comments are stripped before every scan below. They are where the REASONING
// lives, and that reasoning quotes the very strings these tests forbid — the
// wordmark it removed, the 13px step it replaced. Scanning them would make the
// explanation of a fix indistinguishable from the fix's absence.
function consoleStyle() {
  const fs = require('node:fs');
  const raw = fs.readFileSync(sourcePath, 'utf8');
  const style = raw.match(/<style>([\s\S]*?)<\/style>/);
  assert.ok(style, 'console.html must contain exactly one inline <style>');
  return {
    html: raw.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, ''),
    style: style[1].replace(/\/\*[\s\S]*?\*\//g, ''),
  };
}

test('the wordmark is gone: the host rail already says where you are', () => {
  const { html } = consoleStyle();
  // The mark cost 9 characters of chrome to repeat what the highlighted rail icon
  // states for free, in a tracked-uppercase mono voice the shell never uses.
  assert.doesNotMatch(html, /HERMES ONE|Hermes One<\/span>/,
    'no "<SURFACE> HERMES ONE" masthead survives');
  assert.doesNotMatch(html, /class="brand/, 'and no brand block to hang one on');
  // What replaced it is the host's own panel header shape.
  assert.match(html, /class="view-head"/);
  assert.match(html, /class="view-title"/);
  assert.match(html, /class="view-actions"/);
});

test('the header is the host\'s .main-view-header, measured not guessed', () => {
  const { style } = consoleStyle();
  // Measured live off the running shell: .main-view-header is min-height 41px with
  // a 1px bottom border on --border; .main-view-title is 18px / 600 /
  // letter-spacing -.18px in the UI SANS — not mono.
  const head = style.match(/\.view-head \{[\s\S]*?\}/)[0];
  assert.match(head, /min-height: 41px/);
  assert.match(head, /border-bottom: 1px solid var\(--line\)/);
  const title = style.match(/\.view-title \{[\s\S]*?\}/)[0];
  assert.match(title, /600 var\(--t-head\)/, 'the host weight and step');
  assert.match(title, /letter-spacing: -\.18px/);
  assert.match(title, /var\(--sans\)/, 'sans, because a view title is not a machine fact');
  assert.doesNotMatch(title, /text-transform: uppercase/);
});

test('not one colour is hard-coded: every token reads the live skin', () => {
  const { style } = consoleStyle();
  const root = style.match(/:root \{[\s\S]*?\n    \}/)[0];
  // The host ships 21 skins x light/dark. A copied palette is wrong in 20 of them,
  // and in any light skin this console was a black rectangle inside a parchment
  // shell. So every colour token must READ a --host-* property; the hex after the
  // comma is only the fallback for a shell that cannot be read at all.
  const colours = [
    '--bg', '--surface', '--surface-raised', '--surface-hover', '--line',
    '--line-strong', '--text', '--muted', '--faint', '--accent', '--accent-text',
    '--ok', '--warn', '--bad', '--info',
  ];
  for (const token of colours) {
    const declaration = root.match(new RegExp(`\\n\\s*${token}:([^;]*);`));
    assert.ok(declaration, `${token} must be declared`);
    assert.match(declaration[1], /var\(--host-/,
      `${token} must read the shell, not carry a palette (got "${declaration[1].trim()}")`);
  }
  // And the type/shape ladder comes from the shell too, so the console changes
  // font size with the host's own root-font control.
  assert.match(root, /--t-body: var\(--host-font-size-md/);
  assert.match(root, /--t-label: var\(--host-font-size-xs/);
  assert.match(root, /--sans: var\(--host-font-ui/);
  assert.match(root, /--radius: var\(--host-radius-md/);
  assert.match(root, /color-scheme: var\(--host-color-scheme/);
});

test('the type steps are the host\'s ladder, with no one-off sizes', () => {
  const { style } = consoleStyle();
  // The host's guide asks for 11px metadata / 12px labels / 14px body / 16-18px
  // headings and says not to proliferate 10px/10.5px/12.5px one-offs. The old
  // scale was 10.5/13/16/20 — a guest's scale that read as an accident beside
  // host chrome.
  assert.doesNotMatch(style, /10\.5px|12\.5px|\b13px\b/,
    'no guest-scale one-offs survive');
  // Only the two steps the host has no token for are literals.
  const literals = [...style.matchAll(/font-size: (\d+(?:\.\d+)?)px/g)].map((m) => m[1]);
  assert.deepEqual(literals, [], 'every font-size goes through a step token');
});

test('gold marks selection; the semantic four keep reporting condition', () => {
  const { api } = loadConsole();
  const { style } = consoleStyle();
  // The old accent was paper white precisely so no hue competed with a health
  // dot. Inside Hermes One the accent is the skin's identity, so the rule is now
  // split by MEANING: accent = where you are / what you picked; the four state
  // colours = condition. A gold underline must never be readable as health.
  const causes = ['blocklist_veto', 'fail_safe_strong', 'classifier', 'hard_rule'];
  for (const cause of causes) {
    assert.doesNotMatch(api.causeColor(cause), /--accent/,
      `${cause} is a condition and must not wear the selection colour`);
    assert.match(api.causeColor(cause), /var\(--(ok|warn|info|bad)-text\)/,
      `${cause} must use one of the four condition tokens, in its TEXT form`);
  }
  // Condition dots read the four; nothing else does.
  assert.match(style, /\.is-alive\s+\.dot \{ background: var\(--ok\)/);
  assert.match(style, /\.is-degraded\s+\.dot \{ background: var\(--warn\)/);
  assert.match(style, /\.is-quota\s+\.dot \{ background: var\(--info\)/);
  assert.match(style, /\.is-dead\s+\.dot \{ background: var\(--bad\)/);
  // Selection reads the accent: the host's own 20px x 2px tab underline, the
  // picked decision, the matched rule, and the one committing button.
  assert.match(style, /\.tab\[aria-selected="true"\]::after \{[\s\S]*?width: 20px; height: 2px;[\s\S]*?background: var\(--accent\)/);
  assert.match(style, /\.step\.hit \{[^}]*var\(--accent\)/);
  assert.match(style, /\.trace\.on \{[^}]*var\(--accent\)/);
  assert.match(style, /\.btn\.go \{[^}]*background: var\(--accent\)/);
});

test('the touch and viewport guarantees survive the reskin', () => {
  const { style } = consoleStyle();
  // The host's own mobile breakpoint is 640px: below 641px it hides the rail and
  // the drawer becomes the only navigation. Matching it means this console changes
  // shape at the same width the shell around it does.
  assert.match(style, /@media \(max-width: 640px\)/);
  assert.match(style, /@media \(hover: none\) and \(pointer: coarse\)/);
  const touch = style.slice(style.indexOf('@media (hover: none) and (pointer: coarse)'));
  assert.match(touch, /min-height: 44px/, 'a finger needs 44px');
  assert.match(touch, /font-size: max\(16px, 1em\)/, 'under 16px iOS zooms and never zooms back');
  // dvh, not vh: on iOS a vh column overflows by the toolbar's height.
  assert.match(style, /100dvh/);
  assert.doesNotMatch(style, /min-height: 100vh/);
  // ONE authored moment, from an already-visible default, and it yields.
  assert.match(style, /@media \(prefers-reduced-motion: reduce\)/);
  const keyframes = [...style.matchAll(/@keyframes ([\w-]+)/g)].map((m) => m[1]);
  assert.equal(keyframes.length, 1, `one authored motion, got ${keyframes.join(', ')}`);
});

test('the decision sheet spends colour only where it is news', () => {
  // Measured on the live sheet before this rule: five cyan destinations, one gold
  // tab underline, one amber "never fired" and two reds in a single 1708x891
  // viewport. The host's guide is explicit — one accent at a time, semantic colours
  // for semantic state — so the sheet now colours a REFUSAL and nothing else in
  // the destination column. "goes to the classifier" is a kind of destination, not
  // a condition, and it is already named on the Stage 1 heading.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [
      { id: 'trivial', when: { verb_class: { eq: 'trivial' } }, then: { model: 'T1' } },
      { id: 'review', when: { keywords: { contains: 'review' } }, then: { action: 'classify' } },
      { id: 'nope', when: {}, then: { deny: true } },
    ],
    default: { action: 'classify' },
    classifier: { model: 'deepseek-v3.2' },
    fail_safe: { model: 'us.anthropic.claude-opus-5' },
    tiers: { T1: { model: 'glm-5.2' } },
  };
  api.renderSheet();

  const painted = [];
  const walk = (node) => {
    if (!node) return;
    // The tier chip and the plain span are the two destination renderings; both
    // must obey the same budget — the walk names both so a painted chip cannot
    // slip past the invariant as a "new" element.
    if ((node.className === 'step-target' || /(^|\s)step-target(\s|$)/.test(node.className))
        && node.style && node.style.color) {
      painted.push([node.textContent, node.style.color]);
    }
    (node.children || []).forEach(walk);
  };
  walk(dom.get('sheet'));

  // The one refusal: the deny rule. There is no blocklist veto row here because
  // this policy declares no manual ban, and the synthetic row is conditional on
  // blocklist.manual_ban being non-empty (spec 1.3).
  assert.deepEqual(painted.map((p) => p[0]).sort(), ['Recusar a tarefa'],
    `only refusals are coloured, got ${JSON.stringify(painted)}`);
  for (const [, colour] of painted) {
    assert.match(colour, /--bad-text/, 'and a refusal is the danger token');
  }
  // The classifier's destinations exist and are legible — they are just not paint.
  const words = JSON.stringify(dom.get('sheet'));
  assert.match(words, /classificador/, 'the classifier is still named');
  assert.match(words, /gasta uma chamada de modelo a mais/,
    'and inference is still flagged, once');
});

test('the ages are the report, so they never collapse on a phone', () => {
  // The header used to collapse the "checked HH:MM" clock at 390px — that text
  // was chrome, and the clock was the first thing to give way. The three
  // provenance ages are the opposite: the whole reason this header exists is to
  // say WHICH source is stale, and hiding them would resurrect the exact defect
  // this console was built to surface. So they render at every width, and only a
  // dead sidecar or a not-yet-read status changes the words.
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.unreachable = false;
  api.state.status = {
    process_started_at: new Date(T - 2 * 3600 * 1000).toISOString(),
    code_mtime: new Date(T - 1 * 3600 * 1000).toISOString(),
    config_mtime: new Date(T - 5 * 60 * 1000).toISOString(),
  };
  api.renderRail();
  assert.doesNotMatch(dom.get('reach').className, /is-fresh/,
    'no fresh-state class: the ages are not collapsible chrome');
  assert.match(dom.get('reachText').textContent, /serviço no ar há 2h/);

  // A dead sidecar keeps its words at every width: that is a condition, not chrome.
  api.state.unreachable = true;
  api.renderRail();
  assert.doesNotMatch(dom.get('reach').className, /is-fresh/);
  assert.match(dom.get('reachText').textContent, /Não consegui falar com o roteador/);

  // And so does "we have not read anything yet", which is not the same as fine.
  api.state.unreachable = false;
  api.state.status = undefined;
  api.renderRail();
  assert.doesNotMatch(dom.get('reach').className, /is-fresh/);
  assert.equal(dom.get('reachText').textContent, 'Ainda não li nada do roteador.');

  // The title itself never truncates by being hidden — it is always in the DOM.
  const fs = require('node:fs');
  const html = fs.readFileSync(sourcePath, 'utf8');
  assert.match(html, /<h1 class="view-title">Roteador de modelos<\/h1>/);
});

test('a state colour used as TYPE is derived, because the raw hue is illegible', () => {
  // Measured across every palette in the host's style.css that declares a full set
  // (23, both polarities), each state colour as text on that palette's own --bg:
  //   --info 1.38:1 · --success 1.49:1 · --warning 1.61:1 · --error 3.44:1
  // all worst in neon-paint/light, against a 4.5:1 floor for body text. Those
  // values are authored for FILLS. So the console carries two forms of each state
  // colour — the raw hue for a dot, and a 45%-toward---text mix for a word, which
  // measures 5.08:1 in the worst case and stays the right hue in every skin.
  const { style } = consoleStyle();

  for (const token of ['--ok-text', '--warn-text', '--bad-text', '--info-text']) {
    const mix = style.match(new RegExp(token + ': color-mix\\(in srgb,[^;]+;'));
    assert.ok(mix, `${token} must be derived for legibility`);
    assert.match(mix[0], /45%/, 'at the measured ratio');
    assert.match(mix[0], /var\(--host-text/, 'toward the SKIN\'S text, so it cannot invert');
    // A flat pre-color-mix fallback must exist, or an engine without color-mix
    // leaves state words unstyled rather than merely lower-contrast.
    assert.match(style, new RegExp(token + ': var\\(--host-'), token + ' needs a flat fallback');
  }
  // Every place a state colour is TYPE uses the derived form...
  for (const rule of [
    /\.is-alive \.state \{ color: var\(--ok-text\)/,
    /\.is-degraded \.state \{ color: var\(--warn-text\)/,
    /\.is-quota \.state \{ color: var\(--info-text\)/,
    /\.is-dead \.state \{ color: var\(--bad-text\)/,
    /\.step-hits\.zero \{ color: var\(--warn-text\)/,
    /\.stage\.inference \{ color: var\(--info-text\)/,
  ]) assert.match(style, rule, `state type must be derived: ${rule}`);
  // ...and every DOT keeps the raw hue, which is what makes it identifiable.
  assert.match(style, /\.is-alive\s+\.dot \{ background: var\(--ok\);/);
  assert.match(style, /\.is-dead\s+\.dot \{ background: var\(--bad\);/);
  // A 6px dot at 1.38:1 is not locatable, so it gets a hairline edge instead of a
  // different colour.
  assert.match(style, /\.dot \{[\s\S]*?outline: 1px solid var\(--line-strong\)/);
});

test('the probe verdict is a sentence, not a row of fragments', async () => {
  // It was built as a flex row with gap:9px, which LOOKED right and read wrong:
  // a gap is layout, not text, so the string was
  // "Caught byhard-verbs→gpt-5.6-terraon openai-codex" — which is exactly what a
  // screen reader says out loud, and what lands in a clipboard.
  const explain = {
    mode: 'deterministic_dry_run', requires_classifier: false,
    decision: {
      matched_rule_id: 'hard-verbs',
      output: {
        model: 'gpt-5.6-terra', provider: 'openai-codex',
        fallback: [{ model: 'us.anthropic.claude-opus-5' }, { model: 'deepseek-v4-pro' }],
      },
    },
  };
  const { api, dom } = loadConsole({
    fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(explain)) }),
  });
  api.state.loading = false;
  api.state.policy = { rules: [{ id: 'hard-verbs', then: { model: 'T4' } }], tiers: { T4: {} } };
  await api.probe('Debug a race condition in the cache');

  // Flatten the way textContent would.
  const read = (node) => (node.children || []).reduce(
    (acc, kid) => acc + String(kid.textContent || '') + read(kid), '');
  const sentence = read(dom.get('probeResult'));

  assert.match(sentence, /Capturado por hard-verbs/, 'words must not run together');
  assert.match(sentence, /roteado para gpt-5\.6-terra/);
  assert.match(sentence, /em openai-codex/);
  assert.match(sentence, /Recorre a us\.anthropic\.claude-opus-5 → deepseek-v4-pro/);
  assert.doesNotMatch(sentence, /byhard|terraon/, 'no missing space survives');
});

test('the iOS zoom guard names the classes, or it does nothing at all', () => {
  // This block existed and was DECORATIVE. `input, textarea, select` scores
  // (0,0,1); every input in this console is reached by a class — .probe-input,
  // .editor, .field input — which scores (0,1,0) and wins. Measured in a real
  // iPhone 13 context (the only way (pointer:coarse) genuinely matches): the probe
  // field, the decision filter and the COMPACT confirmation all computed 14px with
  // the guard present, so iOS would zoom in on focus and never zoom back out.
  // After naming the classes, all four measure 16px.
  const { style } = consoleStyle();
  const touch = style.slice(style.indexOf('@media (hover: none) and (pointer: coarse)'));
  assert.ok(touch, 'the coarse-pointer block must exist');
  const guard = touch.match(/([^{}]*)\{\s*font-size: max\(16px, 1em\);?\s*\}/);
  assert.ok(guard, 'the 16px guard must be present');
  const selector = guard[1];
  // Every class that actually reaches an input in this console.
  for (const cls of ['.probe-input', '.editor', '.field input']) {
    assert.ok(selector.includes(cls),
      `${cls} outranks a bare element selector, so the guard must name it — got "${selector.trim()}"`);
  }
});

// ── context, capability and the shape of a chain ──────────────────────────
// A rule can now say `est_input_tokens: {gt: 400000}` or `needs_vision: {eq:
// true}`, a tier can pick its fallbacks at random, and the capability filter can
// drop an elo or disqualify every one of them and override itself to keep routing
// alive. Every fact below is one an operator ACTS on, and every one of them is a
// pure function precisely so it is pinned here rather than eyeballed once.

// A value the console created inside the VM, brought back into this realm: its
// Array and Object prototypes are the VM's, so a strict deep-equal against a
// literal here fails on identity alone and says nothing about the data.
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}
// Flatten a rendered subtree the way textContent would.
function flat(node) {
  return (node.children || []).reduce(
    (acc, kid) => acc + String(kid.textContent || '') + flat(kid), '');
}
// Every descendant carrying a class, in document order.
function findAll(node, cls, out = []) {
  (node.children || []).forEach((kid) => {
    if (String(kid.className || '').split(/\s+/).includes(cls)) out.push(kid);
    findAll(kid, cls, out);
  });
  return out;
}

test('a predicate says which family it belongs to, because the three mean different things', () => {
  const { api } = loadConsole();
  // `size_lines: {gt: 400}` is about the TASK, `est_input_tokens: {gt: 400000}`
  // about the CONTEXT it needs, `needs_vision: {eq: true}` about a CAPABILITY the
  // model must have. Reading the second as the first is how a rule gets "fixed"
  // by shrinking a number that was never about the prompt's length.
  assert.equal(api.predicateFamily('size_lines'), 'shape');
  assert.equal(api.predicateFamily('verb_class'), 'shape');
  assert.equal(api.predicateFamily('est_input_tokens'), 'context');
  assert.equal(api.predicateFamily('needs_vision'), 'capability');
  assert.equal(api.predicateFamily('needs_tools'), 'capability');
  assert.equal(api.predicateFamily('attachment_kinds'), 'capability');
  // A field this console has not learned is task shape — what every signal was
  // before context and capability existed — never an invented fourth family.
  assert.equal(api.predicateFamily('some_future_signal'), 'shape');

  const context = plain(api.predicateChip('est_input_tokens', { gt: 400000 }));
  assert.equal(context.family, 'context');
  assert.equal(context.kind, 'context', 'the family is a WORD in the chip, so it survives being read aloud');
  assert.equal(context.text, 'o contexto estimado passa de 400.000 tokens');
  assert.doesNotMatch(context.text, /400000/, 'six digits are compared, not counted');

  assert.deepEqual(plain(api.predicateChip('needs_vision', { eq: true })),
    { family: 'capability', kind: 'needs', text: 'o pedido envolve imagem' });
  // A negative capability clause is a real predicate and must not read as the
  // positive one with a colour difference nobody can hear.
  assert.equal(api.predicateChip('needs_vision', { eq: false }).text,
    'o pedido não envolve imagem');

  const shape = plain(api.predicateChip('verb_class', { eq: 'hard' }));
  assert.equal(shape.family, 'shape');
  assert.equal(shape.kind, '', 'task shape is the default, so it spends no label');
  assert.equal(shape.text, 'o verbo do pedido é difícil');
});

test('a rule row draws one chip per clause, so two conditions never merge into one', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [{
      id: 'huge-vision-task',
      when: { has_code: { eq: true }, est_input_tokens: { gt: 400000 }, needs_vision: { eq: true } },
      then: { model: 'T4' },
    }],
    tiers: { T4: {} }, default: {},
  };
  api.renderSheet();

  const chips = findAll(dom.get('sheet'), 'chip');
  assert.equal(chips.length, 3, 'one chip per clause — a dropped clause misstates why the rule fires');
  const families = chips.map((chip) => chip.className);
  assert.ok(families.some((c) => c.includes('context')), 'the context condition is marked as one');
  assert.ok(families.some((c) => c.includes('capability')), 'and so is the capability condition');
  assert.ok(families.some((c) => /chip shape/.test(c)), 'and the task-shape one stays what it was');
  // Each clause is its own list item, which is what keeps "has code" and "over
  // 400,000 tokens" from being announced as one string.
  const values = findAll(dom.get('sheet'), 'chip-val').map((n) => n.textContent);
  assert.deepEqual(values, ['tem código',
    'o contexto estimado passa de 400.000 tokens', 'o pedido envolve imagem']);
});

test('a rule with no clauses keeps its sentence instead of an empty chip', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [{ id: 'catch-all', when: {}, then: { model: 'T1' } }], tiers: { T1: {} }, default: {} };
  api.renderSheet();
  assert.equal(findAll(dom.get('sheet'), 'chip').length, 0, 'nothing is rendered for nothing');
  assert.match(flat(dom.get('sheet')), /vale para toda tarefa/,
    'and "every task" is still said, as prose');
});

// ── a tier destination is a chip, not a mute span ────────────────────────
// "T3" in the destination column read as a model id while meaning "try this
// chain" — the vocabulary gap the review measured 6× above the fold with its
// definition half a viewport below. The chip reveals the chain in place.

function chipPolicy() {
  return {
    rules: [
      { id: 'deep', when: {}, then: { model: 'T3' } },
      { id: 'no', when: {}, then: { deny: true } },
    ],
    default: {},
    tiers: {
      T3: {
        model: 'gpt-5.6-terra', provider: 'openai-codex', billing_mode: 'subscription',
        fallback: [
          { model: 'deepseek-v4-pro', provider: 'deepseek', billing_mode: 'metered' },
          { model: 'glm-5.3', provider: 'zai', billing_mode: 'plan' },
        ],
        fallback_strategy: 'sequential',
      },
    },
  };
}

test('a tier destination is a chip that reveals the chain it points at', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.renderSheet();

  const chips = findAll(dom.get('sheet'), 'step-tier');
  assert.equal(chips.length, 1, 'the tier destination is the chip; the deny row is not');
  const chip = chips[0];
  // The chip carries the group's meaning, not the bare key (spec 4.3).
  assert.equal(chip.textContent, 'Grupo T3 · Moderado');
  assert.equal(chip.getAttribute('aria-expanded'), 'false');
  // Hover carries the compact form, primary first — the elos of the chain.
  assert.equal(chip.title, 'gpt-5.6-terra · deepseek-v4-pro · glm-5.3');
  // Render nothing for nothing: the chain is not built until it is asked for —
  // the expansion element exists (hidden) so the toggle has a target, but it
  // holds no chain yet.
  const before = findAll(dom.get('sheet'), 'step-chain');
  assert.equal(before.length, 1);
  assert.equal(before[0].hidden, true, 'collapsed by default');
  assert.equal(before[0].children.length, 0, 'and nothing rendered inside');

  chip._listeners.click();
  assert.equal(chip.getAttribute('aria-expanded'), 'true');
  const expansions = findAll(dom.get('sheet'), 'step-chain');
  assert.equal(expansions.length, 1);
  const text = flat(expansions[0]);
  assert.match(text, /gpt-5\.6-terra/);
  assert.match(text, /deepseek-v4-pro/);
  assert.match(text, /glm-5\.3/);
  assert.match(text, /na ordem escrita/, 'the same strategy words the Tier chains group uses');

  chip._listeners.click();
  assert.equal(chip.getAttribute('aria-expanded'), 'false');
  assert.equal(expansions[0].hidden, true, 'a second click collapses the chain again');
});

test('the tier chip expands a rule whose shared hops are the finding', () => {
  // T3 and T4 share deepseek-v4-pro and glm-5.3 — the fact that changes the
  // reading of "a regra manda pra T4": it is not a single model, and it is not
  // independent of T3. The chip must expose the SHARED hops, not just a name.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [{ id: 'vision', when: {}, then: { model: 'T4' } }],
    default: {},
    tiers: {
      T3: { model: 'gpt-5.6-terra', provider: 'openai-codex',
        fallback: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }, { model: 'glm-5.3', provider: 'zai' }] },
      T4: { model: 'gpt-5.5', provider: 'openai-codex',
        fallback: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }, { model: 'glm-5.3', provider: 'zai' }] },
    },
  };
  api.renderSheet();
  const chip = findAll(dom.get('sheet'), 'step-tier')[0];
  assert.equal(chip.textContent, 'Grupo T4 · Difícil');
  chip._listeners.click();
  const text = flat(findAll(dom.get('sheet'), 'step-chain')[0]);
  assert.match(text, /gpt-5\.5/, 'T4\'s own primary');
  assert.match(text, /deepseek-v4-pro/, 'the hop T4 shares with T3');
  assert.match(text, /glm-5\.3/, 'and the hop it shares with T3 — the two hops T4 is not independent on');
});

test('the tier chip does not trigger the row\'s edit click', () => {
  // In editing mode the row opens the inspector on click; the chip is inside
  // the row, so its click must not navigate — an operator expanding a chain is
  // not selecting the rule for editing.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.setMode('editing');
  api.renderSheet();
  const chip = findAll(dom.get('sheet'), 'step-tier')[0];
  assert.ok(chip, 'the chip exists in editing mode too');
  chip._listeners.click();
  assert.equal(api.state.selected, null,
    'expanding the chain did not open the inspector');
  assert.equal(chip.getAttribute('aria-expanded'), 'true');
});

// ── the window a count is taken over ─────────────────────────────────────
// "counts over the last 40 decisions, since 17d ago" implied continuous
// coverage the file did not have: the corpus under review spanned one 3h39min
// session of hand-typed smoke lines, and the policy had changed since. Every
// assertion below pins the replacement — the window named by its endpoints,
// and a sheet that refuses to count rules newer than it.

test('windowSpan names the window by its endpoints, never by the oldest age', () => {
  const { api } = loadConsole();
  // The measured corpus: 22:12:11 on 01/08 → 01:51:38 on 02/08 UTC, 3h39min27s.
  const t0 = Date.UTC(2026, 7, 1, 22, 12, 11) / 1000;
  const t1 = Date.UTC(2026, 7, 2, 1, 51, 38) / 1000;
  assert.equal(api.windowSpan([
    { ts: t1 }, { ts: t0 + 100 }, { ts: t0 },
  ]), 'de 01/08 22:12 a 02/08 01:51 UTC',
  'a window crossing midnight UTC names both endpoints');
  // Same UTC day → the compact form the review names: "3h39min de 01/08".
  const a = Date.UTC(2026, 7, 1, 19, 12, 11) / 1000;
  const b = Date.UTC(2026, 7, 1, 22, 51, 38) / 1000;
  assert.equal(api.windowSpan([{ ts: b }, { ts: a }]), '3h39min de 01/08');
  // Sub-minute windows stay truthful, and junk ts contribute nothing.
  assert.equal(api.windowSpan([{ ts: b }, { ts: b - 45 }]), '45s de 01/08');
  assert.equal(api.windowSpan([{ ts: 'junk' }, {}]), '');
  assert.equal(api.windowSpan([]), '');
});

test('hitsWindow names the period a count is true of, read from the data', () => {
  // It used to return the legend that sat ABOVE the list, and after spec §3.3 moved
  // the period into each row it had no caller at all. Same reading, now in the
  // fewest words that fit inside a row — and read from the SPAN of the decisions
  // actually recorded, never from STALE_WINDOW_DAYS: 40 decisions over one evening
  // are not "os últimos 7 dias" just because the freshness gate uses 7.
  const { api } = loadConsole();
  const t0 = Date.UTC(2026, 7, 1, 22, 12, 11) / 1000;
  const t1 = Date.UTC(2026, 7, 2, 1, 51, 38) / 1000;
  api.state.routes = Array.from({ length: 40 }, (_, i) => ({ ts: t0 + (t1 - t0) * i / 39 }));
  assert.equal(api.hitsWindow(), 'nas últimas 4h');
  // A week of decisions reads in days, with agreement, and a single day is singular.
  const day = 86400;
  api.state.routes = [{ ts: t1 - 7 * day }, { ts: t1 }];
  assert.equal(api.hitsWindow(), 'nos últimos 7 dias');
  api.state.routes = [{ ts: t1 - day }, { ts: t1 }];
  assert.equal(api.hitsWindow(), 'no último dia', 'one of anything is singular');
  api.state.routes = [{ ts: t1 - 3600 }, { ts: t1 }];
  assert.equal(api.hitsWindow(), 'na última hora');
  // Under an hour it says minutes rather than rounding a real window down to zero.
  api.state.routes = [{ ts: t1 - 300 }, { ts: t1 }];
  assert.equal(api.hitsWindow(), 'nos últimos 5min');
  api.state.routes = [];
  assert.equal(api.hitsWindow(), '', 'no window, no claim');
});

test('logFreshness says when the window cannot describe the current policy', () => {
  const { api } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  const nowS = T / 1000;
  const hourS = 3600;
  const dayS = 86400;

  // Fresh: the newest decision is AFTER the config mtime and inside the day backstop.
  api.state.routes = [{ ts: nowS - 2 * hourS }, { ts: nowS - 3 * hourS }];
  api.state.status = { config_mtime: new Date(T - 5 * hourS * 1000).toISOString() };
  assert.deepEqual(plain(api.logFreshness()), { stale: false, reason: null, days: 2 / 24 });

  // The router.yaml on disk is NEWER than the newest decision: every rule in the
  // sheet is newer than the window, so a zero hit proves nothing about it.
  api.state.routes = [{ ts: nowS - 2 * hourS }];
  api.state.status = { config_mtime: new Date(T - 1 * hourS * 1000).toISOString() };
  assert.equal(api.logFreshness().stale, true);
  assert.equal(api.logFreshness().reason, 'config');
  assert.ok(Math.abs(api.logFreshness().days - 2 / 24) < 1e-9,
    'the config case still measures the age for the banner');

  // An older sidecar reports no config_mtime: the wall-clock backstop.
  api.state.status = {};
  api.state.routes = [{ ts: nowS - 17 * dayS }, { ts: nowS - 18 * dayS }];
  assert.equal(api.logFreshness().stale, true);
  assert.equal(api.logFreshness().reason, 'age');
  assert.ok(api.logFreshness().days > 16, 'the age is measured in days');

  // Nothing recorded at all.
  api.state.routes = [];
  assert.equal(api.logFreshness().stale, true);
  assert.equal(api.logFreshness().reason, 'empty');
});

test('a window older than the policy demotes every count and says why', () => {
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.loading = false;
  // The measured corpus: 40 hand-typed lines spanning 3h39min of 01/08, under a
  // policy that changed since (config_mtime 17 days after the last decision).
  const t0 = Date.UTC(2026, 7, 1, 22, 12, 11) / 1000;
  const t1 = Date.UTC(2026, 7, 2, 1, 51, 38) / 1000;
  api.state.routes = Array.from({ length: 40 }, (_, i) => ({
    id: `r${i}`, ts: t0 + (t1 - t0) * i / 39,
    cause: 'fail_safe_strong', rule_id: null,
  }));
  api.state.status = { config_mtime: new Date(T - 3600 * 1000).toISOString() };
  api.state.policy = {
    rules: [
      { id: 'hard-verbs', when: { verb_class: { eq: 'hard' } }, then: { model: 'T4' } },
      { id: 'huge-context-read', when: { est_input_tokens: { gt: 400000 } }, then: { model: 'T4' } },
    ],
    default: { action: 'classify' },
    classifier: { model: 'deepseek-v3.2' },
    fail_safe: { model: 'glm-4.7' },
    tiers: { T4: {} },
  };
  api.renderSheet();

  const banner = flat(dom.get('windowStale'));
  assert.match(banner, /Nenhuma decisão há 17 dias/,
    'the banner names how long since the last decision');
  assert.match(banner, /Estas contagens descrevem de 01\/08 22:12 a 02\/08 01:51 UTC, não o presente/,
    'and names the real window, not the present');
  assert.equal(dom.get('windowStale').hidden, false);

  // The counter REFUSES: no row carries a count or an amber "never fired" —
  // even though this corpus would have painted both rules amber before.
  const hits = findAll(dom.get('sheet'), 'step-hits');
  // 2 rules + classifier: no blocklist row, because this policy declares no
  // manual ban (spec 1.3).
  assert.ok(hits.length >= 3, '2 rules + classifier carry hits');
  assert.ok(hits.every((n) => n.textContent === 'sem histórico: o registro de decisões não cobre este período'),
    `every count demoted, got ${JSON.stringify(hits.map((n) => n.textContent))}`);
  assert.ok(hits.every((n) => /empty/.test(n.className)), 'all demoted rows carry the muted class');
  assert.ok(hits.every((n) => !/zero/.test(n.className)), 'no amber zero survives on a stale sheet');
  assert.doesNotMatch(flat(dom.get('sheet')), /never fired/);
  // The fail-safe % would have been computed from a window that predates the
  // rule; stale means the plain sentence, never a percentage about old data.
  assert.doesNotMatch(flat(dom.get('sheet')), /% of the decisions/);
  // The window is still NAMED — by the disclosure banner asserted above, which
  // spells out "de 01/08 22:12 a 02/08 01:51 UTC, não o presente". The single legend
  // that used to sit above the list is gone on purpose: spec §3.3 moved the period
  // into each row's own count, and §1.2 gave #pipelineNote a different job. On a
  // stale sheet the row says what is actually wrong — the record does not cover the
  // period — instead of naming a window the row never showed.
  // The sheet wears the widening class.
  assert.ok(dom.get('sheet').classList.contains('stale'));
});

test('a covering window keeps the amber "never fired" finding', () => {
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.loading = false;
  const nowS = T / 1000;
  const hourS = 3600;
  api.state.routes = [
    { id: 'a', ts: nowS - 3600, cause: 'hard_rule', rule_id: 'hard-verbs' },
    { id: 'b', ts: nowS - 7200, cause: 'hard_rule', rule_id: 'hard-verbs' },
  ];
  api.state.status = { config_mtime: new Date(T - 5 * hourS * 1000).toISOString() };
  api.state.policy = {
    rules: [
      { id: 'hard-verbs', when: {}, then: { model: 'T4' } },
      { id: 'never-caught', when: { keywords: { contains: 'audit' } }, then: { model: 'T4' } },
    ],
    default: {}, classifier: { model: 'm' }, fail_safe: { model: 'm' }, tiers: { T4: {} },
  };
  api.renderSheet();

  assert.equal(dom.get('windowStale').hidden, true,
    'a window covering the current policy shows no disclosure');
  const text = flat(dom.get('sheet'));
  assert.match(text, /2×/, 'a rule that fired keeps its count');
  assert.match(text, /nunca disparou/,
    'a rule that existed and never fired keeps the finding');
  assert.doesNotMatch(text, /sem histórico/);
  assert.doesNotMatch(text, /% of the decisions/);
  // §3.3: the count carries its PERIOD. Two hours of decisions is "nas últimas 2h" —
  // a number with no period reads as a claim about now.
  assert.match(text, /disparou 2× na última hora/, 'the count says what it counted over');
  assert.match(text, /nunca disparou na última hora/, 'and so does the zero');
  // Both zero-hit rows (never-caught, fail-safe) are amber: the window covers the
  // policy, so every zero is a genuine finding. There is no blocklist row — this
  // policy declares no manual ban (spec 1.3).
  const zeros = findAll(dom.get('sheet'), 'step-hits').filter((n) => /zero/.test(n.className));
  assert.equal(zeros.length, 2, 'every zero-hit row stays an amber finding');
  assert.equal(findAll(dom.get('sheet'), 'step-hits').filter((n) => /empty/.test(n.className)).length, 0);
  assert.ok(!dom.get('sheet').classList.contains('stale'));
  // No disclosure banner here (the window covers the policy) and no global legend
  // either — see the note in the stale-window test above. What this window earns is
  // the per-row counts asserted above, each carrying the period it was counted over.
});

test('an empty log demotes every count and says nothing is recorded yet', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [];
  api.state.status = { config_mtime: '2026-08-18T22:40:00Z' };
  api.state.policy = {
    rules: [{ id: 'hard-verbs', when: {}, then: { model: 'T4' } }],
    default: {}, classifier: { model: 'm' }, fail_safe: { model: 'm' }, tiers: { T4: {} },
  };
  api.renderSheet();

  assert.equal(dom.get('windowStale').hidden, false);
  assert.match(flat(dom.get('windowStale')), /Nenhuma decisão registrada ainda/);
  const words = findAll(dom.get('sheet'), 'step-hits').map((n) => n.textContent);
  assert.ok(words.length >= 2, 'every rule-bearing row renders a hits cell');
  assert.ok(words.every((w) => w === 'sem histórico: o registro de decisões não cobre este período'));
  assert.doesNotMatch(flat(dom.get('sheet')), /never fired/);
  // No window exists, so no window is claimed.
  assert.doesNotMatch(flat(dom.get('sheet')), /counts over/);
});

test('an old sidecar without config_mtime still falls back to the age backstop', () => {
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.loading = false;
  api.state.status = {};  // older sidecar: provenance absent
  const t0 = Date.UTC(2026, 7, 1, 22, 12, 11) / 1000;
  const t1 = Date.UTC(2026, 7, 2, 1, 51, 38) / 1000;
  api.state.routes = [{ id: 'a', ts: t0 }, { id: 'b', ts: t1 }];
  api.state.policy = {
    rules: [{ id: 'hard-verbs', when: {}, then: { model: 'T4' } }],
    default: {}, classifier: { model: 'm' }, fail_safe: { model: 'm' }, tiers: { T4: {} },
  };
  api.renderSheet();
  const banner = flat(dom.get('windowStale'));
  assert.match(banner, /Nenhuma decisão há 17 dias/);
  assert.match(banner, /Estas contagens descrevem de 01\/08 22:12 a 02\/08 01:51 UTC, não o presente/);
  assert.doesNotMatch(flat(dom.get('sheet')), /never fired/);
});

test('billing is named in the operator\'s words, and a missing mode is a finding', () => {
  const { api } = loadConsole();
  // router.yaml's own vocabulary (capabilities.BILLING_MODES), because a console
  // that renames plan to "included" makes the YAML unsearchable.
  for (const mode of ['plan', 'subscription', 'metered', 'free']) {
    const badge = api.billingBadge(mode);
    assert.equal(badge.word, mode);
    assert.equal(badge.unknown, false);
    assert.ok(badge.meaning.length > 0, `${mode} must say what it means for cost`);
  }
  assert.match(api.billingBadge('plan').meaning, /sem cobrança por token/);
  assert.equal(api.billingBadge(' PLAN ').word, 'plan', 'whitespace and case are the file\'s, not the fact\'s');

  // An elo whose rail is undeclared cannot be costed, which is the operator's
  // problem and therefore said out loud rather than left as a blank cell.
  const missing = api.billingBadge(undefined);
  assert.equal(missing.unknown, true);
  assert.match(missing.word, /não declarado/);
  // And a mode this console has not learned still renders as written: swallowing
  // it would claim the elo declares nothing when it declares something unknown.
  const odd = api.billingBadge('barter');
  assert.equal(odd.unknown, true);
  assert.equal(odd.word, 'barter');
});

test('a context window is written to be compared, not counted', () => {
  const { api } = loadConsole();
  assert.equal(api.ctxWindow(1000000), '1M');
  assert.equal(api.ctxWindow(1048576), '1M');
  assert.equal(api.ctxWindow(1050000), '1.1M');
  assert.equal(api.ctxWindow(200000), '200K');
  assert.equal(api.ctxWindow(900), '900');
  // Unknown is not zero. A window rendered as "0" would read as an elo that can
  // hold nothing, which is a different and much louder claim.
  assert.equal(api.ctxWindow(0), '');
  assert.equal(api.ctxWindow(undefined), '');
  assert.equal(api.ctxWindow('big'), '');
});

test('a dropped elo says WHY in words, never as a raw enum', () => {
  const { api } = loadConsole();
  const reasons = {
    context_too_small: /janela de contexto dele é menor/,
    no_vision: /não lê imagem/,
    no_tool_calling: /não chama ferramentas/,
    no_structured_output: /não devolve resposta em formato fixo/,
    capability_unknown: /não foram verificadas/,
  };
  for (const [reason, expected] of Object.entries(reasons)) {
    const words = api.rejectWhy(reason);
    assert.match(words, expected, `${reason} must be actionable prose`);
    assert.doesNotMatch(words, /_/, 'an enum leaking through is the whole failure');
  }
  // A reason from a newer router still renders — an unexplained rejection is
  // worse than an awkwardly worded one.
  assert.equal(api.rejectWhy('no_audio_input'), 'no audio input');
  assert.match(api.rejectWhy(''), /não deu motivo/);

  // "Too small" is only actionable next to the two numbers.
  assert.equal(api.contextShortfall(200000, 500000), 'tem 200K, precisa de 500K');
  assert.equal(api.contextShortfall(undefined, 500000), '', 'no invented numbers');
  assert.equal(api.contextShortfall(200000, 0), '');
  // A near-miss must not read as a tie. min_context is ceil(est_input_tokens × 1.25)
  // (capabilities.derive_requirements), so a 840,001-token turn asks 1,050,002 of
  // gpt-5.6-terra's 1,050,000 — and rounded to one decimal both are "1.1M", which put
  // "holds 1.1M, needs 1.1M" beside "its context window is smaller than this task
  // needs": the row denying its own reason. Where the rounding hides the difference,
  // the digits are shown.
  const window = registryFacts('gpt-5.6-terra').context_window;
  assert.equal(window, 1050000, 'the window this comparison is against');
  assert.equal(api.ctxWindow(window), api.ctxWindow(Math.ceil(840001 * 1.25)),
    'both round to the same string, which is the trap');
  assert.equal(api.contextShortfall(window, Math.ceil(840001 * 1.25)),
    'tem 1,050,000, precisa de 1,050,002');
  // And an actual tie is not dressed up as a shortfall with invented digits.
  assert.equal(api.contextShortfall(window, window), 'tem 1.1M, precisa de 1.1M');
});

test('the derived requirements read in the same three families as the rules', () => {
  const { api } = loadConsole();
  const chips = plain(api.requirementChips({ min_context: 500000, vision: true, tool_calling: false }));
  assert.deepEqual(chips, [
    { family: 'context', kind: 'context', text: 'pelo menos 500,000 tokens' },
    { family: 'capability', kind: 'needs', text: 'imagem' },
  ], 'only a TRUE boolean constrains anything, so a false one is not drawn as a requirement');
  // A requirement key this console has not learned is still shown, because an
  // elo rejected by an invisible requirement is unexplainable.
  assert.match(api.requirementChips({ min_audio_seconds: 30 })[0].text, /min audio seconds 30/);
  assert.deepEqual(plain(api.requirementChips(null)), []);
});

test('sequential is ordered and random must not pretend to be', () => {
  const { api } = loadConsole();
  const seq = api.strategyWords('sequential');
  assert.equal(seq.ordered, true);
  assert.match(seq.label, /na ordem escrita/);

  const rand = api.strategyWords('random', { pinPrimary: true });
  assert.equal(rand.ordered, false, 'a set has no first hop, so it gets no ordinals');
  assert.match(rand.label, /sorteada/);
  assert.match(rand.note, /primeira fica fixa/);
  assert.match(api.strategyWords('random', { pinPrimary: false }).note, /todas as tentativas/);

  // capabilities.order_chain degrades an unrecognised strategy to sequential. The
  // console must degrade the same way AND say so: silently drawing a typo'd
  // strategy as a random set would describe routing that never happens.
  const typo = api.strategyWords('shuffled');
  assert.equal(typo.ordered, true);
  assert.match(typo.note, /shuffled/);
  assert.match(typo.note, /não é uma ordem que o roteador conhece/);
  assert.equal(api.strategyWords(undefined).ordered, true);
});

test('redundancy is counted in upstreams, not in vendor names', () => {
  const { api } = loadConsole();
  // Nous Portal resells OpenRouter, so a chain that hops from one to the other
  // survives nothing. Mirrors capabilities._UPSTREAM_GROUPS.
  assert.equal(api.upstreamGroup('nous'), 'openrouter');
  assert.equal(api.upstreamGroup('OpenRouter'), 'openrouter');
  assert.equal(api.upstreamGroup('zai'), 'zai');
  assert.equal(api.upstreamGroup(''), '');

  assert.equal(api.independentRails([{ provider: 'nous' }, { provider: 'openrouter' }]), 1,
    'two names, one upstream, one rail');
  assert.equal(api.independentRails([{ provider: 'zai' }, { provider: 'deepseek' }, { provider: 'xiaomi' }]), 3);
  // An unattributable hop is not evidence of independence.
  assert.equal(api.independentRails([{ provider: 'zai' }, {}]), 1);

  assert.deepEqual(plain(api.sharedUpstream([{ provider: 'nous' }, { provider: 'openrouter' }, { provider: 'zai' }])),
    { group: 'openrouter', first: 1, second: 2 });
  assert.equal(api.sharedUpstream([{ provider: 'zai' }, { provider: 'deepseek' }]), null);
});

// ── the tier chain view ──────────────────────────────────────────────────

function tierPolicy() {
  return {
    rules: [], default: {},
    tiers: {
      T1: {
        model: 'glm-4.7', provider: 'zai', billing_mode: 'plan',
        fallback: [
          { model: 'gpt-5.6-luna', provider: 'openai-codex', billing_mode: 'subscription' },
          { model: 'mimo-v2.5', provider: 'xiaomi', billing_mode: 'metered' },
        ],
        fallback_strategy: 'sequential',
      },
      T2: {
        model: 'glm-5.3', provider: 'zai', billing_mode: 'plan',
        fallback: [
          { model: 'deepseek-v4-pro', provider: 'deepseek', billing_mode: 'metered' },
          { model: 'gpt-5.5', provider: 'openai-codex', billing_mode: 'subscription' },
        ],
        fallback_strategy: 'random', pin_primary: false,
      },
    },
  };
}

test('a sequential chain is numbered down a spine; a random one is neither', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.renderLadder();

  const lists = findAll(dom.get('ladder'), 'hops');
  // T1 is one ordered list; T2 with pin_primary false is one unordered set.
  assert.equal(lists.length, 2);
  assert.match(lists[0].className, /ordered/, 'sequential must LOOK ordered');
  assert.match(lists[1].className, /drawn/, 'random must not imply an order it does not have');

  const ordinals = findAll(lists[0], 'hop-ord').map((n) => n.textContent);  // findAll is ours, so this array is too
  assert.deepEqual(ordinals, ['1', '2', '3'], 'the order it will actually try them');
  assert.equal(findAll(lists[1], 'hop-ord').length, 0,
    'a numbered random chain is a lie about which elo runs first');

  const text = flat(dom.get('ladder'));
  assert.match(text, /na ordem escrita/);
  assert.match(text, /em ordem sorteada/);
  assert.match(text, /todas as tentativas são sorteadas/, 'pin_primary false shuffles the primary too');
  // "top to bottom, every time" beside "tried in order" is one fact written twice
  // (DESIGN.md §2.3), so an ordinary sequential tier carries no second line.
  assert.doesNotMatch(text, /top to bottom/);
});

test('a pinned random chain draws the primary as first and the rest as a set', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  const policy = tierPolicy();
  delete policy.tiers.T2;
  policy.tiers.T1.fallback_strategy = 'random';
  policy.tiers.T1.pin_primary = true;
  api.state.policy = policy;
  api.renderLadder();

  const lists = findAll(dom.get('ladder'), 'hops');
  assert.equal(lists.length, 2, 'the pinned primary and the drawn tail are different KINDS of list');
  assert.match(lists[0].className, /ordered/);
  assert.deepEqual(findAll(lists[0], 'hop-ord').map((n) => n.textContent), ['1']);
  assert.match(lists[1].className, /drawn/);
  assert.equal(findAll(lists[1], 'hop-ord').length, 0);
  assert.equal(findAll(lists[1], 'hop-model').length, 2, 'and the tail holds both fallbacks');
});

test('every elo shows the rail it runs on, how it is billed and what it can hold', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.capabilities = { 'glm-4.7': { context_window: 200000 }, 'gpt-5.6-luna': { context_window: 1000000 } };
  api.renderLadder();

  const text = flat(dom.get('ladder'));
  assert.match(text, /glm-4\.7/);
  assert.match(text, /zai/, 'the rail, because a chain is only as independent as its upstreams');
  assert.match(text, /plan/);
  assert.match(text, /subscription/);
  assert.match(text, /metered/);
  assert.match(text, /200K de contexto/);
  assert.match(text, /1M de contexto/);
  // An elo nothing knows is not a blank cell: it routes UNCHECKED, and the filter
  // can neither clear it nor reject it.
  assert.match(text, /sem capacidade verificada/);
  assert.match(dom.get('ladderNote').textContent, /sem capacidade verificada/,
    'and the group head counts them, so the gap is visible without reading every row');
});

test('a tier with one elo and no fallback says what to do about it', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: { model: 'glm-4.7', provider: 'zai', billing_mode: 'plan' } } };
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /Sem reserva declarada/);
  assert.match(text, /zai/, 'it names the rail whose outage takes the tier down');
  assert.match(text, /Acrescente uma tentativa em outro provedor/, 'an empty state that teaches the next action');
  assert.match(text, /1 provedor independente em 1 tentativa/);
});

test('a chain whose first two hops share an upstream is called out, not counted as two', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [], default: {},
    tiers: {
      T3: {
        model: 'kimi-k3', provider: 'nous',
        fallback: [{ model: 'glm-5.3', provider: 'openrouter' }, { model: 'mimo-v2.5', provider: 'xiaomi' }],
      },
    },
  };
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /2 provedores independentes em 3 tentativas/, 'three vendors, two upstreams');
  assert.match(text, /As tentativas 1 e 2 caem as duas em openrouter/);
  assert.match(text, /Reordene para a 2ª tentativa ficar em outro provedor/);
});

test('no tiers is an instruction, not a blank panel', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.renderLadder();
  assert.match(flat(dom.get('ladder')), /Nenhuma regra pode mandar tarefa para um grupo/);
  assert.equal(dom.get('ladderNote').textContent, '', 'and no note about a chain that does not exist');

  // Unreachable is a different claim from empty, and the console must not make the
  // second while the first is true.
  api.state.unreachable = true;
  api.renderLadder();
  assert.match(flat(dom.get('ladder')), /não for possível falar com o roteador/);
});

// ── the chain plan for a probed task ─────────────────────────────────────

function chainPlan(extra) {
  return Object.assign({
    chain: [
      { model: 'gpt-5.6-terra', provider: 'openai-codex' },
      { model: 'deepseek-v4-pro', provider: 'deepseek', billing_mode: 'metered' },
    ],
    requirements: { min_context: 500000 },
    rejected: [],
    unknown: [],
    bypassed: false,
    strategy: 'sequential',
    // pin_primary is part of the real shape: rules.plan_chain emits it beside
    // `strategy`, because the two together are what decide whether hop 1 is
    // genuinely first. This factory omitted it while a test injected it by hand,
    // which is how the console came to read an absent field as `true` and print
    // "the primary stays first" about a chain whose index 0 had been shuffled.
    pin_primary: true,
    independent_rails: 2,
    // THE FULL SHAPE rules.plan_chain ALWAYS EMITS (see its docstring). Every key
    // below was missing from this factory, and each absence let a test pass against
    // a plan the router cannot produce:
    //   strategy_declared / strategy_degraded_reason — the two fields the degrade
    //     banner has to read. Without them a test could only pin the console's own
    //     re-derivation, which is how "The tier declares “sequential”, but it did
    //     not run" survived four reviews.
    //   multipliers — a mapping, always present, EMPTY without a clock. Absent, it
    //     arrived at eloRow as `null`, which Number() reads as 0, and every hop
    //     rendered "0× cheap window".
    //   capped / demoted / promoted / peak_priced / time_cap_bypassed /
    //     unsatisfiable — the time layer's diagnostics, always emitted, so "no cap
    //     fired" is a reported fact rather than a missing key. `peak_priced` and
    //     `demoted` are two of them and not one: apply_time_policy's own invariant
    //     is set(demoted) <= set(peak_priced), so a plan carrying `demoted` without
    //     `peak_priced` is one the router cannot send.
    strategy_declared: 'sequential',
    strategy_degraded: false,
    strategy_degraded_reason: '',
    unsatisfiable: [],
    time_cap_bypassed: false,
    capped: [],
    demoted: [],
    promoted: [],
    peak_priced: [],
    multipliers: {},
  }, extra || {});
}

test('the chain plan shows the requirements and the order the elos will be tried', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.capabilities = { 'gpt-5.6-terra': { context_window: 1050000 } };
  api.renderChainPlan(chainPlan());

  const box = dom.get('chainPlan');
  assert.deepEqual(findAll(box, 'chip-val').map((n) => n.textContent), ['pelo menos 500,000 tokens']);
  assert.deepEqual(findAll(box, 'hop-model').map((n) => n.textContent),
    ['gpt-5.6-terra', 'deepseek-v4-pro'], 'the order it will really try them');
  assert.deepEqual(findAll(box, 'hop-ord').map((n) => n.textContent), ['1', '2']);
  const text = flat(box);
  assert.match(text, /1\.1M de contexto/);
  assert.match(text, /2 provedores independentes em 2 tentativas elegíveis/);
});

test('a bypassed capability filter is the first thing the panel says', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  // bypassed means the filter disqualified EVERYTHING and overrode itself to keep
  // routing alive: the task runs on a model that cannot meet its own stated
  // requirements. An operator discovering that by accident is the failure.
  api.renderChainPlan(chainPlan({ bypassed: true }));
  const first = dom.get('chainPlan').children[0];
  assert.match(first.className, /warn-line bad/, 'it takes the loudest line this console has');
  const said = String(first.textContent || '') + flat(first);
  assert.match(said, /ignorado/i);
  assert.match(said, /tentar todas mesmo assim/, 'it says what the router will do');
  assert.match(said, /baixe as exigências do grupo/, 'and what the operator can do about it');
});

test('a rejected elo carries the reason and the two numbers behind it', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.capabilities = { 'glm-5-turbo': { context_window: 200000 } };
  api.renderChainPlan(chainPlan({
    rejected: [{ model: 'glm-5-turbo', provider: 'zai', reject_reason: 'context_too_small' }],
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /Tirados da fila \(1\)/);
  assert.match(text, /glm-5-turbo/);
  assert.match(text, /janela de contexto dele é menor do que esta tarefa precisa/);
  assert.match(text, /tem 200K, precisa de 500K/, 'the numbers are what make it fixable');
  assert.doesNotMatch(text, /context_too_small/, 'the enum never reaches the screen');
  assert.match(text, /não está bloqueada nem fora do ar/, 'ineligible is a different condition from unhealthy');
});

test('an unverified elo is named as running unchecked', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.renderChainPlan(chainPlan({ unknown: ['mystery-2'] }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /Sem verificação/);
  assert.match(text, /mystery-2/);
  assert.match(text, /elegíveis por suposição/, 'the filter neither cleared nor rejected it');
  assert.match(text, /router\.yaml/, 'and the fix is named');
});

test('an emptied chain says the filter emptied it instead of showing a blank list', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.renderChainPlan(chainPlan({
    chain: [],
    rejected: [{ model: 'glm-4.7', provider: 'zai', reject_reason: 'no_vision' }],
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /Nenhuma tentativa passou pelo filtro/);
  assert.match(text, /não lê imagem/);
});

test('no requirements is said plainly, and no plan renders nothing at all', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.renderChainPlan(chainPlan({ requirements: {} }));
  assert.match(flat(dom.get('chainPlan')), /Nenhuma exigência de capacidade foi derivada/);

  // DESIGN.md §2.1: a section with no data is absent, never a framed void. A task
  // bound for the classifier has no resolved chain yet.
  api.renderChainPlan(null);
  assert.equal(dom.get('chainPlan').children.length, 0);
  api.renderChainPlan({ chain: [], rejected: [], requirements: {} });
  assert.equal(dom.get('chainPlan').children.length, 0);
});

test('a random chain plan loses its ordinals too, wherever it is read', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.renderChainPlan(chainPlan({ strategy: 'random', pin_primary: false }));
  const lists = findAll(dom.get('chainPlan'), 'hops');
  assert.equal(lists.length, 1);
  assert.match(lists[0].className, /drawn/);
  assert.equal(findAll(dom.get('chainPlan'), 'hop-ord').length, 0);
  assert.match(flat(dom.get('chainPlan')), /sorteada/);
});

test('the primary\'s billing comes from the policy, not from nowhere', () => {
  const { api, dom } = loadConsole();
  // rules._build_chain gives the primary hop only {model, provider}, so without an
  // index the elo an operator reads most often would report its rail as
  // undeclared — false, and about the most important hop in the chain.
  api.state.policy = tierPolicy();
  const index = api.billingIndex(api.state.policy);
  assert.equal(index['glm-4.7'], 'plan');
  assert.equal(index['deepseek-v4-pro'], 'metered');

  api.renderChainPlan(chainPlan({ chain: [{ model: 'glm-4.7', provider: 'zai' }] }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /plan/);
  assert.doesNotMatch(text, /modo de pagamento não declarado/);
});

test('an elo declaring its own capabilities beats the registry', () => {
  const { api } = loadConsole();
  // capabilities_for applies exactly this precedence: `declared` WINS, so an
  // operator correcting a stale window in router.yaml must see THEIR number.
  const registry = { 'glm-5.3': { context_window: 1000000, billing_mode: 'plan' } };
  const stale = api.hopCaps({ model: 'glm-5.3', provider: 'zai' }, registry);
  assert.equal(stale.caps.context_window, 1000000);
  assert.equal(stale.verified, true);

  const corrected = api.hopCaps({ model: 'glm-5.3', provider: 'zai', context_window: 128000 }, registry);
  assert.equal(corrected.caps.context_window, 128000);

  const nothing = api.hopCaps({ model: 'who-knows', provider: 'zai' }, registry);
  assert.equal(nothing.verified, false, 'nothing knows it, so it routes unchecked');
  assert.equal(nothing.caps.context_window, undefined);
});

test('the capability registry is read defensively, because a missing one is normal', () => {
  const { api } = loadConsole();
  assert.equal(api.capabilityRegistry({ missing: true }), null, 'a sidecar without the endpoint is not an error');
  assert.equal(api.capabilityRegistry({ error: true, status: 500 }), null);
  assert.deepEqual(plain(api.capabilityRegistry({ data: { models: { 'glm-4.7': { context_window: 200000 } } } })),
    { 'glm-4.7': { context_window: 200000 } });
  // A flat map is accepted too — the console must not need a shape negotiation to
  // show a context window.
  assert.deepEqual(plain(api.capabilityRegistry({ data: { 'glm-4.7': { context_window: 200000 } } })),
    { 'glm-4.7': { context_window: 200000 } });
  // And a shape nobody agreed on leaves the chain view saying "unverified" rather
  // than throwing on the first tier.
  assert.equal(api.capabilityRegistry({ data: { models: 'soon' } }), null);
  assert.equal(api.capabilityRegistry({ data: [] }), null);
  assert.equal(api.capabilityRegistry(null), null);
});

test('the console builds the chain the router will walk, primary first', () => {
  const { api } = loadConsole();
  // Mirrors rules._build_chain: the tier itself is the primary elo, then its
  // fallback hops in declared order. A chain view that dropped the primary would
  // describe a route that never runs.
  const chain = plain(api.tierChain(tierPolicy().tiers.T1));
  assert.deepEqual(chain.map((hop) => hop.model), ['glm-4.7', 'gpt-5.6-luna', 'mimo-v2.5']);
  assert.equal(chain[0].billing_mode, 'plan');
  assert.deepEqual(plain(api.tierChain({})), [], 'a tier with no model has no chain');
  assert.deepEqual(plain(api.tierChain(null)), []);
});

test('an invalid policy is reported where the operator is, with the first error', () => {
  const { api, dom } = loadConsole();
  // RouterService.explain REFUSES while lint fails, so the probe and the chain
  // plan are dead until it passes. Leaving that in the lint endpoint makes the
  // console look broken instead of the policy.
  api.state.status = { validation_errors: ["tier 'T9': 'fallback_strategy' must be one of sequential, random"] };
  api.renderWarnings();
  const text = flat(dom.get('warnings'));
  assert.match(text, /Não é possível salvar enquanto houver erro\. 1 erro no arquivo\./);
  assert.match(text, /A simulação é recusada/);
  assert.match(text, /fallback_strategy/, 'the first error itself, not a count of errors');
});

test('a shadowed finding carries its definition where the word sits', () => {
  // "shadowed" is the finding whose WORD does not say what it means: the row
  // looks alive, the counts say it never fired, and the fix is to understand
  // that an earlier rule already covers everything it would. The definition
  // rides the line that uses the term — the banner is where the operator
  // decides, and the fix button alone names the shadower, not the condition.
  const { api, dom } = loadConsole();
  api.state.status = shadowStatus();  // validation_errors + error_targets
  api.renderWarnings();
  const text = flat(dom.get('warnings'));
  assert.match(text, /is shadowed by earlier rule/);
  assert.match(text, /nunca roda: uma regra anterior já cobre tudo o que esta cobriria/);
});

test('a non-shadowed error gets no invented definition', () => {
  // A config-level error names no rule, so nothing is defined for it — a
  // definition for a term that is not there is exactly the invented vocabulary
  // DESIGN.md §2.6 forbids.
  const { api, dom } = loadConsole();
  api.state.status = {
    validation_errors: ["tier 'T9': 'fallback_strategy' must be one of sequential, random"],
    error_targets: [null],
  };
  api.renderWarnings();
  assert.doesNotMatch(flat(dom.get('warnings')), /nunca roda/);
});

test('a shadowed finding from an older sidecar (no error_targets) is still defined', () => {
  // The word appears in the message even when the structured target is absent;
  // the definition must not depend on the newest field.
  const { api, dom } = loadConsole();
  api.state.status = { validation_errors: ["rule 'late' is shadowed by earlier rule 'broad'"], error_targets: [] };
  api.renderWarnings();
  assert.match(flat(dom.get('warnings')), /nunca roda/);
});

test('a probe refused by an invalid policy explains which of the two is broken', async () => {
  const { api, dom } = loadConsole({
    fetch: () => Promise.resolve({
      ok: false, status: 400,
      text: () => Promise.resolve(JSON.stringify({ error: 'router policy is invalid' })),
    }),
  });
  api.state.loading = false;
  await api.probe('anything at all');
  const text = flat(dom.get('probeResult'));
  assert.match(text, /não é possível simular enquanto houver erro no arquivo/i);
  assert.match(text, /Corrija os erros nomeados acima/, 'the next action, not a status code');
  assert.equal(dom.get('chainPlan').children.length, 0, 'and no stale plan survives a refused probe');
});

// ── LINT → FIX: the only actionable message becomes an action ──────────
// The defect under test: the one message that asks the operator to DO
// something was dead text, and the one friendly control stayed enabled while
// knowing it would be refused. These tests pin the fix end to end — the
// backend supplies error_targets beside validation_errors, and the console
// must turn the first one into a jump.

function shadowTarget(over) {
  return Object.assign({
    code: 'shadowed', later_index: 6, later_id: 'review-request',
    earlier_index: 0, earlier_id: 'broad',
    message: "rule 'review-request' is shadowed by earlier rule 'broad'",
  }, over || {});
}
function shadowStatus(target) {
  const t = shadowTarget(target);
  return { validation_errors: [t.message], error_targets: [t] };
}
// Three rows for the position/state tests: a shadower, its victim, and a rule
// the lint does not name. The sheet ordinal for index N is N+1, so later_index
// 1 is "regra 2" and 2 is "regra 3".
function rulePolicy() {
  return {
    rules: [
      { id: 'broad', when: {}, then: { model: 'T2' } },
      { id: 'dead', when: {}, then: { model: 'T4' } },
      { id: 'r3', when: {}, then: { model: 'T2' } },
    ],
  };
}
// The message element the inspector appended. The DOM stub keys nodes by the id
// they were CREATED with, so document.getElementById('nodeMsg') answers with an
// unrelated empty node — the real one is found where renderInspector put it.
function inspectorMsg(dom) {
  return dom.get('inspector').children.find((c) => c.id === 'nodeMsg');
}
// wire() is stripped from the harness, so the tab machinery is driven through
// its named function: give querySelectorAll a real table to act on.
function tabWire(dom) {
  const tabs = ['health', 'pipeline', 'routes'].map((name) => {
    const t = dom.get(`tab-${name}`);
    t.dataset.tab = name;
    return t;
  });
  const screens = ['panel-health', 'panel-pipeline', 'panel-routes'].map((name) => dom.get(name));
  dom.document.querySelectorAll = (sel) => sel === '.tab' ? tabs : (sel === '.screen' ? screens : []);
  return { tabs, screens };
}

test('the invalid-policy line grows a jump button when the error names a rule', () => {
  const { api, dom } = loadConsole();
  api.state.status = shadowStatus();
  api.renderWarnings();
  const text = flat(dom.get('warnings'));
  assert.match(text, /Não é possível salvar enquanto houver erro\. 1 erro no arquivo\./);
  assert.match(text, /Ver regra 7/, 'the button names the row by its sheet ordinal (index 6 + 1)');
});

test('a config-level error (no target) stays dead text — no invented button', () => {
  const { api, dom } = loadConsole();
  api.state.status = { validation_errors: ["missing mandatory 'default' routing"], error_targets: [null] };
  api.renderWarnings();
  assert.doesNotMatch(flat(dom.get('warnings')), /Ver regra/, 'no rule exists to jump to');
});

test('an invalid policy disables Route it, says why, and gates the result space', () => {
  const { api, dom } = loadConsole();
  api.state.status = shadowStatus();
  api.renderWarnings();
  const go = dom.get('routeGo');
  assert.equal(go.disabled, true);
  assert.match(go.title, /Não é possível simular enquanto houver erro no arquivo/);
  const gate = flat(dom.get('probeResult'));
  assert.match(gate, /Não é possível simular enquanto houver erro no arquivo\. Corrija o erro:/);
  assert.match(gate, /Ver regra 7/, 'the fix path rides in the result space too');
});

test('a fixed policy re-enables Route it and clears the stale gate', () => {
  const { api, dom } = loadConsole();
  api.state.status = shadowStatus();
  api.renderWarnings();
  assert.equal(dom.get('routeGo').disabled, true);
  assert.notEqual(flat(dom.get('probeResult')), '');

  api.state.status = { validation_errors: [], error_targets: [] };
  api.renderWarnings();
  assert.equal(dom.get('routeGo').disabled, false);
  assert.equal(dom.get('routeGo').title, '');
  assert.equal(flat(dom.get('probeResult')), '', 'the refusal must not outlive the policy');
});

test('a probe refuses locally when the policy is invalid — no round-trip', async () => {
  let called = false;
  const { api, dom } = loadConsole({
    fetch: () => { called = true; return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') }); },
  });
  api.state.loading = false;
  api.state.status = shadowStatus();
  await api.probe('anything at all');
  assert.equal(called, false, 'the refusal is known before asking — Enter cannot bypass a disabled button');
  assert.match(flat(dom.get('probeResult')), /Não é possível simular enquanto houver erro no arquivo/);
  assert.match(flat(dom.get('probeResult')), /Ver regra 7/);
});

test('selectTab flips the aria state and the visible screen', () => {
  const { api, dom } = loadConsole();
  const { tabs, screens } = tabWire(dom);
  api.selectTab('pipeline');
  assert.equal(tabs[1].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[0].getAttribute('aria-selected'), 'false');
  assert.equal(screens[1].classList.contains('active'), true);
  assert.equal(screens[0].classList.contains('active'), false);
});

test('the jump button drives the whole fix path: tab, hit row, inspector, scroll', () => {
  const { api, dom } = loadConsole();
  const { screens } = tabWire(dom);
  // Seven rows so the sheet's ordinal for index 6 really is "regra 7".
  api.state.policy = { rules: [
    { id: 'broad', when: {}, then: { model: 'T2' } },
    { id: 'r2', when: {}, then: { model: 'T2' } },
    { id: 'r3', when: {}, then: { model: 'T2' } },
    { id: 'r4', when: {}, then: { model: 'T2' } },
    { id: 'r5', when: {}, then: { model: 'T2' } },
    { id: 'r6', when: {}, then: { model: 'T2' } },
    { id: 'review-request', when: {}, then: { model: 'T4' } },
  ] };
  api.state.status = shadowStatus();
  api.renderWarnings();
  const line = dom.get('warnings').children[0];
  const btn = line.children.find((c) => c.textContent === 'Ver regra 7');
  assert.ok(btn, 'the warn-line carries the button');
  btn._listeners.click();

  assert.equal(screens[1].classList.contains('active'), true, 'the Pipeline tab is now visible');
  assert.equal(api.state.lintRule, 'review-request');
  const row = dom.get('sheet').children.find((c) => c.dataset.ruleId === 'review-request');
  assert.ok(row, 'the dead row exists on the re-rendered sheet');
  assert.equal(row.classList.contains('hit'), true, 'marked like a probe winner');
  assert.equal(row.getAttribute('aria-current'), 'true');
  assert.equal(api.state.selected, 'rule:review-request', 'the inspector opened on the rule');
  assert.match(flat(dom.get('inspector')), /review-request/);
  assert.deepEqual(plain(row._scrolledTo), { block: 'center' }, 'and the row was scrolled into view');
});

test('a probe that follows clears the lint mark — one accent, one answer', async () => {
  const { api, dom } = loadConsole({
    fetch: () => Promise.resolve({
      ok: true, status: 200,
      text: () => Promise.resolve(JSON.stringify({
        mode: 'deterministic_dry_run', requires_classifier: false,
        decision: { matched_rule_id: 'r2', output: { model: 'gpt-5.6-terra' } },
      })),
    }),
  });
  api.state.loading = false;
  api.state.policy = { rules: [
    { id: 'broad', when: {}, then: { model: 'T2' } },
    { id: 'r2', when: {}, then: { model: 'T2' } },
    { id: 'review-request', when: {}, then: { model: 'T4' } },
  ] };
  // 1. The policy is broken; the operator jumps to the dead row.
  api.state.status = shadowStatus({ later_index: 2 });
  api.renderWarnings();
  dom.get('warnings').children[0]
    .children.find((c) => c.textContent === 'Ver regra 3')._listeners.click();
  assert.equal(api.state.lintRule, 'review-request');

  // 2. The policy is fixed and refreshed; the mark survives the re-render,
  //    like the probe winner's does.
  api.state.status = { validation_errors: [], error_targets: [] };
  api.renderWarnings();
  api.renderSheet();
  assert.equal(
    dom.get('sheet').children.find((c) => c.dataset.ruleId === 'review-request')
      .classList.contains('hit'),
    true, 'the lint answer survives a refresh');

  // 3. A probe asks its own question; the old answer must not compete.
  await api.probe('a new task');
  assert.equal(api.state.lintRule, null);
  const marked = dom.get('sheet').children.filter((c) => c.classList.contains('hit'));
  assert.equal(marked.length, 1, 'exactly one row wears the accent');
  assert.equal(marked[0].dataset.ruleId, 'r2', 'and it is the probe winner');
});

test('the warnings line is sticky, so the fix path cannot scroll out of view', () => {
  const { style } = consoleStyle();
  assert.match(style, /#warnings\s*\{[^}]*?position: sticky; top: 0/,
    'the only actionable message pins to the scrollport top');
  assert.match(style, /#warnings\s*\{[^}]*?background: var\(--bg\)/,
    'and paints the plane, so scrolled content never reads through it');
});

// ── THE FIX, STRUCTURED: POSITION AND STATE WITHOUT TOUCHING `when` ───────
// The amber shadow finding says a later rule can never fire because an earlier
// one matches everything it matches. The only fixes that class needs are order
// and an off-switch, and the structured inspector offered neither — 'routes to'
// and 'profile' were the only fields, and the whole-policy JSON editor was the
// only door. These tests pin the two buttons as a user drives them (through
// the rendered inspector), the one honest constraint (a move button can only
// exist for a rule the lint actually names), and the blocklist row that used
// to promise an editor it does not have.

test('the shadowed rule grows a move button naming its shadower, and the click splices the draft', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = rulePolicy();
  api.state.status = shadowStatus({ later_index: 1, later_id: 'dead', earlier_index: 0, earlier_id: 'broad' });
  api.setMode('editing');
  api.renderInspector({ id: 'rule:dead', name: 'dead', bind: 'rule', ruleIndex: 1 });
  const move = findAll(dom.get('inspector'), 'btn')
    .find((b) => /Mover para antes de/.test(b.textContent || ''));
  assert.ok(move, 'the button exists for the rule the lint names');
  assert.match(move.textContent, /antes de broad/, 'and it names the shadower from error_targets');
  move._listeners.click();
  assert.deepEqual(plain(api.state.draft.rules.map((r) => r.id)), ['dead', 'broad', 'r3'],
    'the dead row now precedes the row that shadowed it');
  assert.equal(move.disabled, true, 'one move per draft — a second click cannot redo it');
  assert.match(inspectorMsg(dom).textContent, /Movido/, 'the message says what changed');
});

test('the move button targets the EARLIEST shadower when several shadow the same rule', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = rulePolicy();
  api.state.status = shadowStatus({ later_index: 2, later_id: 'r3', earlier_index: 1, earlier_id: 'dead' });
  // A second, earlier shadower: moving before the later one would leave this
  // one in front of the rule still, and the finding would survive the move.
  api.state.status.error_targets.push({
    code: 'shadowed', later_index: 2, later_id: 'r3',
    earlier_index: 0, earlier_id: 'broad',
    message: "rule 'r3' is shadowed by earlier rule 'broad'",
  });
  api.setMode('editing');
  api.renderInspector({ id: 'rule:r3', name: 'r3', bind: 'rule', ruleIndex: 2 });
  const move = findAll(dom.get('inspector'), 'btn')
    .find((b) => /Mover para antes de/.test(b.textContent || ''));
  assert.ok(move);
  assert.match(move.textContent, /antes de broad/, 'the EARLIEST shadower is the fix');
  move._listeners.click();
  assert.equal(plain(api.state.draft.rules.map((r) => r.id))[0], 'r3',
    'the row lands before the earliest shadower, resolving every finding at once');
});

test('the disable button turns the rule off in the draft and back on', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = rulePolicy();
  api.state.status = { validation_errors: [], error_targets: [] };
  api.setMode('editing');
  api.renderInspector({ id: 'rule:dead', name: 'dead', bind: 'rule', ruleIndex: 1 });
  const toggle = findAll(dom.get('inspector'), 'btn')
    .find((b) => /Desativar esta regra/.test(b.textContent || ''));
  assert.ok(toggle, 'every rule can be disabled, shadowed or not');
  toggle._listeners.click();
  assert.equal(api.state.draft.rules[1].enabled, false, 'the draft rule carries enabled:false');
  assert.match(toggle.textContent, /Ativar esta regra/, 'the label offers the way back');
  assert.match(inspectorMsg(dom).textContent, /Desativada/);
  toggle._listeners.click();
  assert.equal('enabled' in api.state.draft.rules[1], false,
    're-enabling removes the field — missing means live, per the matcher');
  assert.match(toggle.textContent, /Desativar esta regra/);
});

test('a rule the lint does not name gets no move button — disable is still there', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = rulePolicy();
  api.state.status = shadowStatus({ later_index: 1, later_id: 'dead', earlier_index: 0, earlier_id: 'broad' });
  api.setMode('editing');
  api.renderInspector({ id: 'rule:r3', name: 'r3', bind: 'rule', ruleIndex: 2 });
  const labels = findAll(dom.get('inspector'), 'btn').map((b) => b.textContent || '');
  assert.ok(!labels.some((t) => /Mover/.test(t)),
    'no move button without a finding — the button must not invent a shadower');
  assert.ok(labels.some((t) => /Desativar/.test(t)), 'disable is always available for a rule');
});

test('the blocklist row is not clickable in editing mode — no pointer for a row with no editor', () => {
  const { api, dom } = loadConsole();
  api.state.policy = rulePolicy();
  // The synthetic row is conditional on a manual ban existing (spec 1.3), so the
  // subject of this test has to be declared for the test to have a subject at all.
  api.state.policy.blocklist = { manual_ban: ['glm-4.7'] };
  api.state.status = { validation_errors: [], error_targets: [] };
  api.setMode('editing');
  api.renderSheet();
  const row = dom.get('sheet').children.find((c) => c.dataset.ruleId === '__blocklist');
  assert.ok(row, 'the informational row still renders');
  assert.equal(row.classList.contains('editable'), false,
    'no cursor:pointer promise on a row that cannot edit');
  assert.equal(row._listeners.click, undefined, 'and no click handler to ignore');
});

test('a disabled rule renders marked off on the sheet', () => {
  const { api, dom } = loadConsole();
  api.state.policy = {
    rules: [
      { id: 'broad', when: {}, then: { model: 'T2' } },
      { id: 'dead', enabled: false, when: {}, then: { model: 'T4' } },
    ],
  };
  api.state.status = { validation_errors: [], error_targets: [] };
  api.renderSheet();
  const dead = dom.get('sheet').children.find((c) => c.dataset.ruleId === 'dead');
  assert.ok(dead, 'the disabled row is still on the sheet');
  // The off class is baked into the row's className string by line() — exactly
  // like the rail's state class, so the string is what has to be read: the
  // stub's classList only tracks add()/remove() calls, and asserting through
  // it would pass no matter what the console rendered.
  assert.match(dead.className, /\boff\b/, 'it wears the off state');
  assert.match(flat(dead), /\(desligada\)/,
    'and the marker is visible in the row text');
  const live = dom.get('sheet').children.find((c) => c.dataset.ruleId === 'broad');
  assert.doesNotMatch(live.className, /\boff\b/, 'a live rule does not');
});

test('a fresh probe clears the previous task\'s chain plan before asking', async () => {
  const explain = {
    mode: 'deterministic_dry_run', requires_classifier: false,
    decision: {
      matched_rule_id: 'huge-context',
      output: { model: 'gpt-5.6-terra', provider: 'openai-codex' },
      chain_plan: chainPlan({ unknown: ['gpt-5.6-terra'] }),
    },
  };
  const { api, dom } = loadConsole({
    fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(explain)) }),
  });
  api.state.loading = false;
  api.state.policy = tierPolicy();
  await api.probe('Read the whole repository and summarise it');
  assert.match(flat(dom.get('chainPlan')), /gpt-5\.6-terra/, 'the plan rides on the decision');
  assert.match(flat(dom.get('chainPlan')), /Sem verificação/);
  assert.deepEqual(plain(api.state.chainPlan.requirements), { min_context: 500000 },
    'and it is kept in state, so a refresh re-renders it instead of dropping it');
});

// ── THE SIZE A PREVIEW IS MEASURED FROM ───────────────────────────────────
// Production routes on the text the child really receives (context + goal) and
// this console asked /explain about só a linha do objetivo, so the Explain panel
// showed a plan production never attempts: measured on the shipped policy, a
// 615,059-char composed prompt is 170,850 estimated tokens with a 213,563-token
// min_context floor, while the same goal by itself measures 17 and derives none.
//
// /explain has accepted `prompt_text` all along and reports `preview.sized_from`
// for exactly this case. So the two things these tests pin are the two halves that
// have to agree: the console SENDS the parameter the sidecar READS, and it
// DESCRIBES the answer with the value the service SENT — never with a guess made
// from whether it happened to supply a context.
const SIDECAR_PATH = 'router/one_sidecar.py';
const SERVICE_PATH = 'router/service.py';
// The parameters /explain accepts, read off the loop that validates them, so a
// rename on the server breaks this instead of the operator's probe.
function explainParameters() {
  const source = fs.readFileSync(SIDECAR_PATH, 'utf8');
  const loop = source.match(/for name, value in \(([\s\S]*?)\):/);
  assert.ok(loop, `${SIDECAR_PATH} must still validate /explain's parameters by name`);
  const names = [...loop[1].matchAll(/\("([a-z_]+)", /g)].map((hit) => hit[1]);
  assert.ok(names.includes('task') && names.includes('prompt_text'),
    `/explain must still take a goal and a composed prompt, got ${names.join(', ')}`);
  return names;
}
// The two values `preview.sized_from` can hold, read from the constants the
// service reports them from. There is no third, which is what lets the console
// read the field by NAME rather than inferring the case.
function sizedFromValues() {
  const source = fs.readFileSync(SERVICE_PATH, 'utf8');
  const task = source.match(/_SIZED_FROM_TASK = "([a-z_]+)"/);
  const prompt = source.match(/_SIZED_FROM_PROMPT = "([a-z_]+)"/);
  assert.ok(task && prompt, `${SERVICE_PATH} must still name which text a preview was sized from`);
  return { task: task[1], prompt: prompt[1] };
}

test('a probe sends the composed context, under the name the sidecar reads it by', async () => {
  const names = explainParameters();
  const calls = [];
  const { api, dom } = loadConsole({
    csrfToken: 'host-token',
    fetch: (url, init) => {
      calls.push({ url, init });
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.loading = false;

  // A goal alone stays the historical GET, byte for byte: a link-shaped probe must
  // keep working, and one_sidecar keeps /explain in _GET_ROUTES for that reason.
  await api.probe('Debug a race condition in the cache');
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/explain\?task=Debug%20a%20race%20condition%20in%20the%20cache$/);
  assert.ok(!calls[0].init.method || calls[0].init.method === 'GET', 'a goal needs no body');

  // A context supplied travels in a POST BODY. It has to: http.server refuses a
  // request line over 65536 bytes, and the contexts this exists for run to 600k
  // chars, so a query string cannot carry one at all.
  const context = `Debug a race condition in the cache\n${'x'.repeat(615_000)}`;
  dom.get('probeContext').value = context;
  await api.probe('Debug a race condition in the cache');
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url.replace(/^.*\/sidecar/, ''), '/explain', 'no query string carries a context');
  assert.equal(calls[1].init.method, 'POST');
  const body = JSON.parse(calls[1].init.body);
  // EVERY key the console sends is one the sidecar validates by that name, and the
  // context arrives at full length — a truncated prompt would produce a smaller
  // est_input_tokens and therefore a confidently wrong plan.
  for (const key of Object.keys(body)) {
    assert.ok(names.includes(key), `${key} is not a parameter /explain accepts (${names.join(', ')})`);
  }
  assert.equal(body.task, 'Debug a race condition in the cache');
  assert.equal(body.prompt_text, context, 'the whole composed text, untrimmed');
});

test('the request shape is chosen by whether there is a context, not by taste', () => {
  const { api } = loadConsole();
  // service._resolve_prompt tests `prompt_text or task`, so an EMPTY box means
  // "size it from the goal" on both sides — and the console must not send an empty
  // prompt_text, which would be the same request in a shape a GET-only proxy
  // cannot answer.
  for (const empty of ['', null, undefined]) {
    const req = api.explainRequest('rename a variable', empty);
    assert.match(req.path, /^\/explain\?task=rename%20a%20variable$/);
    assert.deepEqual(plain(req.options), {});
  }
  // Whitespace is NOT empty and is NOT trimmed: the router measures whatever the
  // turn really carries, and stripping here would report fewer characters than
  // production sends.
  const spaces = api.explainRequest('rename a variable', '   \n  ');
  assert.equal(spaces.options.method, 'POST');
  assert.equal(spaces.options.body.prompt_text, '   \n  ');
});

test('a probe says which text the plan was sized from, in the service\'s own words', async () => {
  const sized = sizedFromValues();
  // The numbers service.explain really returns for "Debug a race condition in the
  // cache" carrying a 615,059-char composed prompt, against the shipped router.yaml
  // — trimmed to the fields this panel reads. The same goal on its own measures 35
  // chars and 10 tokens, which is the test below.
  const explain = {
    mode: 'deterministic_dry_run', requires_classifier: false,
    decision: {
      matched_rule_id: 'hard-verbs',
      output: { model: 'gpt-5.6-terra', provider: 'openai-codex' },
    },
    features: { est_input_tokens: 170850, char_len: 615059 },
    preview: { sized_from: sized.prompt, prompt_chars: 615059 },
  };
  const { api, dom } = loadConsole({
    csrfToken: 'host-token',
    fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(explain)) }),
  });
  api.state.loading = false;
  api.state.policy = { rules: [{ id: 'hard-verbs', then: { model: 'T4' } }], tiers: { T4: {} } };
  dom.get('probeContext').value = 'x'.repeat(615_059);
  await api.probe('Debug a race condition in the cache');

  const text = flat(dom.get('probeResult'));
  assert.match(text, /medido a partir do contexto que você informou/);
  // Both numbers, each from the surface that owns it: the length of the text
  // measured, and the token count the rules were evaluated against.
  assert.match(text, /615,059 caracteres/);
  assert.match(text, /170,850 tokens estimados/);
  assert.doesNotMatch(text, /goal line alone/, 'it was not sized from the goal');
});

test('a preview sized from the goal line says so, because it reads as production\'s plan', async () => {
  const sized = sizedFromValues();
  // The SAME goal with no context: 10 estimated tokens instead of 170,850, and a
  // min_context floor that came from the TIER rather than from the turn. This is the
  // case the line exists for — on screen it is indistinguishable from the plan the
  // real turn gets, and it is the one an operator reads as production's.
  const explain = {
    mode: 'deterministic_dry_run', requires_classifier: false,
    decision: { matched_rule_id: 'hard-verbs', output: { model: 'gpt-5.6-terra', provider: 'openai-codex' } },
    features: { est_input_tokens: 10, char_len: 35 },
    preview: { sized_from: sized.task, prompt_chars: 35 },
  };
  const { api, dom } = loadConsole({
    fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(explain)) }),
  });
  api.state.loading = false;
  api.state.policy = { rules: [{ id: 'hard-verbs', then: { model: 'T4' } }], tiers: { T4: {} } };
  await api.probe('Debug a race condition in the cache');

  const text = flat(dom.get('probeResult'));
  assert.match(text, /medido a partir da linha do objetivo apenas — 35 caracteres, 10 tokens estimados\./);
  assert.match(text, /cole o contexto acima/, 'and it names what to do about it');

  // A sidecar that reports no preview at all is not described: "sized from the
  // goal line" would be a claim about a field nobody sent.
  assert.equal(api.sizedFromWords(undefined, { est_input_tokens: 17 }), null);
  assert.equal(api.sizedFromWords({ sized_from: 'something_new' }, {}), null,
    'and a value the service does not define is not guessed at either');
  // A preview that named the text but no size still says which text it was.
  const partial = api.sizedFromWords({ sized_from: sized.prompt }, {});
  assert.equal(partial.said, 'do contexto que você informou.', 'no invented characters, no invented tokens');
  // And a null size is not zero: `Number(null)` is 0, so a coerced one would read
  // as a measured "0 characters" — a number nobody sent.
  assert.equal(api.sizedFromWords({ sized_from: sized.prompt, prompt_chars: null },
                                  { est_input_tokens: null }).said,
               'do contexto que você informou.');
});

test('a context that cannot be POSTed is refused before it becomes an HTTP status', async () => {
  // A POST through the host's proxy carries the CSRF token only the Hermes One
  // shell hands out (the same obstacle writable() names for a write). Learning that
  // as "the dry run is unavailable on this sidecar" would send the operator to look
  // at the sidecar, which is not where the problem is.
  const calls = [];
  const { api, dom } = loadConsole({
    fetch: (url) => {
      calls.push(url);
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.loading = false;
  dom.get('probeContext').value = 'a composed context, and no token to POST it with';
  await api.probe('Add a retry to the cache client');
  assert.equal(calls.length, 0, 'nothing is sent that cannot be answered');
  const text = flat(dom.get('probeResult'));
  assert.match(text, /Abra o Hermes One e volte aqui pelo menu lateral/);
  assert.match(text, /limpe o contexto/, 'and the other way out is named too');
});

test('an input the server refuses is quoted, not restated as a number', async () => {
  // service._resolve_prompt is the only authority for the prompt ceiling
  // (_DEFAULT_MAX_PROMPT_CHARS). A console that pre-checked it with its own copy of
  // 1048576 would be a second bound that can drift from the one being enforced, so
  // the server's own sentence is what the operator reads.
  const source = fs.readFileSync(SERVICE_PATH, 'utf8');
  assert.match(source, /_DEFAULT_MAX_PROMPT_CHARS = 1_048_576/,
    'the ceiling lives in the service, and this test knows only that it lives there');
  const html = fs.readFileSync(sourcePath, 'utf8');
  assert.doesNotMatch(html, /1048576|1_048_576|1048_576/, 'the console must not carry a copy of it');

  const { api, dom } = loadConsole({
    csrfToken: 'host-token',
    fetch: () => Promise.resolve({
      ok: false, status: 400,
      text: () => Promise.resolve(JSON.stringify({ error: 'prompt_text exceeds 1048576 characters' })),
    }),
  });
  api.state.loading = false;
  dom.get('probeContext').value = 'x'.repeat(64);
  await api.probe('Add a retry to the cache client');
  const text = flat(dom.get('probeResult'));
  assert.match(text, /prompt_text exceeds 1048576 characters/, 'the server said what it refused');
  assert.doesNotMatch(text, /unavailable on this sidecar/, 'the sidecar answered — it refused the input');
});

// ── the time layer ────────────────────────────────────────────────────────
// Three of the rails this router uses price by wall-clock window and the swing
// is 2x, which is large enough to decide where a task goes. Everything below is
// pinned for one reason above all others: THE CLOCK IS A PARAMETER. Not one of
// these functions reads the wall clock, so not one of these tests passes at
// 05:00 UTC and fails at 07:00 — which is exactly the failure the router's own
// injected-clock contract exists to prevent, and a console that read `new Date()`
// inside its formatters would have re-introduced it on the operator's screen.
//
// The window values are the vendors' own, verified:
//   deepseek 2.0x at 01:00-04:00 and 06:00-10:00 UTC, EVERY day
//   zai      2.0x at 06:00-10:00 UTC, MON-FRI only
//   xiaomi   0.8x at 16:00-00:00 UTC — a CHEAP window, not a peak
// Hours are half-open [start, end), so hour 10 already bills at base.

// A Monday inside the overlapping deepseek+zai peak, in the middle of an hour so
// the minute formatting is exercised too.
const PEAK = new Date(Date.UTC(2026, 7, 17, 7, 14));
// The same hour on a Saturday: deepseek still doubles, zai does not.
const WEEKEND = new Date(Date.UTC(2026, 7, 22, 7, 0));
// Inside xiaomi's night discount, outside everything else.
const NIGHT = new Date(Date.UTC(2026, 7, 17, 18, 0));

// ── the registry's declarations, READ OUT OF THE RUNNING PATH ─────────────
// These fixtures used to be hand-written, and one of them was hand-written wrong
// in the direction that hid a bug: a `{provider: 'deepseek'}` elo with no windows
// of its own was handed the RAIL's two peak windows by eloWindows(), so a test
// asserting "2× at 07:00" passed for an elo whose registry entry says nothing of
// the kind. So the facts are now READ from router/capabilities.py — the same
// literals capabilities.price_multiplier prices with and GET /capabilities serves
// — and a registry edit that moves a window can no longer leave these green
// against a stale number.
const REGISTRY_PATH = 'router/capabilities.py';
function registryFacts(model) {
  const source = fs.readFileSync(REGISTRY_PATH, 'utf8');
  const start = source.indexOf(`\n    "${model}": {`);
  assert.ok(start > 0, `${model} must be a real entry in ${REGISTRY_PATH}`);
  // Entries sit at one indent inside MODEL_CAPABILITIES, so the first `\n    },`
  // after the key is this entry's own close and never an inner list's.
  const entry = source.slice(start, source.indexOf('\n    },', start));
  const number = (key) => {
    const hit = entry.match(new RegExp(`"${key}": (None|[\\d.]+)`));
    return hit ? (hit[1] === 'None' ? null : Number(hit[1])) : undefined;
  };
  const windows = [...entry.matchAll(
    /\{"hours_utc": \[(\d+), (\d+)\](?:, "weekdays": \[([\d, ]+)\])?, "multiplier": ([\d.]+)\}/g,
  )].map((match) => {
    const window = { hours_utc: [Number(match[1]), Number(match[2])], multiplier: Number(match[4]) };
    if (match[3]) window.weekdays = match[3].split(',').map((day) => Number(day.trim()));
    return window;
  });
  return {
    provider: (entry.match(/"provider": "([^"]+)"/) || [])[1],
    billing_mode: (entry.match(/"billing_mode": "([^"]+)"/) || [])[1],
    context_window: Number(String((entry.match(/"context_window": ([\d_]+)/) || [])[1]).replace(/_/g, '')),
    price_in: number('price_in'),
    price_out: number('price_out'),
    price_windows: windows,
  };
}
// One elo exactly as GET /capabilities serves it (router/service.py:capabilities):
// the allowlisted registry fields, plus `price_published` — which service.py
// answers by asking capabilities.effective_price, the function cheapest_now ranks
// on, so the catalogue cannot disagree with the ordering the console audits.
function catalogueEntry(model) {
  const facts = registryFacts(model);
  const entry = {
    provider: facts.provider,
    billing_mode: facts.billing_mode,
    context_window: facts.context_window,
    price_in: facts.price_in === undefined ? null : facts.price_in,
    price_out: facts.price_out === undefined ? null : facts.price_out,
    price_published: typeof facts.price_in === 'number' && typeof facts.price_out === 'number',
  };
  if (facts.price_windows.length) entry.price_windows = facts.price_windows;
  return entry;
}
// ── WHICH UNIT A RAIL IS PRICED IN, read out of the running path ──────────
// `cheapest_now`'s buckets and a `time_cap`'s ceiling key on the SAME table:
// capabilities._BILLING_RANK. apply_time_cap removes an elo only out of the
// dollars bucket and reports every other mode as `cap_exempt`, left in the chain.
// So the table is parsed from capabilities.py rather than restated here: a console
// that describes the cap as evicting a plan rail then fails against the module
// that does the evicting, which is the disagreement three reviews missed. The
// bucket name is read from apply_time_cap's own comparison, so moving
// `subscription` out of dollars breaks this test instead of the operator's mental
// model.
function billingUnits() {
  const source = fs.readFileSync(REGISTRY_PATH, 'utf8');
  const start = source.indexOf('_BILLING_RANK: Dict[str, int] = {');
  assert.ok(start > 0, `_BILLING_RANK must still be the unit table in ${REGISTRY_PATH}`);
  const body = source.slice(start, source.indexOf('}', start));
  const modes = [...body.matchAll(/"([a-z_]+)": (_BUCKET_[A-Z_]+)/g)]
    .map((hit) => ({ mode: hit[1], bucket: hit[2] }));
  assert.ok(modes.length >= 4, 'every billing mode the router knows must appear in the table');
  const cap = source.slice(source.indexOf('def apply_time_cap'));
  const dollars = (cap.match(/_BILLING_RANK\.get\(mode\) == (_BUCKET_[A-Z_]+)/) || [])[1];
  assert.ok(dollars, 'apply_time_cap must still name the bucket it is allowed to remove from');
  const credits = (body.match(/"plan": (_BUCKET_[A-Z_]+)/) || [])[1];
  return {
    modes,
    // What the cap may remove, and what it may only report.
    removable: modes.filter((m) => m.bucket === dollars).map((m) => m.mode),
    exempt: modes.filter((m) => m.bucket !== dollars).map((m) => m.mode),
    // The credits bucket specifically: a published dollar rate on one of these is
    // a list price nobody is invoiced for.
    inCredits: modes.filter((m) => m.bucket === credits).map((m) => m.mode),
    dollarsBucket: dollars,
  };
}
// The whole catalogue for a set of models, in the endpoint's own envelope.
const catalogue = (...models) => ({
  data: {
    models: models.reduce((all, model) => Object.assign(all, { [model]: catalogueEntry(model) }), {}),
    unknown_models: [], warnings: [], registry_available: true, time_agnostic: true,
  },
});

test('the clock is read as the router reads it: UTC hour, and Monday is 0', () => {
  const { api } = loadConsole();
  assert.deepEqual(plain(api.whenOf(PEAK)), { hour: 7, weekday: 0 },
    'JS counts weekdays from Sunday and the router counts from Monday; converting once is the whole point');
  assert.deepEqual(plain(api.whenOf(WEEKEND)), { hour: 7, weekday: 5 }, 'Saturday');
  // Junk is not an hour. A guessed clock is worse than none, because every window
  // below would then be reported against it.
  assert.equal(api.whenOf(null), null);
  assert.equal(api.whenOf('07:00'), null);
});

test('both clocks are labelled, because a bare 07:14 is ambiguous by the offset', () => {
  const { api } = loadConsole();
  // The windows are published in UTC and the operator lives in UTC−03, so an
  // unlabelled time is wrong by exactly the difference between "the peak is on"
  // and "the peak is three hours away". The offset is INJECTED, so this test says
  // the same thing in every timezone a developer runs it in.
  const face = api.timeFace(PEAK, -180);
  assert.equal(face.utc, '07:14 UTC');
  assert.equal(face.local, '04:14 local');
  assert.equal(face.zone, 'UTC−03:00');
  // East of UTC, and across midnight — where an unlabelled clock is most wrong.
  assert.equal(api.timeFace(NIGHT, 480).local, '02:00 local', 'UTC+8 rolls into the next day');
  assert.equal(api.timeFace(NIGHT, 480).zone, 'UTC+08:00');
  assert.equal(api.timeFace(PEAK, 0).zone, 'UTC+00:00');
  assert.equal(api.timeFace(null, 0), null);
});

test('a declared window is read from either spelling, and junk is read as flat', () => {
  const { api } = loadConsole();
  // The declared form the spec adds.
  assert.deepEqual(plain(api.entryWindows({
    price_windows: [{ hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2 }],
  })), [{ hours: [6, 10], multiplier: 2, weekdays: [0, 1, 2, 3, 4] }]);
  // And the pair the registry already carried for deepseek before windows were
  // generalised — dropping it would silently un-price the busiest metered rail.
  assert.deepEqual(plain(api.entryWindows({
    peak_windows_utc: [[1, 4], [6, 10]], peak_multiplier: 2,
  })), [
    { hours: [1, 4], multiplier: 2, weekdays: null },
    { hours: [6, 10], multiplier: 2, weekdays: null },
  ]);
  // A shape nobody agreed on must leave the console reporting FLAT pricing, not
  // throwing on the first elo: a sidecar's payload is not this console's to trust.
  for (const junk of [null, {}, { price_windows: 'soon' }, { price_windows: [{ hours_utc: [10, 4], multiplier: 2 }] },
                      { price_windows: [{ hours_utc: [1, 4] }] }, { peak_windows_utc: [[1, 4]] }]) {
    assert.deepEqual(plain(api.entryWindows(junk)), [], `${JSON.stringify(junk)} is not a window`);
  }
});

// ── A MALFORMED WINDOW, PRICED BY BOTH SIDES ──────────────────────────────
// The console's window validation was the LAXER of the two, so JS and Python
// priced the same malformed declaration differently — a display contradicting the
// run on precisely the inputs an operator is trying to diagnose. Measured before
// the fix: `{hours_utc: [16.5, 24]}` at hour 17 gave the router 1.0 and this
// console 0.8; `[-1, 5]` at hour 0 gave the router 1.0 and this console 3.0; an
// unparseable `weekdays` became null here and null means EVERY DAY, while
// _multiplier_at skipped the window outright.
//
// So each shape below is priced ONCE BY THE ROUTER and once by the console, and
// the two answers are compared. capabilities.py imports only
// {__future__, datetime, random, typing} — no yaml, no IO — which is what makes it
// callable straight from here; asserting the console's own answer alone is exactly
// how the divergence survived a green suite.
function pythonBin() {
  const { execFileSync } = require('node:child_process');
  for (const bin of ['python3.11', 'python3']) {
    try {
      execFileSync(bin, ['-c', 'pass'], { stdio: 'ignore' });
      return bin;
    } catch (_) { /* try the next candidate */ }
  }
  return assert.fail('no python3 on PATH: this test prices each window with capabilities.py itself');
}
// One process for the whole table. `declared` overrides the registry's windows,
// exactly as the console's eloWindows reads them, and glm-4.6 is the elo that
// declares none of its own — so the window under test is the only one in play.
// 2026-08-17 is a Monday, so `17 + weekday` lands on the weekday asked for.
function routerMultipliers(cases) {
  const { execFileSync } = require('node:child_process');
  const script = [
    'import json, sys',
    'from datetime import datetime',
    'from router import capabilities',
    'out = []',
    'for window, hour, weekday in json.loads(sys.argv[1]):',
    '    when = datetime(2026, 8, 17 + weekday, hour)',
    '    out.append(capabilities.price_multiplier(',
    '        "glm-4.6", when, declared={"price_windows": [window]}))',
    'print(json.dumps(out))',
  ].join('\n');
  const payload = JSON.stringify(cases.map((c) => [c.window, c.at.hour, c.at.weekday]));
  return JSON.parse(execFileSync(pythonBin(), ['-c', script, payload], { encoding: 'utf8' }));
}

test('a window the router refuses is priced flat here too, never reinterpreted', () => {
  const { api } = loadConsole();
  const cases = [
    // The well-formed control: xiaomi's own cheap window, and the answer both
    // sides must give for it.
    { why: 'a whole-hour window prices its own hours', window: { hours_utc: [16, 24], multiplier: 0.8 }, at: { hour: 17, weekday: 0 } },
    { why: 'and hour 24 is a legal end, so a full day is one entry', window: { hours_utc: [0, 24], multiplier: 2 }, at: { hour: 0, weekday: 0 } },
    // A FRACTIONAL hour is a typo, not a boundary: truncating [16.5, 24) to
    // [16, 24) would start the window half an hour before the one written.
    { why: 'a fractional start is refused, not truncated', window: { hours_utc: [16.5, 24], multiplier: 0.8 }, at: { hour: 17, weekday: 0 } },
    { why: 'and a string that is not a whole hour is refused with it', window: { hours_utc: ['16.0', 24], multiplier: 0.8 }, at: { hour: 17, weekday: 0 } },
    // Out of range, both ends. 0 <= start < end <= 24, and nothing else.
    { why: 'a negative hour is not an hour', window: { hours_utc: [-1, 5], multiplier: 3 }, at: { hour: 0, weekday: 0 } },
    { why: 'and neither is 25', window: { hours_utc: [6, 25], multiplier: 2 }, at: { hour: 20, weekday: 0 } },
    { why: 'start > end is refused rather than read as wrapping midnight', window: { hours_utc: [10, 4], multiplier: 2 }, at: { hour: 2, weekday: 0 } },
    // A weekday gate that is PRESENT and unusable must not become every day.
    { why: 'an unparseable weekday drops the window, it does not open it', window: { hours_utc: [6, 10], weekdays: ['Mon'], multiplier: 2 }, at: { hour: 7, weekday: 0 } },
    { why: 'a fractional weekday is a typo too', window: { hours_utc: [6, 10], weekdays: [0, 1.5], multiplier: 2 }, at: { hour: 7, weekday: 0 } },
    { why: 'an empty gate is malformed, not "every day"', window: { hours_utc: [6, 10], weekdays: [], multiplier: 2 }, at: { hour: 7, weekday: 5 } },
    { why: 'and a gate that is not a list at all is malformed', window: { hours_utc: [6, 10], weekdays: 7, multiplier: 2 }, at: { hour: 7, weekday: 0 } },
    { why: 'a weekday out of 0..6 is refused', window: { hours_utc: [6, 10], weekdays: [7], multiplier: 2 }, at: { hour: 7, weekday: 0 } },
    // What a usable gate does: Monday-only inside, weekend outside.
    { why: 'a real gate matches the day it names', window: { hours_utc: [6, 10], weekdays: [0, 0, 1], multiplier: 2 }, at: { hour: 7, weekday: 0 } },
    { why: 'and only the day it names', window: { hours_utc: [6, 10], weekdays: [0, 1], multiplier: 2 }, at: { hour: 7, weekday: 5 } },
    // The multiplier, by the same rules _as_float applies.
    { why: 'a numeric string is a multiplier', window: { hours_utc: ['6', '10'], multiplier: '2' }, at: { hour: 7, weekday: 0 } },
    { why: 'nan is not — it would compare false against every threshold', window: { hours_utc: [6, 10], multiplier: 'nan' }, at: { hour: 7, weekday: 0 } },
    { why: 'and neither is zero or less', window: { hours_utc: [6, 10], multiplier: 0 }, at: { hour: 7, weekday: 0 } },
  ];
  const router = routerMultipliers(cases);
  cases.forEach((c, i) => {
    const mine = api.priceMultiplier(api.entryWindows({ price_windows: [c.window] }), c.at);
    assert.equal(mine, router[i],
      `${c.why}: capabilities.price_multiplier says ${router[i]} for `
      + `${JSON.stringify(c.window)} at hour ${c.at.hour} weekday ${c.at.weekday}, the console says ${mine}`);
  });
  // The two the finding measured, spelled out — so a regression names the number
  // rather than only the disagreement.
  assert.equal(router[2], 1, '[16.5, 24] is priced flat by the router');
  assert.equal(router[4], 1, 'and so is [-1, 5]');
  assert.equal(router[0], 0.8, 'while the well-formed window still prices 0.8×');
});

test('a refused window is DROPPED, so a rail with only one reads as untimed', () => {
  const { api } = loadConsole();
  // Dropping rather than keeping-and-ignoring is what makes the other three
  // readers agree with the router: windowWords, nextWindowChange and the clock
  // line's rows all answer from this list, and a window kept with a neutral
  // multiplier would produce "base rate until 06:00, then 1×" for a window the
  // router cannot see at all.
  for (const window of [
    { hours_utc: [16.5, 24], multiplier: 0.8 },
    { hours_utc: [-1, 5], multiplier: 3 },
    { hours_utc: [6, 10], weekdays: ['Mon'], multiplier: 2 },
    { hours_utc: [6, 10], weekdays: [], multiplier: 2 },
    // `Number(true)` is 1, so this used to become a real window with a neutral
    // multiplier: invisible to priceMultiplier and visible to everything else.
    { hours_utc: [6, 10], multiplier: true },
    { hours_utc: [6, 10], multiplier: 'soon' },
    { hours_utc: [6, 10, 14], multiplier: 2 },
    { hours_utc: '6-10', multiplier: 2 },
  ]) {
    assert.deepEqual(plain(api.entryWindows({ price_windows: [window] })), [],
      `${JSON.stringify(window)} is not a window the router would honour`);
    assert.equal(api.windowWords(api.entryWindows({ price_windows: [window] }), { hour: 7, weekday: 0 }),
      'sem preço que varia com a hora', 'and the words say so, exactly as they do for a flat rail');
  }
  // A window BESIDE a malformed one still prices: the bad entry is skipped, not
  // the declaration — _multiplier_at walks past it to the next candidate.
  assert.equal(api.priceMultiplier(api.entryWindows({
    price_windows: [{ hours_utc: [16.5, 24], multiplier: 3 }, { hours_utc: [16, 24], multiplier: 0.8 }],
  }), { hour: 17, weekday: 0 }), 0.8);
});

test('the hour and weekday bounds are the router\'s own, read from its module', () => {
  const { api } = loadConsole();
  const source = fs.readFileSync(REGISTRY_PATH, 'utf8');
  // Restating 24 and 7 here would be a third copy of the two numbers; they are
  // read from the module whose validators these transcribe.
  const hours = Number((source.match(/_HOURS_IN_DAY = (\d+)/) || [])[1]);
  const days = Number((source.match(/_DAYS_IN_WEEK = (\d+)/) || [])[1]);
  assert.ok(hours > 0 && days > 0, `${REGISTRY_PATH} must still declare both bounds`);

  assert.deepEqual(plain(api.hourBounds([0, hours])), [0, hours], 'the whole day is in range');
  assert.equal(api.hourBounds([0, hours + 1]), null, 'and one hour past it is not');
  assert.equal(api.hourBounds([hours, hours]), null, 'a zero-width window is not a window');
  assert.equal(api.hourBounds([6]), null, 'a window has two bounds');
  assert.equal(api.hourBounds([6, 10, 14]), null, 'and only two');
  // 16.0 names hour 16 exactly — YAML's number parsing decided it was a float, not
  // the operator — which is the one float _as_whole_number accepts.
  assert.deepEqual(plain(api.hourBounds([16.0, 24])), [16, 24]);
  assert.deepEqual(plain(api.hourBounds(['16', '24'])), [16, 24]);
  assert.equal(api.hourBounds([true, 24]), null, 'true is not an hour');

  // THREE outcomes, because "absent" and "malformed" are different claims: absent
  // is every day, malformed is a window nobody can honour.
  assert.equal(api.weekdaySet(undefined), null, 'absent means every day');
  assert.equal(api.weekdaySet(null), null);
  assert.deepEqual(plain(api.weekdaySet([days - 1])), [days - 1], 'the last day is in range');
  assert.equal(api.weekdaySet([days]), false, 'and one past it is not');
  assert.equal(api.weekdaySet([]), false, 'an empty gate is malformed, like _weekday_set says');
  assert.deepEqual(plain(api.weekdaySet([1, 1, 0])), [1, 0], 'and a repeated day is one day');
});

test('an elo with no window of its own is priced flat, never at its rail\'s peak', () => {
  const { api } = loadConsole();
  // N9. glm-4.6 is on zai, and zai peaks 2× at 06:00-10:00 UTC Mon-Fri — but
  // glm-4.6 declares NO price_windows, so capabilities.price_multiplier('glm-4.6',
  // Monday 07:00) is 1.0 and its effective price never leaves (0.60, 2.20). The
  // console said "2× peak · $1.20 in / $4.40 out per 1M" about it, because
  // eloWindows() fell back to the rail. A vendor's peak is a fact about the
  // vendor's windowed models, not about every id it serves.
  const flat = registryFacts('glm-4.6');
  assert.deepEqual(flat.price_windows, [], 'the registry gives glm-4.6 no window');
  assert.equal(api.eloWindows(catalogueEntry('glm-4.6')).length, 0);
  assert.equal(api.priceMultiplier(api.eloWindows(catalogueEntry('glm-4.6')), { hour: 7, weekday: 0 }), 1,
    'flat at the hour its rail doubles — the same answer capabilities.price_multiplier gives');
  const words = api.priceWords(catalogueEntry('glm-4.6'), 1, 'metered', true);
  assert.equal(words, '$0.60 entrada / $2.20 saída por 1M', 'and the price it renders is the base rate, undoubled');
  assert.doesNotMatch(words, /peak/);

  // An elo that DOES declare one keeps it, and it is the registry's own.
  const windowed = api.eloWindows(catalogueEntry('glm-4.7'));
  assert.deepEqual(plain(windowed), [{ hours: [6, 10], multiplier: 2, weekdays: [0, 1, 2, 3, 4] }]);
  assert.equal(api.priceMultiplier(windowed, { hour: 7, weekday: 0 }), 2);
  // A rate declared on the elo in router.yaml still wins over the registry's.
  assert.deepEqual(plain(api.eloWindows({ provider: 'deepseek', price_windows: [{ hours_utc: [0, 2], multiplier: 3 }] })),
    [{ hours: [0, 2], multiplier: 3, weekdays: null }]);

  // WHICH VENDOR IS EXPENSIVE is still said, once, where the claim is about the
  // vendor and is therefore true: the clock line's own rows.
  const zai = plain(api.railWindowRows(null, { hour: 7, weekday: 0 })).find((row) => row.rail === 'zai');
  assert.equal(zai.multiplier, 2, 'the rail claim survives; only the per-elo one was false');
});

test('the multiplier is 1.0 whenever the clock was not supplied', () => {
  const { api } = loadConsole();
  // The spec's fail-direction: no clock means no time-based pricing, never a
  // guessed hour. A console that defaulted to `new Date()` here would report a
  // peak on a plan the router made time-agnostically.
  const deepseek = api.eloWindows(catalogueEntry('deepseek-v4-flash'));
  assert.equal(api.priceMultiplier(deepseek, null), 1);
  assert.equal(api.priceMultiplier(deepseek, {}), 1);
  assert.equal(api.priceMultiplier(deepseek, { hour: 'seven' }), 1);
});

test('an overlap resolves the way the router resolves it, not the other way', () => {
  const { api } = loadConsole();
  // N10. Overlapping windows are a lint error, so neither side is a resolution
  // POLICY — both are determinism guarantees for a malformed registry. But the two
  // fallbacks must still agree, and they did not: capabilities._multiplier_at
  // returns on its FIRST match while this console accumulated, so for the pair
  // below the router priced hour 9 at 2.0 and the console displayed 3.0.
  const overlapping = api.entryWindows({
    price_windows: [{ hours_utc: [6, 10], multiplier: 2 }, { hours_utc: [8, 12], multiplier: 3 }],
  });
  assert.equal(api.priceMultiplier(overlapping, { hour: 9, weekday: 0 }), 2,
    'the first matching window wins, exactly as capabilities._multiplier_at does');
  assert.equal(api.priceMultiplier(overlapping, { hour: 7, weekday: 0 }), 2, 'only the first matches here');
  assert.equal(api.priceMultiplier(overlapping, { hour: 11, weekday: 0 }), 3, 'only the second matches here');

  // Read off the running path rather than remembered: _multiplier_at returns
  // INSIDE its loop, which is what makes it first-match-wins. An edit that made it
  // accumulate would flip the router without touching this console, and this is the
  // assertion that fails then.
  const source = fs.readFileSync(REGISTRY_PATH, 'utf8');
  const start = source.indexOf('def _multiplier_at(');
  assert.ok(start > 0, 'capabilities._multiplier_at must exist');
  const fn = source.slice(start, source.indexOf('\ndef ', start + 1));
  assert.match(fn, /first matching window/, 'and it documents first-match-wins');
  assert.match(fn, /for window in windows:[\s\S]*\n        return multiplier\n/,
    'returning inside the loop IS the first-match rule');
});

test('the verified windows price the hour they say they do', () => {
  const { api } = loadConsole();
  // Per ELO, from the registry's own declarations: deepseek-v4-flash carries both
  // peaks, glm-4.7 the weekday-gated one, mimo-v2.5 the cheap night window.
  const deepseek = api.eloWindows(catalogueEntry('deepseek-v4-flash'));
  const zai = api.eloWindows(catalogueEntry('glm-4.7'));
  const xiaomi = api.eloWindows(catalogueEntry('mimo-v2.5'));

  // deepseek: both peaks, every day.
  assert.equal(api.priceMultiplier(deepseek, { hour: 1, weekday: 0 }), 2);
  assert.equal(api.priceMultiplier(deepseek, { hour: 3, weekday: 6 }), 2, 'every day, including Sunday');
  assert.equal(api.priceMultiplier(deepseek, { hour: 4, weekday: 0 }), 1, 'half-open: hour 4 is already base');
  assert.equal(api.priceMultiplier(deepseek, { hour: 7, weekday: 0 }), 2);
  assert.equal(api.priceMultiplier(deepseek, { hour: 10, weekday: 0 }), 1, 'half-open: hour 10 is already base');

  // zai: the same hours, Mon-Fri only. The whole weekend bills off-peak.
  assert.equal(api.priceMultiplier(zai, { hour: 7, weekday: 4 }), 2, 'Friday');
  assert.equal(api.priceMultiplier(zai, { hour: 7, weekday: 5 }), 1, 'Saturday is off-peak all day');
  assert.equal(api.priceMultiplier(zai, { hour: 7, weekday: 6 }), 1, 'and so is Sunday');
  // A weekday-restricted window with no weekday to check against must NOT match:
  // claiming a peak on an unknown day overstates the price.
  assert.equal(api.priceMultiplier(zai, { hour: 7 }), 1);
  // And an explicit null is the same "nobody said" — not Monday. `Number(null)` is 0
  // and 0 IS Monday, so this matched zai's Mon-Fri peak on a day nobody named. It is
  // reachable: planWhen builds exactly this shape for a plan reporting utc_hour with
  // no utc_weekday.
  assert.equal(api.priceMultiplier(zai, { hour: 7, weekday: null }), 1);
  assert.deepEqual(plain(api.planWhen({ utc_hour: 7 }, PEAK)).when, { hour: 7, weekday: null },
    'the shape the console really builds');
  assert.equal(api.priceMultiplier(deepseek, { hour: 7, weekday: null }), 2,
    'an ungated window still matches — an unknown day only blocks a gated one');

  // xiaomi is the one that goes the other way — a discount, not a peak.
  assert.equal(api.priceMultiplier(xiaomi, { hour: 18, weekday: 0 }), 0.8);
  assert.equal(api.priceMultiplier(xiaomi, { hour: 23, weekday: 0 }), 0.8);
  assert.equal(api.priceMultiplier(xiaomi, { hour: 0, weekday: 0 }), 1, 'half-open at midnight, so no wrap-around');

  // The two primary rails share the 06:00-10:00 peak, which is the fact that
  // makes overnight cron traffic pay double on both at once.
  assert.equal(api.priceMultiplier(deepseek, { hour: 8, weekday: 2 }), 2);
  assert.equal(api.priceMultiplier(zai, { hour: 8, weekday: 2 }), 2);
});

test('the next change is a real hour, so "until when" is not invented', () => {
  const { api } = loadConsole();
  const deepseek = api.eloWindows(catalogueEntry('deepseek-v4-flash'));
  const zai = api.eloWindows(catalogueEntry('glm-4.7'));
  const xiaomi = api.eloWindows(catalogueEntry('mimo-v2.5'));

  const out = plain(api.nextWindowChange(deepseek, { hour: 7, weekday: 0 }));
  assert.equal(out.hour, 10, 'the peak ends at 10:00 UTC');
  assert.equal(out.hoursAhead, 3);
  assert.equal(out.multiplier, 1);
  // From base, the next change is the peak OPENING.
  assert.equal(api.nextWindowChange(deepseek, { hour: 5, weekday: 0 }).hour, 6);
  assert.equal(api.nextWindowChange(xiaomi, { hour: 12, weekday: 0 }).hour, 16);
  assert.equal(api.nextWindowChange(xiaomi, { hour: 18, weekday: 0 }).hour, 0, 'the discount ends at midnight');
  // Saturday inside zai's peak hours: the next 2x is Monday, and the search has to
  // cross days to find it rather than reporting "no change".
  const monday = api.nextWindowChange(zai, { hour: 7, weekday: 5 });
  assert.equal(monday.hour, 6);
  assert.equal(monday.weekday, 0, 'Monday');
  assert.equal(monday.multiplier, 2);
  // Nothing to report is null, never a fabricated hour.
  assert.equal(api.nextWindowChange([], { hour: 7, weekday: 0 }), null);
  assert.equal(api.nextWindowChange(deepseek, null), null);
  // An unknown day cannot answer "until when" for a weekday-GATED window: the peak it
  // would count down to may be two days off, so null is the only honest answer. An
  // ungated window is unaffected, because it does not depend on the day.
  assert.equal(api.nextWindowChange(zai, { hour: 7, weekday: null }), null);
  assert.equal(api.nextWindowChange(deepseek, { hour: 7, weekday: null }).hour, 10);
});

test('a rail says what it costs now and until when, in one clause', () => {
  const { api } = loadConsole();
  const deepseek = api.eloWindows(catalogueEntry('deepseek-v4-flash'));
  const xiaomi = api.eloWindows(catalogueEntry('mimo-v2.5'));
  assert.equal(api.windowWords(deepseek, { hour: 7, weekday: 0 }), '2× em hora de pico até 10:00 UTC');
  assert.equal(api.windowWords(deepseek, { hour: 12, weekday: 0 }), 'tarifa base até 01:00 UTC, depois 2×');
  assert.equal(api.windowWords(xiaomi, { hour: 18, weekday: 0 }), '0.8× em hora barata até 00:00 UTC');
  assert.equal(api.windowWords([], { hour: 7, weekday: 0 }), 'sem preço que varia com a hora');
  // Time-agnostic is its own answer and must not read as off-peak.
  assert.match(api.windowWords(deepseek, null), /independe da hora/);
});

test('the clock line names every timed rail, expensive ones first', () => {
  const { api } = loadConsole();
  // With no registry at all — the normal case, since /capabilities is optional —
  // the declared rails still have to be reported, or "no window declared" and
  // "off-peak right now" become indistinguishable on screen.
  const rows = plain(api.railWindowRows(null, { hour: 7, weekday: 0 }));
  assert.deepEqual(rows.map((r) => r.rail), ['deepseek', 'zai', 'xiaomi'],
    'the two rails at 2x lead, because they are the ones that change a decision');
  assert.equal(rows[0].expensive, true);
  assert.equal(rows[0].changesAt, 10);
  assert.equal(rows[2].multiplier, 1, 'xiaomi is at base rate at 07:00 UTC');

  // Saturday: deepseek alone is expensive, so zai must fall out of the peak group.
  const weekend = plain(api.railWindowRows(null, { hour: 7, weekday: 5 }));
  assert.equal(weekend[0].rail, 'deepseek');
  assert.equal(weekend.filter((r) => r.expensive).length, 1);

  // A published window WINS over the built-in table, the same precedence the rest
  // of this console applies to declared capability data.
  const published = plain(api.railWindowRows(
    { 'deepseek-v4-pro': { provider: 'deepseek', price_windows: [{ hours_utc: [20, 22], multiplier: 3 }] } },
    { hour: 21, weekday: 0 },
  ));
  assert.equal(published[0].rail, 'deepseek');
  assert.equal(published[0].multiplier, 3);
});

test('a model with no dollar price is never rendered as free', () => {
  const { api } = loadConsole();
  // glm-5.3 has no published per-token price: it is billed in plan credits. $0
  // would say the opposite of the truth — a plan model is not free, and a zero
  // would make it win every cost comparison on this screen.
  assert.equal(api.effectivePrices({ price_in: null, price_out: null }, 2), null);
  assert.equal(api.effectivePrices({}, 1), null);
  // A genuinely free rail HAS a price and keeps it.
  assert.deepEqual(plain(api.effectivePrices({ price_in: 0, price_out: 0 }, 2)), { in: 0, out: 0 });
  // And the multiplier is applied to the stored BASE rate, never to a peak number
  // that was already multiplied.
  const peak = api.effectivePrices({ price_in: 0.66, price_out: 1.98 }, 2);
  assert.equal(api.money(peak.in), '$1.32');
  assert.equal(api.money(peak.out), '$3.96');
  const off = api.effectivePrices({ price_in: 0.66, price_out: 1.98 }, 1);
  assert.equal(api.money(off.in), '$0.66');
});

test('a price is written the way an invoice is read', () => {
  const { api } = loadConsole();
  assert.equal(api.money(1.98), '$1.98');
  assert.equal(api.money(0.8), '$0.80');
  assert.equal(api.money(0), '$0', 'free is a price, and it is exact');
  // A cache-hit rate under a cent must not round to "$0.01" on a screen used to
  // check a bill.
  assert.equal(api.money(0.007), '$0.007');
  assert.equal(api.money('n/a'), '');
});

test('an elo\'s cost line reports the multiplier and the prices behind it', () => {
  const { api } = loadConsole();
  // Inside the peak: both numbers, because "2x" without the rate is not something
  // an operator can compare against the next hop.
  assert.equal(api.priceWords({ price_in: 0.66, price_out: 1.98 }, 2, 'metered'),
    '2× em hora de pico · $1.32 entrada / $3.96 saída por 1M');
  assert.equal(api.priceWords({ price_in: 0.22, price_out: 0.66 }, 1, 'metered'),
    '$0.22 entrada / $0.66 saída por 1M');
  // A plan model in a peak window: the multiplier is real (the credits double) and
  // there is still no dollar figure to show.
  const plan = api.priceWords({ price_in: null, price_out: null }, 2, 'plan');
  assert.match(plan, /2× em hora de pico/);
  assert.match(plan, /créditos do plano/);
  assert.doesNotMatch(plan, /\$0/, 'a plan model rendered as $0 would win every comparison');
  // A cheap window says which direction it goes.
  assert.match(api.priceWords({ price_in: 0.3, price_out: 0.9 }, 0.8, 'metered'), /0\.8× em hora barata/);
  // Nothing to say is nothing rendered: a flat metered rail with no published rate
  // spends no words on it.
  assert.equal(api.priceWords({}, 1, 'metered'), '');
});

test('cheapest_now says out loud that its order is only true of this hour', () => {
  const { api } = loadConsole();
  // An order that silently differs from the declared YAML is indistinguishable
  // from a bug, so the strategy has to name the hour it was computed at.
  const now = api.strategyWords('cheapest_now', { when: { hour: 7, weekday: 0 }, pinPrimary: true });
  assert.equal(now.key, 'cheapest_now');
  assert.equal(now.ordered, true, 'ascending price IS an order, so it keeps its ordinals');
  assert.equal(now.timeRelative, true);
  assert.match(now.label, /pelo mais barato agora/);
  assert.match(now.note, /07:00 UTC/);
  assert.match(now.note, /depende da hora/);
  assert.match(now.note, /com a primeira fixa/);

  // With no clock it IS sequential (capabilities.order_chain), and an order
  // labelled "cheapest" that is really declared order is the most expensive kind
  // of wrong this screen can be.
  const agnostic = api.strategyWords('cheapest_now', { when: null });
  assert.equal(agnostic.key, 'sequential');
  assert.equal(agnostic.ordered, true);
  assert.equal(agnostic.timeRelative, false);
  assert.equal(agnostic.declared, 'cheapest_now', 'what the tier asked for is still reported');
  assert.match(agnostic.note, /precisa de hora/);
});

test('a strategy that did not run is reported as the one that did', () => {
  const { api } = loadConsole();
  // plan_chain reports the DECLARED strategy even when order_chain degraded it —
  // a sequential chain labelled "random" otherwise. The console reads the
  // degradation flag and says which one happened.
  const degraded = api.strategyWords('random', { pinPrimary: false, degraded: true });
  assert.equal(degraded.ordered, true, 'it really was tried in declared order');
  assert.equal(degraded.declared, 'random');
  assert.match(degraded.note, /fonte de sorteio/);
  const cheap = api.strategyWords('cheapest_now', { when: { hour: 7, weekday: 0 }, degraded: true });
  assert.equal(cheap.timeRelative, false);
  assert.match(cheap.note, /precisa de hora/);
});

test('an unreported pin_primary says so instead of claiming the primary is first', () => {
  const { api } = loadConsole();
  // THE F7 FIX. `pin_primary` used to be absent from every chain plan while the
  // console read `opts.pinPrimary !== false` — i.e. TRUE when absent. A tier
  // configured `fallback_strategy: random, pin_primary: false` therefore printed
  // "the primary stays first" and drew hop 1 as ordinal 1 while order_chain had
  // shuffled index 0. The console stated the opposite of what ran.
  const unknown = api.strategyWords('random', {});
  assert.equal(unknown.pinPrimary, null, 'three-valued: true, false, and nobody said');
  assert.equal(unknown.ordered, false);
  assert.match(unknown.note, /não diz se a primeira fica fixa/);
  assert.match(unknown.note, /não se sabe qual tentativa roda primeiro/);
  assert.doesNotMatch(unknown.note, /stays first/, 'the wrong claim is what F7 was');

  // A TIER is different, and the difference is not a detail: the console reads the
  // same file the router does, and rules._pin_primary_of defaults an absent (or
  // non-boolean) value to True. So absence in the POLICY is the documented
  // default, while absence in a computed plan is missing information.
  assert.equal(api.declaredPin(undefined), true);
  assert.equal(api.declaredPin('yes'), true, 'a non-boolean coerces exactly as the router coerces it');
  assert.equal(api.declaredPin(false), false);
  assert.equal(api.declaredPin(true), true);
});

test('a chain plan with no pin_primary draws no hop as first', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  const plan = chainPlan({ strategy: 'random' });
  delete plan.pin_primary;   // an older sidecar, or a build that does not report it
  api.renderChainPlan(plan);
  const lists = findAll(dom.get('chainPlan'), 'hops');
  assert.equal(lists.length, 1, 'one set, not a pinned primary plus a drawn tail');
  assert.match(lists[0].className, /drawn/);
  assert.equal(findAll(dom.get('chainPlan'), 'hop-ord').length, 0,
    'an ordinal on hop 1 is the claim "this runs first", and nothing here supports it');
  assert.match(flat(dom.get('chainPlan')), /não diz se a primeira fica fixa/);

  // And when it IS reported true, the primary is drawn as the first hop it really is.
  api.renderChainPlan(chainPlan({ strategy: 'random', pin_primary: true }));
  assert.deepEqual(findAll(dom.get('chainPlan'), 'hop-ord').map((n) => n.textContent), ['1']);
});

test('the pricing clock names the hour in both zones and the rails in a window', () => {
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;   // the injected clock: no test here depends on when it runs
  api.renderClock();

  assert.equal(dom.get('clockbar').hidden, false);
  assert.equal(dom.get('clockNow').textContent, '07:14 UTC');
  assert.equal(dom.get('clockNow').attrs.datetime, PEAK.toISOString(),
    'a real <time> carries the instant, not only a string that looks like a clock');
  const local = dom.get('clockLocal').textContent;
  assert.match(local, /local/, 'the local reading says it is local');
  assert.match(local, /UTC[+−]\d\d:\d\d/, 'and names its own offset, in any timezone this runs in');

  const rails = dom.get('clockRails').children;
  assert.equal(rails.length, 3, 'one line per rail that prices by the hour');
  const first = flat(rails[0]);
  assert.match(first, /deepseek 2× em hora de pico até 10:00 UTC/, 'real spaces, so it reads aloud');
  assert.match(rails[0].className, /peak/, 'and amber, because paying double needs attention');
  assert.doesNotMatch(rails[2].className, /peak/, 'a rail at base rate is not a condition');

  // The night discount is not a peak and must not be painted as one.
  api.state.clock = NIGHT;
  api.renderClock();
  const night = findAll(dom.get('clockRails'), 'clock-rail').concat(dom.get('clockRails').children);
  const xiaomi = night.find((row) => /xiaomi/.test(flat(row)));
  assert.match(flat(xiaomi), /0\.8× em hora barata/);
  assert.doesNotMatch(xiaomi.className, /peak/);
});

test('the tier chains show a cheapest_now order as time-relative, with the prices', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.clock = PEAK;
  const policy = tierPolicy();
  delete policy.tiers.T1;
  policy.tiers.T2.fallback_strategy = 'cheapest_now';
  policy.tiers.T2.pin_primary = true;
  api.state.policy = policy;
  // The catalogue as the endpoint serves it, so every price and every window below
  // is the registry's own rather than this file's idea of it. deepseek-v4-pro
  // DECLARES the 06:00-10:00 peak — which is why it doubles here, and the reason the
  // fixture may not simply name its rail.
  api.state.capabilities = api.capabilityRegistry(catalogue('glm-5.3', 'deepseek-v4-pro', 'gpt-5.5'));
  api.renderLadder();

  const text = flat(dom.get('ladder'));
  assert.match(text, /pelo mais barato agora/);
  assert.match(text, /depende da hora/, 'an order that differs from the YAML must say why');
  assert.match(text, /07:00 UTC/);
  // The numbers the comparison ran on, per elo — and the peak multiplier applied
  // to the stored base rate rather than a pre-doubled number.
  const pro = registryFacts('deepseek-v4-pro');
  assert.match(text, /2× em hora de pico · \$1\.32 entrada \/ \$3\.96 saída por 1M/);
  assert.deepEqual([pro.price_in * 2, pro.price_out * 2], [1.32, 3.96],
    'and those two numbers are the registry rate times the declared multiplier');
  assert.match(text, /\$5\.00 entrada \/ \$30\.00 saída por 1M/);
  // The plan-covered primary has no dollar price and must not acquire one.
  assert.match(text, /créditos do plano/);
  assert.doesNotMatch(text, /\$0 in/);
});

test('a tier states what its time knobs will do', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.clock = PEAK;
  api.state.policy = {
    rules: [], default: {},
    tiers: {
      T1: {
        model: 'glm-4.7', provider: 'zai', billing_mode: 'plan',
        fallback: [{ model: 'gpt-5.6-luna', provider: 'openai-codex', billing_mode: 'subscription' }],
        time_cap: { max_multiplier: 1.5 },
        time_policy: { avoid_peak: ['deepseek', 'zai'], prefer: ['gpt-5.6-luna'] },
      },
    },
  };
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /o teto se desliga se isso fosse deixar a fila vazia/,
    'a cost control that can cause an outage is the one thing the cap must not be');
  assert.match(text, /manda deepseek e zai para o fim da fila enquanto estiverem em hora de pico/);
  assert.match(text, /prefere gpt-5\.6-luna enquanto estiverem fora do pico/);
  // The knobs are facts about the CONFIG. Which rail is expensive right now is the
  // clock line's fact, and it is said in exactly one place.
  assert.deepEqual(plain(api.timeKnobWords({})), [], 'a tier with no knobs spends no words');

  // ── THE CAP SAYS WHAT THE CAP DOES ──────────────────────────────────────
  // "declines any rail over 1.5×" was false about exactly the rail it matters most
  // for: T1's primary is plan-billed, its zai window is 2.0×, and apply_time_cap
  // cannot remove it because the ceiling is denominated in DOLLARS and a plan draws
  // credits off an allowance already bought. The same claim had been corrected twice
  // in router.yaml's comments before it shipped in this line, so it is pinned here
  // against the module that does the removing rather than against the wording.
  const units = billingUnits();
  const tier = api.state.policy.tiers.T1;
  const hops = plain(api.tierChain(tier));
  const words = plain(api.timeKnobWords(tier, { hops }));
  const declines = words.find((w) => /recusa uma tentativa/.test(w)) || '';
  const exemption = words.find((w) => /não pode tirar/.test(w)) || '';

  assert.doesNotMatch(text, /recusa qualquer provedor/,
    'the third appearance of a dollar cap described as evicting anything it is priced against');
  // A frase nomeia o modo na palavra do glossário (§4.6), não no enum cru.
  const MODE_WORD = { metered: 'por token', subscription: 'por assinatura', plan: 'plano', free: 'sem cobrança' };
  units.removable.forEach((mode) => assert.match(declines, new RegExp(MODE_WORD[mode] || mode),
    `the cap removes ${mode} hops, so it has to name them`));
  units.exempt.forEach((mode) => assert.doesNotMatch(declines, new RegExp(MODE_WORD[mode] || mode),
    `${mode} is not in the dollars bucket, so the cap cannot decline it`));
  // And the exemption is named on THIS tier's own hop, with the reason: an operator
  // reading 1.5× over a 2.0× primary is owed the answer, not left to measure it.
  assert.match(exemption, /glm-4\.7/);
  assert.match(exemption, /créditos saem de uma franquia já comprada/);
  assert.match(exemption, /cobra a mais/, 'why credits are not dollars, not just that they differ');
  assert.equal(words.some((w) => /o teto não tira nada aqui/.test(w)), false,
    'this chain still holds two dollar-billed hops, so the cap is not inert');

  // AGREEMENT ON THE TABLE ITSELF, mode for mode: the console's answer to "is this
  // priced in dollars?" is capabilities._BILLING_RANK's, or the sentence above is
  // true today and wrong after one registry edit.
  units.modes.forEach(({ mode, bucket }) => assert.equal(
    api.billsInDollars(mode), bucket === units.dollarsBucket,
    `${mode} belongs to ${bucket}`));
  assert.equal(api.billsInDollars('carrier-pigeon'), false, 'an unknown unit is never assumed to be dollars');
  assert.equal(api.billsInDollars(undefined), false, 'and neither is an undeclared one');
});

test('a cap over a chain with no dollar-billed hop says it can remove nothing', () => {
  // capabilities.apply_time_cap's own worked example: behind a plan primary a tier
  // may hold nothing the ceiling can act on, and then the cap is insurance plus a
  // standing report of the credit peak. Reading "declines … over 1.5×" there sends
  // an operator looking for a hop it dropped, of which there are none.
  const { api } = loadConsole();
  const tier = {
    model: 'glm-4.7', provider: 'zai', billing_mode: 'plan',
    fallback: [{ model: 'nemotron-ultra', provider: 'nous', billing_mode: 'free' }],
    time_cap: { max_multiplier: 1.5 },
  };
  const words = plain(api.timeKnobWords(tier)).join(' ');
  assert.match(words, /não pode tirar glm-4\.7 pago por plano e nemotron-ultra sem cobrança/);
  assert.match(words, /múltiplo de zero continua zero/, 'each unit gets its own reason');
  assert.match(words, /o teto não tira nada aqui/);

  // A hop whose billing mode NOBODY declared is not evidence that the cap is inert:
  // the console does not know that hop's unit, so it says what it does know — the
  // cap leaves it alone — and drops the "removes nothing" claim.
  const undeclared = plain(api.timeKnobWords({
    model: 'glm-4.7', provider: 'zai', billing_mode: 'plan',
    fallback: [{ model: 'mystery-1', provider: 'somewhere' }],
    time_cap: { max_multiplier: 1.5 },
  })).join(' ');
  assert.match(undeclared, /mystery-1, modo de pagamento não declarado/);
  assert.match(undeclared, /nunca é adivinhada para tirar um provedor da fila/);
  assert.doesNotMatch(undeclared, /o teto não tira nada aqui/,
    'a cost control is never reported as inert on the strength of a gap');

  // A mode the console has not learned is exempt for the same reason — _BILLING_RANK
  // answers undefined for it too — and is named AS WRITTEN rather than swallowed.
  const foreign = plain(api.timeKnobWords({
    model: 'glm-4.7', provider: 'zai', billing_mode: 'plan',
    fallback: [{ model: 'mystery-1', provider: 'somewhere', billing_mode: 'prepaid' }],
    time_cap: { max_multiplier: 1.5 },
  })).join(' ');
  assert.match(foreign, /mystery-1, pago em prepaid/);
  assert.doesNotMatch(foreign, /o teto não tira nada aqui/);
});

test('a bypassed time cap is as loud as a bypassed capability filter', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;
  api.renderChainPlan(chainPlan({
    time_cap_bypassed: true, time_cap: { max_multiplier: 1.5 }, utc_hour: 7, utc_weekday: 0,
  }));
  const first = dom.get('chainPlan').children[0];
  assert.match(first.className, /warn-line bad/);
  const said = String(first.textContent || '') + flat(first);
  assert.match(said, /Teto de preço ignorado/);
  assert.match(said, /1\.5×/, 'the cap that was dropped');
  assert.match(said, /07:00 UTC/, 'and the hour it was dropped at');
  assert.match(said, /paga preço de pico/, 'what it costs');
  assert.match(said, /aumente o teto|fora do pico/, 'and what the operator can do');
});

test('a degraded strategy names the DECLARED word and the router\'s own reason', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;
  // N4, and the shape is the one rules.plan_chain really emits: `strategy` is what
  // RAN, `strategy_declared` is what the tier asked for, and
  // `strategy_degraded_reason` is rules._effective_strategy's own sentence. The
  // banner read `strategy` — the effective one — so after a degrade it rendered
  // "The tier declares “sequential”, but it did not run", a sentence that
  // contradicts itself, and then GUESSED the reason the router had already computed.
  api.renderChainPlan(chainPlan({
    strategy: 'sequential',
    strategy_declared: 'cheapest_now',
    strategy_degraded: true,
    strategy_degraded_reason: 'no clock was injected, so prices could not be compared',
    time_agnostic: true,
  }));
  const banner = dom.get('chainPlan').children[0];
  const said = String(banner.textContent || '') + flat(banner);
  assert.match(said, /A ordem de reserva não foi a declarada/);
  assert.match(said, /declara “cheapest_now”/, 'the DECLARED word, which is the only one that can have failed to run');
  assert.doesNotMatch(said, /declares “sequential”/, 'never the strategy that did run');
  assert.match(said, /no clock was injected, so prices could not be compared/,
    'the router computed the reason; the console must not guess at it');
  assert.match(said, /ordem que de fato rodou — na ordem escrita/, 'and what ran instead');

  // The reason is the SERVER'S: change it and the banner changes with it.
  api.renderChainPlan(chainPlan({
    strategy: 'sequential',
    strategy_declared: 'random',
    strategy_degraded: true,
    strategy_degraded_reason: 'no rng was injected, so the tail was not shuffled',
  }));
  const random = flat(dom.get('chainPlan')) + String(dom.get('chainPlan').children[0].textContent || '');
  assert.match(random, /declara “random”/);
  assert.match(random, /no rng was injected, so the tail was not shuffled/);

  // A plan that reports no reason still states the degrade, and the fallback wording
  // is derived from the DECLARED word — asking about the effective one returned an
  // empty note, which is what made the guess unconditional.
  api.renderChainPlan(chainPlan({
    strategy: 'sequential', strategy_declared: 'random', strategy_degraded: true, strategy_degraded_reason: '',
  }));
  assert.match(flat(dom.get('chainPlan')) + String(dom.get('chainPlan').children[0].textContent || ''),
    /sorteada.*fonte de sorteio/s, 'the fallback wording is about random, because random is what was declared');

  // And a plan that reports no declared word names none: the degrade is still said,
  // without inventing a strategy nobody sent.
  api.renderChainPlan(chainPlan({ strategy: 'sequential', strategy_degraded: true, strategy_declared: '' }));
  const nameless = String(dom.get('chainPlan').children[0].textContent || '')
    + flat(dom.get('chainPlan').children[0]);
  assert.match(nameless, /A ordem de reserva declarada não foi a que rodou/);
  assert.doesNotMatch(nameless, /declares “/);
});

test('the degrade banner and the chain agree about which strategy ran', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;
  // `random` that degraded DID run in declared order, so the chain keeps its
  // ordinals — the banner and the numbered hops are two readings of one field
  // (`strategy`), and this pins that they cannot come apart.
  api.renderChainPlan(chainPlan({
    strategy: 'sequential', strategy_declared: 'random', strategy_degraded: true,
    strategy_degraded_reason: 'no rng was injected, so the tail was not shuffled',
  }));
  assert.deepEqual(findAll(dom.get('chainPlan'), 'hop-ord').map((n) => n.textContent), ['1', '2'],
    'the order that ran is an order, so it is numbered');
  // A `random` that DID run has no first hop and no ordinals, and no banner.
  api.renderChainPlan(chainPlan({
    strategy: 'random', strategy_declared: 'random', strategy_degraded: false, pin_primary: false,
  }));
  assert.equal(findAll(dom.get('chainPlan'), 'hop-ord').length, 0);
  assert.doesNotMatch(flat(dom.get('chainPlan')), /A ordem de reserva não foi a declarada/);
});

test('a task whose eligible chain collapsed to one rail is told it has no fallback', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;
  // F12's failure mode: a bare "chart" in the task infers vision, the filter drops
  // two of three hops, and what is left is a single subscription rail — which
  // looks exactly like a healthy tier unless the console says otherwise. The
  // DECLARED chain still lists three hops, so this is invisible in router.yaml.
  api.renderChainPlan(chainPlan({
    chain: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }],
    rejected: [
      { model: 'glm-5.3', provider: 'zai', reject_reason: 'no_vision' },
      { model: 'deepseek-v4-flash', provider: 'deepseek', reject_reason: 'no_vision' },
    ],
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /1 provedor independente em 1 tentativa elegível/);
  assert.match(text, /Sem reserva para esta tarefa/);
  assert.match(text, /openai-codex/, 'it names the upstream everything now depends on');
  assert.match(text, /não tem para onde ir/);
});

test('an elo the time cap refused says the two numbers that make it fixable', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.capabilities = api.capabilityRegistry(catalogue('deepseek-v4-pro'));
  // The cap's OWN numbers: capabilities.apply_time_cap returns
  // capped: [{model, multiplier}] and rules._multipliers_for seeds `multipliers`
  // from exactly those entries, so 2.0 here is the value the refusal was decided
  // on. The old fixture carried neither and the row still printed "2× now" — from
  // the console's rail-window guess, which is the same number by luck and a
  // different number as soon as the elo is not the rail.
  api.renderChainPlan(chainPlan({
    utc_hour: 7, utc_weekday: 0,
    time_cap: { max_multiplier: 1.5 },
    chain: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }],
    capped: [{ model: 'deepseek-v4-pro', multiplier: 2 }],
    multipliers: { 'deepseek-v4-pro': 2 },
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /Tirados da fila \(1\)/, 'a cost refusal and a capability refusal answer the same question');
  assert.match(text, /deepseek-v4-pro/);
  assert.match(text, /nesta hora ele custa mais do que o teto de preço do grupo permite/);
  assert.match(text, /2× now, cap 1\.5×/, 'the price and the ceiling, not an enum');
  assert.doesNotMatch(text, /time_cap allows|reject_reason/, 'the enum never reaches the screen');

  // The capped ENTRY's own multiplier is enough on its own: a plan whose
  // `multipliers` map is empty (no clock reached _multipliers_for) still carries the
  // number apply_time_cap decided with, and the row must read it rather than
  // recompute one.
  api.renderChainPlan(chainPlan({
    utc_hour: 7, utc_weekday: 0,
    time_cap: { max_multiplier: 1.5 },
    chain: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }],
    capped: [{ model: 'deepseek-v4-pro', multiplier: 2 }],
  }));
  assert.match(flat(dom.get('chainPlan')), /2× now, cap 1\.5×/);
});

test('a capped elo is listed once, even when the filter also reported it', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.renderChainPlan(chainPlan({
    utc_hour: 7, utc_weekday: 0,
    rejected: [{ model: 'deepseek-v4-pro', provider: 'deepseek', reject_reason: 'time_cap' }],
    capped: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }],
  }));
  assert.match(flat(dom.get('chainPlan')), /Tirados da fila \(1\)/, 'one elo, one row');
  assert.deepEqual(plain(api.cappedEntries({ capped: [{ model: 'a' }, 'b'] }, [{ model: 'a' }])),
    [{ model: 'b', reject_reason: 'time_cap' }], 'a plain string is a model too');
  assert.deepEqual(plain(api.cappedEntries({}, [])), []);
});

test('an elo the time policy moved says why it moved', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;
  api.renderChainPlan(chainPlan({
    utc_hour: 7, utc_weekday: 0,
    chain: [
      { model: 'gpt-5.6-luna', provider: 'openai-codex' },
      { model: 'deepseek-v4-pro', provider: 'deepseek' },
    ],
    demoted: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }],
    promoted: ['gpt-5.6-luna'],
    // A demoted elo is a peak-priced one by construction — apply_time_policy only
    // demotes what is inside a multiplier > 1.0 window — so the plan that carries
    // one carries the other.
    peak_priced: ['deepseek-v4-pro'],
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /foi para o fim da fila — deepseek está em hora cara/);
  assert.match(text, /só é tentada se tudo à frente falhar/);
  assert.match(text, /foi para o começo da fila — este grupo a prefere/);
  // Both flag shapes the router uses reach the same words.
  assert.deepEqual(plain(api.timeFlagIndex({ demoted: [{ model: 'x' }], promoted: ['y'], capped: ['z'] })),
    { x: { demoted: true }, y: { promoted: true }, z: { capped: true } });
  assert.deepEqual(plain(api.timeFlagIndex(null)), {});
  // A per-elo flag works too, because the plan may carry it either way.
  assert.match(api.timePolicyMove({ provider: 'zai', demoted: true }, null), /foi para o fim da fila — zai/);
  assert.equal(api.timePolicyMove({ provider: 'zai' }, null), '');
});

test('the plan\'s own hour wins over the console\'s, and it says which it used', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = NIGHT;   // 18:00 UTC on the console's clock

  // A plan that reports the hour it planned against is authoritative for every
  // price below it: reading a 03:00 plan at 18:00 prices would misprice it.
  const planned = plain(api.planWhen({ utc_hour: 3, utc_weekday: 0 }, NIGHT));
  assert.deepEqual(planned.when, { hour: 3, weekday: 0 });
  assert.equal(planned.source, 'plan');
  api.renderChainPlan(chainPlan({ utc_hour: 3, utc_weekday: 0 }));
  assert.match(flat(dom.get('chainPlan')), /planejado às 03:00 UTC/,
    'a plan made at another hour must not be read as though it were made now');

  // And when the plan was made at the hour the clock line is already reporting,
  // saying so is the same fact twice — which is what put two "07:00 UTC" into one
  // sentence on the live screen.
  api.state.clock = new Date(Date.UTC(2026, 7, 17, 18, 30));
  api.renderChainPlan(chainPlan({ utc_hour: 18, utc_weekday: 0 }));
  assert.doesNotMatch(flat(dom.get('chainPlan')), /planned at/);

  // A plan that reports nothing falls back to the console's clock — which is the
  // hour the clock line at the top of the screen already names, so there is still
  // exactly one authority for "now".
  const fallback = plain(api.planWhen({}, NIGHT));
  assert.deepEqual(fallback.when, { hour: 18, weekday: 0 });
  assert.equal(fallback.source, 'console');
  // And a plan that says it was time-agnostic is honoured: that is a real answer,
  // and it is the one that makes cheapest_now degrade.
  assert.equal(plain(api.planWhen({ time_agnostic: true }, NIGHT)).when, null);
  // An hour outside 0..23 is not an hour.
  assert.equal(plain(api.planWhen({ utc_hour: 99 }, NIGHT)).source, 'console');

  assert.equal(api.timeCapOf({ time_cap: { max_multiplier: 1.5 } }), 1.5);
  assert.equal(api.timeCapOf({ max_multiplier: 2 }), 2);
  assert.equal(api.timeCapOf({}), null, 'no cap is null, never 1.0 — those are different claims');
});

test('the planner\'s own multiplier wins over the console\'s arithmetic', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.capabilities = { 'deepseek-v4-pro': { provider: 'deepseek', price_in: 0.66, price_out: 1.98 } };
  // plan['multipliers'] is the number the ORDERING decision was actually made on.
  // If it disagrees with what the console would compute, the plan is the truth —
  // the console must not quietly overwrite the router's reasoning with its own.
  api.renderChainPlan(chainPlan({
    utc_hour: 12, utc_weekday: 0,             // the console would compute 1x here
    chain: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }],
    multipliers: { 'deepseek-v4-pro': 2 },
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /2× em hora de pico · \$1\.32 entrada \/ \$3\.96 saída por 1M/);
});

test('an ordinary sequential tier gains no price noise from the time layer', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  // 12:00 UTC: no rail is in a window, and nothing here is ordered by price. A
  // console that printed a rate on every hop anyway would have spent the whole
  // screen's density on a fact that is not news (DESIGN.md §2.1).
  api.state.clock = new Date(Date.UTC(2026, 7, 17, 12, 0));
  api.state.policy = tierPolicy();
  api.state.capabilities = { 'glm-4.7': { provider: 'zai', context_window: 200000, price_in: 0.6, price_out: 2.2 } };
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.doesNotMatch(text, /per 1M/, 'no window, no price-ordering, no cost line');
  assert.match(text, /200K de contexto/, 'and the facts that were always there stay');
});

test('a bypass that reports no reasons says so, instead of reading as "nothing dropped"', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  // The live shape of the bypass today: every elo was disqualified, and the
  // rejection list comes back EMPTY, so the reasons exist nowhere. An absent
  // Dropped section beside "bypassed" reads as "nothing was dropped" — the exact
  // opposite of what happened.
  api.renderChainPlan(chainPlan({ bypassed: true, rejected: [], unknown: [] }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /não informou motivo por tentativa para esta exceção/);
  assert.match(text, /compare você mesmo as exigências acima/, 'the operator is given the one move left');
  assert.doesNotMatch(text, /Dropped \(0\)/, 'and no empty section is framed to hold it');

  // When the reasons ARE reported, the note is not repeated — the rows carry it.
  api.renderChainPlan(chainPlan({
    bypassed: true,
    rejected: [{ model: 'glm-4.7', provider: 'zai', reject_reason: 'no_vision' }],
  }));
  assert.doesNotMatch(flat(dom.get('chainPlan')), /no per-elo reasons/);
});

// ── a bypass drops nothing, and the panel must say nothing was dropped ────
// The invariant both stages hold: a filter, a cap or a policy that would empty the
// chain BYPASSES ITSELF and keeps its per-elo reasons as diagnostics
// (capabilities.filter_chain: "a consumer that renders `rejected` as 'dropped'
// must therefore check `bypassed` first"; apply_time_cap says the same of
// `capped`). So on a bypass every named elo is still in the chain, and a "Dropped"
// heading over it is the console contradicting itself half a screen apart.

test('a bypassed filter drops nothing, so no elo is rendered twice', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  // The shape apply/filter really produce on the shipped policy: a turn around
  // 840,000 est_input_tokens matches huge-context-read → T3, the derived
  // min_context exceeds every hop's window, all three are rejected, and the filter
  // bypasses — so `chain` is the ORIGINAL three hops and `rejected` names all three.
  const plan = chainPlan({
    bypassed: true,
    // The derived floor, not the raw turn: capabilities.derive_requirements sets
    // min_context = ceil(est_input_tokens × 1.25), so 840,001 tokens asks for
    // 1,050,002 — two more than gpt-5.6-terra's 1,050,000 window, and above all three.
    requirements: { min_context: 1050002 },
    unsatisfiable: ['min_context'],
    chain: [
      { model: 'gpt-5.6-terra', provider: 'openai-codex' },
      { model: 'deepseek-v4-pro', provider: 'deepseek' },
      { model: 'glm-5.3', provider: 'zai' },
    ],
    rejected: [
      { model: 'gpt-5.6-terra', provider: 'openai-codex', reject_reason: 'context_too_small' },
      { model: 'deepseek-v4-pro', provider: 'deepseek', reject_reason: 'context_too_small' },
      { model: 'glm-5.3', provider: 'zai', reject_reason: 'context_too_small' },
    ],
  });
  // The split is the plan's own two flags, not a re-derivation.
  const outcome = plain(api.droppedElos(plan));
  assert.deepEqual(outcome.dropped, [], 'a bypass removes nothing');
  assert.deepEqual(outcome.retained.map((hop) => hop.model),
    ['gpt-5.6-terra', 'deepseek-v4-pro', 'glm-5.3'], 'and keeps every reason');
  assert.equal(outcome.gaveWay, 'filtro de capacidade');

  api.renderChainPlan(plan);
  const text = flat(dom.get('chainPlan'));
  assert.doesNotMatch(text, /Dropped/, 'nothing was dropped, so nothing says it was');
  assert.match(text, /Continuam na fila \(3\)/);
  assert.match(text, /Nada foi tirado — o filtro de capacidade cedeu/);
  assert.match(text, /objeções, não exclusões/);
  // What the router will do and what the operator can do are said ONCE, in the
  // bypass line at the top; this section does not repeat either.
  assert.match(text, /tentar todas mesmo assim/);
  // Every elo appears as an eligible hop AND in the retained list — which is
  // correct, and is exactly why the second list may not be headed "Dropped".
  assert.deepEqual(findAll(dom.get('chainPlan'), 'hop-ord').map((n) => n.textContent), ['1', '2', '3']);
  assert.match(text, /janela de contexto dele é menor do que esta tarefa precisa/);
});

test('a bypassed time cap drops nothing either, and the two bypasses are independent', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  // apply_time_cap's bypass: every elo was over the ceiling, so `chain` is the
  // original and `capped` is retained as diagnostics.
  api.renderChainPlan(chainPlan({
    utc_hour: 7, utc_weekday: 0,
    time_cap: { max_multiplier: 1.5 },
    time_cap_bypassed: true,
    chain: [
      { model: 'deepseek-v4-pro', provider: 'deepseek' },
      { model: 'glm-4.7', provider: 'zai' },
    ],
    capped: [{ model: 'deepseek-v4-pro', multiplier: 2 }, { model: 'glm-4.7', multiplier: 2 }],
    multipliers: { 'deepseek-v4-pro': 2, 'glm-4.7': 2 },
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /Teto de preço ignorado/, 'the loud line still fires');
  assert.doesNotMatch(text, /Dropped/, 'but nothing was dropped');
  assert.match(text, /Continuam na fila \(2\)/);
  assert.match(text, /teto de preço cedeu/);
  assert.match(text, /2× now, cap 1\.5×/, 'and the numbers behind the objection survive');

  // INDEPENDENT: the filter can bypass — restoring everything it rejected — and the
  // cap can then remove a hop from the restored chain for real. The elo the cap
  // actually took out is dropped; the filter's diagnostics are not.
  const both = plain(api.droppedElos(chainPlan({
    bypassed: true,
    time_cap_bypassed: false,
    chain: [{ model: 'gpt-5.6-terra', provider: 'openai-codex' }],
    rejected: [{ model: 'gpt-5.6-terra', reject_reason: 'no_vision' }],
    capped: [{ model: 'deepseek-v4-pro', multiplier: 2 }],
  })));
  assert.deepEqual(both.dropped.map((hop) => hop.model), ['deepseek-v4-pro'],
    'the cap really removed this one');
  assert.deepEqual(both.retained.map((hop) => hop.model), ['gpt-5.6-terra'],
    'and the filter only objected to that one');

  // A real removal outranks a diagnostic naming the SAME elo: its absence from the
  // chain is the fact being read, and it must not be listed as "still in the chain".
  const clash = plain(api.droppedElos(chainPlan({
    bypassed: true,
    chain: [{ model: 'glm-5.3', provider: 'zai' }],
    rejected: [{ model: 'deepseek-v4-pro', reject_reason: 'context_too_small' }],
    capped: [{ model: 'deepseek-v4-pro', multiplier: 2 }],
  })));
  assert.deepEqual(clash.dropped.map((hop) => hop.model), ['deepseek-v4-pro']);
  assert.deepEqual(clash.retained, [], 'one elo, one row, and the row that is true');

  // No bypass at all: both lists are real exclusions and nothing is retained.
  const ordinary = plain(api.droppedElos(chainPlan({
    rejected: [{ model: 'glm-5.3', reject_reason: 'no_vision' }],
    capped: [{ model: 'deepseek-v4-pro', multiplier: 2 }],
  })));
  assert.deepEqual(ordinary.dropped.map((hop) => hop.model), ['glm-5.3', 'deepseek-v4-pro']);
  assert.deepEqual(ordinary.retained, []);
  assert.equal(ordinary.gaveWay, '');
  assert.deepEqual(plain(api.droppedElos(null)), { dropped: [], retained: [], filterBypassed: false, capBypassed: false, gaveWay: '' });
});

// ── `unsatisfiable`: the request is pathological, not the roster ──────────
// The field exists so "no elo could EVER meet this" is distinguishable from "these
// particular elos were rejected", without an operator reconstructing it from three
// coincidental context_too_small reasons. capabilities._unsatisfiable_requirements
// computes it by comparing the derived floor against MAX_REGISTERED_CONTEXT and
// every window the chain declares — so the console's ceiling is read from those same
// two places and never invented.

test('an unsatisfiable requirement names the requirement and the ceiling, not the roster', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  // The registry as GET /capabilities really serves it, so the ceiling below is
  // gpt-5.6-terra's own published window rather than a number written here:
  // 1,050,000, which is MAX_REGISTERED_CONTEXT in router/capabilities.py.
  api.state.capabilities = api.capabilityRegistry(
    catalogue('gpt-5.6-terra', 'deepseek-v4-pro', 'glm-5.3'));
  // The shipped pathological case: ~840,001 est_input_tokens on T3 derives
  // min_context = ceil(tokens × 1.25) = 1,050,002 — two tokens above the widest
  // window there is, so every hop is rejected and the filter bypasses itself.
  const plan = chainPlan({
    bypassed: true,
    unsatisfiable: ['min_context'],
    requirements: { min_context: 1050002 },
    chain: [
      { model: 'gpt-5.6-terra', provider: 'openai-codex' },
      { model: 'deepseek-v4-pro', provider: 'deepseek' },
      { model: 'glm-5.3', provider: 'zai' },
    ],
    rejected: [
      { model: 'gpt-5.6-terra', provider: 'openai-codex', reject_reason: 'context_too_small' },
      { model: 'deepseek-v4-pro', provider: 'deepseek', reject_reason: 'context_too_small' },
      { model: 'glm-5.3', provider: 'zai', reject_reason: 'context_too_small' },
    ],
  });
  api.renderChainPlan(plan);
  const box = dom.get('chainPlan');
  const text = flat(box);

  // CAUSE FIRST: the requirement is why the bypass below happened.
  const first = box.children[0];
  assert.match(first.className, /warn-line/);
  assert.doesNotMatch(first.className, /bad/, 'nothing was refused — the router kept routing');
  const said = String(first.textContent || '') + flat(first);
  assert.match(said, /Exigência que nenhum modelo atende/);
  // The ceiling, at the precision that keeps the two figures DIFFERENT: both round
  // to "1.1M", and "holds 1.1M, needs 1.1M" would deny its own reason.
  assert.match(said, /maior janela de contexto que o roteador alcança tem 1,050,000, precisa de 1,050,002/);
  assert.match(said, /exigência é que está impossível, não as tentativas/,
    'the whole distinction the field carries');
  assert.match(said, /Divida o trabalho em pedidos menores, ou acrescente um modelo com janela maior/,
    'and a recovery that is actually available');
  assert.doesNotMatch(said, /min_context/, 'the requirement key never reaches the screen');

  // The bypass line keeps its own fact — what the router does — and hands the cause
  // and the fix to the line above rather than saying either twice.
  const bypass = box.children[1];
  assert.match(bypass.className, /warn-line bad/);
  const bypassSaid = String(bypass.textContent || '') + flat(bypass);
  assert.match(bypassSaid, /tentar todas mesmo assim/);
  assert.match(bypassSaid, /não atende à exigência acima/);
  assert.doesNotMatch(bypassSaid, /No elo in this chain can meet these requirements/,
    'the cause is stated once');
  assert.doesNotMatch(bypassSaid, /Add an elo that qualifies/,
    'advice nobody can take: by definition nothing could qualify');

  // And the bypass still means nothing was dropped: every elo is in the chain.
  assert.doesNotMatch(text, /Dropped/);
  assert.match(text, /Continuam na fila \(3\)/);
});

test('an unsatisfiable requirement is reported even when the filter did not bypass', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.capabilities = api.capabilityRegistry(catalogue('glm-5.3'));
  // capabilities.filter_chain reports `unsatisfiable` with `bypassed` False whenever
  // a fail-open unknown hop stays eligible — the floor is still unmeetable, and the
  // task is about to run on a model nobody checked. The two flags are independent.
  api.renderChainPlan(chainPlan({
    bypassed: false,
    unsatisfiable: ['min_context'],
    requirements: { min_context: 4000000 },
    chain: [{ model: 'who-knows', provider: 'zai' }],
    rejected: [{ model: 'glm-5.3', provider: 'zai', reject_reason: 'context_too_small' }],
    unknown: ['who-knows'],
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /Exigência que nenhum modelo atende/);
  assert.match(text, /tem 1M, precisa de 4M/, 'the ceiling is the registry\'s widest, not the chain\'s');
  assert.doesNotMatch(text, /Capability filter bypassed/, 'no control gave way here');
  assert.match(text, /elegíveis por suposição/, 'and the unverified hop is still named');
});

test('the ceiling is read from what published one, and never invented', () => {
  const { api } = loadConsole();
  // Two sources, exactly the two capabilities._unsatisfiable_requirements consults.
  const registry = api.capabilityRegistry(catalogue('glm-5.3', 'gpt-5.6-terra'));
  assert.deepEqual(plain(api.contextCeiling({ chain: [{ model: 'glm-5.3' }] }, registry)),
    { tokens: 1050000, scope: 'registry' },
    'with a registry the ceiling is over every model the router can reach');

  // A DECLARED window wins over the registry, the same precedence capabilities_for
  // applies, so an operator who describes a bigger house model is not told their
  // request is impossible when it is not.
  assert.equal(api.contextCeiling(
    { chain: [{ model: 'glm-5.3', context_window: 2000000 }] }, registry).tokens, 2000000);

  // /capabilities is an OPTIONAL read: with no registry the only windows visible are
  // the ones the chain declares, and the words must not claim more than that.
  const chainOnly = plain(api.contextCeiling({
    chain: [{ model: 'a', context_window: 200000 }],
    rejected: [{ model: 'b', context_window: 128000 }],
  }, null));
  assert.deepEqual(chainOnly, { tokens: 200000, scope: 'chain' });

  // Nobody published one. A zero would claim the router can hold nothing.
  assert.deepEqual(plain(api.contextCeiling({ chain: [{ model: 'a' }] }, null)),
    { tokens: null, scope: '' });
  assert.deepEqual(plain(api.contextCeiling(null, null)), { tokens: null, scope: '' });
});

test('an unsatisfiable requirement with no visible ceiling still says which requirement', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  // No registry read answered and no hop declares a window, so there is no number to
  // compare against — the requirement is still named, and no ceiling is fabricated.
  api.renderChainPlan(chainPlan({
    unsatisfiable: ['min_context'],
    requirements: { min_context: 1050002 },
    chain: [{ model: 'who-knows', provider: 'zai' }],
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /Nada que o roteador alcança tem os 1,050,002 tokens que esta tarefa precisa/);
  assert.match(text, /nenhuma tentativa aqui publica janela para comparar/);
  assert.doesNotMatch(text, /holds 0|0 tokens/, 'an unknown ceiling is never zero');

  // A requirement key this console has not learned still renders, for the same
  // reason a requirement chip does: the loudest fact about the request must not be
  // dropped because the vocabulary grew.
  const grown = api.unsatisfiableWords({ unsatisfiable: ['min_output_tokens'] }, null);
  assert.match(grown.said, /Nenhum modelo que o roteador alcança atende a min output tokens/);
  assert.match(grown.said, /baixe o que a regra exige do modelo/);

  // Absence renders nothing at all — not an empty line, not a framed void.
  assert.equal(api.unsatisfiableWords(chainPlan(), null), null, 'an empty list is not a section');
  assert.equal(api.unsatisfiableWords({}, null), null, 'and neither is a plan without the key');
  assert.equal(api.unsatisfiableWords(null, null), null);
});

// ── `peak_priced` is PRICE, `demoted` is POSITION ─────────────────────────
// apply_time_policy's own split: `peak_priced` names every elo `avoid_peak` matched
// inside a dearer window whether or not moving it changed anything, and `demoted`
// names only what the returned permutation actually moved later. One field claiming
// both readings was lying about one of them.

test('peak-priced hops with an unchanged order read as billing double, not as a broken policy', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;
  api.state.capabilities = api.capabilityRegistry(catalogue('deepseek-v4-pro', 'glm-5.3'));
  // THE SHIPPED T3/T4 CASE, exactly as apply_time_policy reports it: avoid_peak
  // [deepseek, zai] over [gpt-5.6-terra, deepseek-v4-pro, glm-5.3] at 07:00 UTC
  // leaves the chain byte-identical, so `demoted` is EMPTY and `peak_priced` names
  // both. Rendering `demoted` here would be a claim about an order nothing changed.
  api.renderChainPlan(chainPlan({
    utc_hour: 7, utc_weekday: 0,
    chain: [
      { model: 'gpt-5.6-terra', provider: 'openai-codex' },
      { model: 'deepseek-v4-pro', provider: 'deepseek' },
      { model: 'glm-5.3', provider: 'zai' },
    ],
    demoted: [],
    peak_priced: ['deepseek-v4-pro', 'glm-5.3'],
    multipliers: { 'deepseek-v4-pro': 2, 'glm-5.3': 2 },
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /Tentativas em hora de pico/);
  assert.match(text, /deepseek-v4-pro e glm-5\.3 estão em hora mais cara às 07:00 UTC/);
  assert.match(text, /paga a tarifa maior nessas tentativas/, 'the BILL, which is what the field is about');
  assert.match(text, /casou com elas e não moveu nada/, 'the policy fired and the permutation was the identity');
  assert.match(text, /já estão no fim da fila/);
  assert.match(text, /não há nada mais barato para tentar antes/,
    'this chain cannot step around them — which is not the same as a policy that failed');
  // POSITION is a different fact, and there is no position to report.
  assert.doesNotMatch(text, /moved to the end/, 'nothing moved, so nothing says it moved');
  assert.doesNotMatch(text, /moved them later/);
  // The per-elo prices are still the router's own numbers, so the line and the hops
  // cannot disagree about which hour this is true of.
  assert.match(text, /2× em hora de pico/);
});

test('a peak-priced hop the policy did move reports the move and the price separately', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;
  api.renderChainPlan(chainPlan({
    utc_hour: 7, utc_weekday: 0,
    chain: [
      { model: 'gpt-5.6-luna', provider: 'openai-codex' },
      { model: 'deepseek-v4-pro', provider: 'deepseek' },
    ],
    demoted: ['deepseek-v4-pro'],
    peak_priced: ['deepseek-v4-pro'],
    multipliers: { 'deepseek-v4-pro': 2 },
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /deepseek-v4-pro está em hora mais cara às 07:00 UTC/, 'singular, because one hop matched');
  assert.match(text, /a mandou para depois, então toda tentativa mais barata é tentada antes/);
  assert.doesNotMatch(text, /moved nothing/);
  // The elo's own row still carries the position, which is where a position belongs.
  assert.match(text, /foi para o fim da fila — deepseek está em hora cara/);

  // A chain the policy could only partly reorder names both halves, so neither
  // reading is implied from the other.
  api.renderChainPlan(chainPlan({
    utc_hour: 7, utc_weekday: 0,
    chain: [
      { model: 'gpt-5.6-luna', provider: 'openai-codex' },
      { model: 'glm-5.3', provider: 'zai' },
      { model: 'deepseek-v4-pro', provider: 'deepseek' },
    ],
    demoted: ['deepseek-v4-pro'],
    peak_priced: ['deepseek-v4-pro', 'glm-5.3'],
    multipliers: { 'deepseek-v4-pro': 2, 'glm-5.3': 2 },
  }));
  const partly = flat(dom.get('chainPlan'));
  assert.match(partly, /moveu o que podia/);
  assert.match(partly, /glm-5\.3 não podia ir mais para trás/);
  assert.match(partly, /não há nada mais barato para tentar antes/);
  // Which hop moved is said on that hop's own row and nowhere else, so one fact
  // does not get two authorities half a panel apart.
  assert.equal(partly.match(/deepseek-v4-pro/g).length, 2, 'the chain hop and the peak set, not a third claim');
  assert.match(partly, /foi para o fim da fila — deepseek está em hora cara/);
});

test('peak pricing splits price from position, and absence renders nothing', () => {
  const { api, dom } = loadConsole();
  // Both list shapes the router uses, and the split the console reads: `stuck` is
  // what avoid_peak matched and the permutation could not move.
  assert.deepEqual(plain(api.peakPricing({
    peak_priced: ['deepseek-v4-pro', { model: 'glm-5.3' }, 'glm-5.3'],
    demoted: [{ model: 'deepseek-v4-pro' }],
  })), {
    priced: ['deepseek-v4-pro', 'glm-5.3'],
    moved: ['deepseek-v4-pro'],
    stuck: ['glm-5.3'],
  });
  assert.deepEqual(plain(api.peakPricing({})), { priced: [], moved: [], stuck: [] });
  assert.deepEqual(plain(api.peakPricing(null)), { priced: [], moved: [], stuck: [] });

  // No peak, no line — an empty list is not a section (DESIGN.md §2.1).
  assert.equal(api.peakPriceWords(chainPlan(), { hour: 7, weekday: 0 }), null);
  assert.equal(api.peakPriceWords({}, null), null, 'and neither is a plan without the key');
  api.state.policy = tierPolicy();
  api.renderChainPlan(chainPlan());
  assert.doesNotMatch(flat(dom.get('chainPlan')), /Peak-priced/);

  // A time-agnostic plan has no hour to report the window at, and inventing the
  // browser's would price a plan that never saw a clock.
  const clockless = api.peakPriceWords({ peak_priced: ['glm-5.3'], demoted: [] }, null);
  assert.match(clockless.said, /glm-5\.3 está em hora mais cara, então a tarefa paga/);
  assert.doesNotMatch(clockless.said, /UTC/);

  // The two moves left are named once per viewport: the time-cap bypass line already
  // carries both, so this line does not repeat them.
  const alsoCapped = api.peakPriceWords(
    { peak_priced: ['glm-5.3'], demoted: [], time_cap_bypassed: true }, { hour: 7, weekday: 0 });
  assert.doesNotMatch(alsoCapped.said, /fora do pico/);
  assert.match(api.peakPriceWords({ peak_priced: ['glm-5.3'], demoted: [] }, { hour: 7, weekday: 0 }).said,
    /Passe o trabalho para uma hora fora do pico/);
});

// ── the headline verdict answers from the plan, not from the declared route ──
// The verdict is this console's primary answer to "where does this task go", and it
// was built from decision.output — the DECLARED tier route — while the Chain-plan
// panel directly below it was built from the plan the executor iterates. Two panels,
// one screen, opposite answers, and the operator acts on the one at the top.

// The exact case the review measured: a vision task lands on T2, whose declared
// chain is glm-5.3 → gpt-5.6-luna → deepseek-v4-flash. Only gpt-5.6-luna can read an
// image (registry: vision true), so the filter drops the other two for no_vision and
// the eligible chain is one hop long.
function visionExplain(extra) {
  return {
    mode: 'deterministic_dry_run', requires_classifier: false,
    decision: {
      matched_rule_id: 'image-attached',
      matched_clauses: {},
      output: {
        model: 'glm-5.3', provider: 'zai',
        fallback: [
          { model: 'gpt-5.6-luna', provider: 'openai-codex' },
          { model: 'deepseek-v4-flash', provider: 'deepseek' },
        ],
      },
      chain_plan: chainPlan(Object.assign({
        requirements: { vision: true },
        chain: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }],
        rejected: [
          { model: 'glm-5.3', provider: 'zai', reject_reason: 'no_vision' },
          { model: 'deepseek-v4-flash', provider: 'deepseek', reject_reason: 'no_vision' },
        ],
        independent_rails: 1,
      }, extra || {})),
    },
  };
}

function probeWith(explain) {
  return loadConsole({
    fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(explain)) }),
  });
}

test('the verdict names the elo the executor will really try first', async () => {
  const explain = visionExplain();
  const { api, dom } = probeWith(explain);
  api.state.loading = false;
  api.state.policy = tierPolicy();
  await api.probe('describe this screenshot');

  const sentence = flat(dom.get('probeResult'));
  // What the plan says, and only that.
  assert.match(sentence, /roteado para gpt-5\.6-luna/);
  assert.match(sentence, /em openai-codex/);
  assert.doesNotMatch(sentence, /routed to glm-5\.3/,
    'the declared primary cannot read an image and is not where this task goes');
  assert.doesNotMatch(sentence, /Falls back to/,
    'one eligible hop is no fallback, and claiming two would be the declared route again');

  // The declared route survives as SECONDARY context, labelled for what it is.
  assert.match(sentence, /rota declarada/);
  assert.match(sentence, /glm-5\.3 → gpt-5\.6-luna → deepseek-v4-flash/);

  // AGREEMENT, which is the whole point: the model the verdict names is the model
  // the chain-plan panel numbers as hop 1, and every model the verdict does NOT name
  // is one the panel reports as rejected.
  const plan = explain.decision.chain_plan;
  assert.equal(api.verdictRoute(plan, explain.decision.output).first, plan.chain[0].model);
  const eligible = findAll(dom.get('chainPlan'), 'hops')[0];
  assert.deepEqual(findAll(eligible, 'hop-model').map((n) => n.textContent), [plan.chain[0].model]);
  assert.match(flat(dom.get('chainPlan')), /não lê imagem/);
});

test('verdictRoute reads the plan, and reports what it cannot know', () => {
  const { api } = loadConsole();
  const declared = {
    model: 'glm-5.3', provider: 'zai',
    fallback: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }, { model: 'deepseek-v4-flash', provider: 'deepseek' }],
  };

  // A sequential plan: first is first, and the rest are a real order.
  const sequential = plain(api.verdictRoute(chainPlan({
    chain: [
      { model: 'gpt-5.6-luna', provider: 'openai-codex' },
      { model: 'deepseek-v4-flash', provider: 'deepseek' },
    ],
  }), declared));
  assert.equal(sequential.source, 'plan');
  assert.equal(sequential.first, 'gpt-5.6-luna');
  assert.equal(sequential.provider, 'openai-codex');
  assert.deepEqual(sequential.rest, ['deepseek-v4-flash']);
  assert.equal(sequential.ordered, true);
  assert.equal(sequential.differs, true, 'the declared route has three hops and this one has two');

  // cheapest_now reordered the same set: nothing was dropped, and where the task
  // goes still changed. Length comparison alone would have called these identical.
  const reordered = plain(api.verdictRoute(chainPlan({
    strategy: 'cheapest_now', strategy_declared: 'cheapest_now',
    chain: [
      { model: 'deepseek-v4-flash', provider: 'deepseek' },
      { model: 'gpt-5.6-luna', provider: 'openai-codex' },
      { model: 'glm-5.3', provider: 'zai' },
    ],
  }), declared));
  assert.equal(reordered.first, 'deepseek-v4-flash');
  assert.equal(reordered.differs, true, 'a reorder changes the answer without dropping a hop');

  // An unshuffled `random` chain has NO first hop, and saying one would be the same
  // false claim the chain list already refuses to draw as an ordinal.
  const shuffled = plain(api.verdictRoute(chainPlan({
    strategy: 'random', strategy_declared: 'random', pin_primary: false,
    chain: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }, { model: 'mimo-v2.5', provider: 'xiaomi' }],
  }), declared));
  assert.equal(shuffled.first, '', 'nothing runs first until the request is made');
  assert.equal(shuffled.ordered, false);
  assert.equal(shuffled.pinPrimary, false);
  assert.deepEqual(shuffled.rest, ['gpt-5.6-luna', 'mimo-v2.5']);

  // pin_primary UNREPORTED is a third answer, not the same as false: the plan did not
  // say, so "drawn at random" would be inventing the field's value.
  const unreported = plain(api.verdictRoute(chainPlan({
    strategy: 'random', strategy_declared: 'random', pin_primary: undefined,
    chain: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }, { model: 'mimo-v2.5', provider: 'xiaomi' }],
  }), declared));
  assert.equal(unreported.first, '');
  assert.equal(unreported.pinPrimary, null);

  // pin_primary true is the honest middle: hop 1 IS first, the tail is not ordered.
  const pinned = plain(api.verdictRoute(chainPlan({
    strategy: 'random', strategy_declared: 'random', pin_primary: true,
    chain: [
      { model: 'glm-5.3', provider: 'zai' },
      { model: 'gpt-5.6-luna', provider: 'openai-codex' },
      { model: 'deepseek-v4-flash', provider: 'deepseek' },
    ],
  }), declared));
  assert.equal(pinned.first, 'glm-5.3');
  assert.equal(pinned.ordered, false, 'the tail is a set');
  assert.equal(pinned.differs, false, 'and this one IS the declared route');

  // A `random` that DEGRADED ran in declared order, so hop 1 is genuinely first —
  // the same reading the chain list uses to restore its ordinals.
  const degraded = plain(api.verdictRoute(chainPlan({
    strategy: 'sequential', strategy_declared: 'random', strategy_degraded: true, pin_primary: false,
    chain: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }, { model: 'mimo-v2.5', provider: 'xiaomi' }],
  }), declared));
  assert.equal(degraded.first, 'gpt-5.6-luna');
  assert.equal(degraded.ordered, true);

  // No plan at all: the declared route is the only answer there is, and it is
  // returned marked as declared so the caller can label it.
  const none = plain(api.verdictRoute(null, declared));
  assert.equal(none.source, 'declared');
  assert.equal(none.first, 'glm-5.3');
  assert.deepEqual(none.rest, ['gpt-5.6-luna', 'deepseek-v4-flash']);
  assert.equal(none.differs, false, 'there is nothing to differ from');
  assert.equal(plain(api.verdictRoute(null, {})).source, 'none');
});

test('a shuffled chain is not given a first hop by the verdict either', async () => {
  const explain = visionExplain({
    strategy: 'random', strategy_declared: 'random', pin_primary: false,
    chain: [
      { model: 'gpt-5.6-luna', provider: 'openai-codex' },
      { model: 'mimo-v2.5', provider: 'xiaomi' },
    ],
    rejected: [{ model: 'glm-5.3', provider: 'zai', reject_reason: 'no_vision' }],
  });
  const { api, dom } = probeWith(explain);
  api.state.loading = false;
  api.state.policy = tierPolicy();
  await api.probe('describe this screenshot');
  const sentence = flat(dom.get('probeResult'));
  assert.match(sentence, /roteado para qualquer um de/);
  assert.match(sentence, /sorteadas a cada pedido/);
  assert.doesNotMatch(sentence, /roteado para gpt-5\.6-luna/, 'no elo is named as first');
  // And the panel below draws no ordinals, which is the same fact reached the same
  // way — one field, `strategy`, read once.
  assert.equal(findAll(dom.get('chainPlan'), 'hop-ord').length, 0);
});

test('the declared route is silent when it is the route that runs', async () => {
  // DESIGN.md §2.2: two authorities for one fact is worse than none. When the plan
  // IS the declared chain there is nothing to contrast, so the secondary line does
  // not ship — and neither does an empty one.
  const explain = visionExplain({
    requirements: {},
    chain: [
      { model: 'glm-5.3', provider: 'zai' },
      { model: 'gpt-5.6-luna', provider: 'openai-codex' },
      { model: 'deepseek-v4-flash', provider: 'deepseek' },
    ],
    rejected: [],
    independent_rails: 3,
  });
  const { api, dom } = probeWith(explain);
  api.state.loading = false;
  api.state.policy = tierPolicy();
  await api.probe('write a docstring');
  const sentence = flat(dom.get('probeResult'));
  assert.match(sentence, /roteado para glm-5\.3 em zai/);
  assert.match(sentence, /Recorre a gpt-5\.6-luna → deepseek-v4-flash/);
  assert.doesNotMatch(sentence, /rota declarada/, 'the same fact twice is not context');
});

// ── the price audit: an unread catalogue is not a missing price ────────────
// GET /capabilities is the read behind the audit, and while the route did not exist
// the call 404'd and the panel did not go blank — it went FALSE. Every elo rendered
// as capability-unverified, and deepseek-v4-flash, which publishes 0.22 in / 0.66
// out, rendered "sem preço por token publicado".

test('an unanswered price question renders as silence, never as "no price"', () => {
  const { api } = loadConsole();
  const flash = registryFacts('deepseek-v4-flash');
  assert.equal(flash.price_in, 0.22, 'the rail this line was wrong about does publish a rate');
  assert.equal(flash.price_out, 0.66);

  // NO CATALOGUE. The console knows nothing about this elo's price, and a peak
  // multiplier does not license it to claim there is none.
  assert.equal(api.pricePublished({}, null), null, 'three-valued: null is "nobody answered"');
  assert.equal(api.priceWords({}, 2, 'metered', null), '2× em hora de pico',
    'the multiplier is the router\'s and survives; the invented absence does not');
  assert.doesNotMatch(api.priceWords({}, 2, 'metered', null), /no per-token price/);

  // THE CATALOGUE ANSWERED, and it says this elo bills in credits. That is a
  // reported fact and it earns words — an operator has to know a plan rail is not
  // free. `price_published` is service.py's, computed by asking the running path.
  const plan = catalogueEntry('glm-5.3');
  assert.equal(plan.price_published, false, 'glm-5.3 publishes no dollar rate');
  assert.equal(api.pricePublished(plan, plan), false);
  const words = api.priceWords(plan, 2, 'plan', api.pricePublished(plan, plan));
  assert.match(words, /2× em hora de pico/);
  assert.match(words, /cobrado em créditos do plano/);
  assert.doesNotMatch(words, /\$0/, 'a plan rail rendered as $0 would win every comparison on screen');

  // THE CATALOGUE ANSWERED WITH A RATE: it is rendered, at the multiplier applied.
  const metered = catalogueEntry('deepseek-v4-flash');
  assert.equal(metered.price_published, true);
  assert.equal(api.pricePublished(metered, metered), true);
  assert.equal(api.priceWords(metered, 1, 'metered', true), '$0.22 entrada / $0.66 saída por 1M');
  assert.equal(api.priceWords(metered, 2, 'metered', true), '2× em hora de pico · $0.44 entrada / $1.32 saída por 1M');

  // A rate declared on the elo in router.yaml still wins over the catalogue's answer,
  // the same precedence capabilities.capabilities_for applies everywhere else.
  assert.equal(api.pricePublished({ price_in: 0.5, price_out: 1 }, { price_published: false }), true);
});

test('a plan-billed elo with a list price still says the dollars are not invoiced', () => {
  // glm-4.7 is the case this line was wrong about: plan-covered AND carrying a
  // published rate ("also purchasable metered at the same price"), so the console
  // rendered `2× peak · $1.20 in / $4.40 out per 1M` for a rail that draws 16 output
  // credits — 32 inside the window — and invoices none of those dollars on a plan
  // key. The credits-versus-dollars split is what cheapest_now buckets on and the
  // only thing a time_cap may act on, so the surface that shows prices must carry it.
  const { api } = loadConsole();
  const entry = catalogueEntry('glm-4.7');
  const facts = registryFacts('glm-4.7');
  assert.equal(entry.billing_mode, 'plan', 'the registry bills this one in credits');
  assert.equal(entry.price_published, true, 'and publishes a dollar rate anyway — that is the trap');
  assert.deepEqual(facts.price_windows, [{ hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2.0 }]);

  // The mode comes from the entry the endpoint sent, not from a literal here.
  const peak = api.priceWords(entry, facts.price_windows[0].multiplier, entry.billing_mode,
                              api.pricePublished(entry, entry));
  assert.match(peak, /2× em hora de pico/);
  assert.match(peak, new RegExp(`\\$${(facts.price_in * 2).toFixed(2)} entrada / \\$${(facts.price_out * 2).toFixed(2)} saída por 1M`),
    'the numbers stay: they are the registry rate times the declared multiplier');
  assert.match(peak, /preço de tabela/, 'the dollars are named as the list price they are, not left as the bill');
  assert.match(peak, /cobrado em créditos do plano/, 'and what the elo actually spends is said in the same breath');

  // AGREEMENT WITH THE BUCKETING, mode for mode. The qualifier fires on the modes
  // capabilities._BILLING_RANK puts in the credits bucket and on no others —
  // `subscription` publishes the per-token rate its seat bills at, so its dollars are
  // real dollars and marking them as credits would take a whole chain's prices off
  // the comparison the console is auditing.
  const units = billingUnits();
  units.modes.forEach(({ mode }) => {
    const words = api.priceWords(entry, 1, mode, true);
    assert.equal(/créditos/.test(words), units.inCredits.indexOf(mode) !== -1,
      `${mode} prices read in ${units.inCredits.indexOf(mode) !== -1 ? 'credits' : 'dollars'}`);
    assert.match(words, /\$0\.60 entrada \/ \$2\.20 saída por 1M/, 'every mode still shows the published rate');
  });
});

test('the chain plan shows the catalogue\'s prices instead of reporting it has none', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = PEAK;
  // With the endpoint answering, the audit reads the registry's own numbers.
  api.state.capabilities = api.capabilityRegistry(catalogue('deepseek-v4-flash', 'gpt-5.6-luna'));
  const plan = chainPlan({
    utc_hour: 7, utc_weekday: 0,
    requirements: {},
    chain: [
      { model: 'gpt-5.6-luna', provider: 'openai-codex' },
      { model: 'deepseek-v4-flash', provider: 'deepseek' },
    ],
    multipliers: { 'gpt-5.6-luna': 1, 'deepseek-v4-flash': 2 },
  });
  api.renderChainPlan(plan);
  const text = flat(dom.get('chainPlan'));
  assert.doesNotMatch(text, /sem preço por token publicado/,
    'both of these elos publish one; the panel said otherwise while /capabilities 404\'d');
  assert.match(text, /2× em hora de pico · \$0\.44 entrada \/ \$1\.32 saída por 1M/, 'the peak rate, from the plan\'s own multiplier');
  assert.match(text, /\$0\.20 entrada \/ \$1\.20 saída por 1M/, 'and the flat rail\'s, undoubled');
  assert.doesNotMatch(text, /unverified/, 'the catalogue verified them, so nothing routes unchecked');

  // WITHOUT the endpoint the same plan says less, and nothing false: the multipliers
  // are the router's, and no price is claimed either to exist or not to.
  api.state.capabilities = api.capabilityRegistry({ missing: true });
  api.renderChainPlan(plan);
  const blind = flat(dom.get('chainPlan'));
  assert.doesNotMatch(blind, /sem preço por token publicado/, 'silence, not a false absence');
  assert.doesNotMatch(blind, /per 1M/, 'and no price it cannot source');
  assert.match(blind, /2× em hora de pico/, 'while the router\'s own multiplier is still reported');
});

test('the catalogue envelope is read by name, and an empty one is not a model', () => {
  const { api } = loadConsole();
  // The endpoint's real shape.
  const table = plain(api.capabilityRegistry(catalogue('glm-4.6')));
  assert.deepEqual(Object.keys(table), ['glm-4.6']);
  assert.equal(table['glm-4.6'].price_published, true);
  // `{models: {}}` is a real answer — the endpoint replied and the registry knows
  // nothing. Read positionally it fell through to the envelope itself, which yielded
  // a registry holding one entry called "models": a model id that does not exist,
  // presented as verified capability data.
  assert.equal(api.capabilityRegistry({ data: { models: {}, unknown_models: [], warnings: [] } }), null);
  assert.equal(api.capabilityRegistry({ data: { registry_available: false, models: {} } }), null);
});

// ── liveness already priced every elo, and the console read none of it ────
// service.py's liveness entries carry price_multiplier, in_expensive_window and
// next_window_change per elo, evaluated at an hour it names. Where the server has
// answered, the server's number is the one on screen.

function livenessPayload(models, at) {
  return {
    models, worst: 'alive',
    evaluated_at: { at: at.iso, at_source: 'now', utc_hour: at.hour, utc_weekday: at.weekday },
  };
}

test('liveness\'s own multipliers are preferred, and only for the hour it named', () => {
  const { api } = loadConsole();
  const payload = livenessPayload([
    { model_key: 'glm-4.7@zai', model: 'glm-4.7', provider: 'zai', state: 'alive',
      capabilities_known: true, in_expensive_window: true, price_multiplier: 2.0,
      next_window_change: { hour: 10, weekday: 0, hours_ahead: 3, multiplier: 1.0 } },
    { model_key: 'mimo-v2.5@xiaomi', model: 'mimo-v2.5', provider: 'xiaomi', state: 'alive',
      capabilities_known: true, in_expensive_window: false, price_multiplier: 0.8,
      next_window_change: { hour: 0, weekday: 1, hours_ahead: 17, multiplier: 1.0 } },
  ], { iso: '2026-08-17T07:14:00+00:00', hour: 7, weekday: 0 });

  const index = plain(api.liveMultipliers(payload, { hour: 7, weekday: 0 }));
  assert.deepEqual(index['glm-4.7'], { multiplier: 2, expensive: true, changesAt: 10 });
  // A CHEAP window is not an expensive one, and the server is the one that decides:
  // in_expensive_window is False at 0.8×, so nothing here takes the amber.
  assert.deepEqual(index['mimo-v2.5'], { multiplier: 0.8, expensive: false, changesAt: 0 });

  // THE HOUR IS THE GATE. A payload read at 07:00 says nothing about 08:00, and a
  // stale peak is exactly as wrong as an invented one.
  assert.deepEqual(plain(api.liveMultipliers(payload, { hour: 8, weekday: 0 })), {},
    'another hour is not an answer');
  assert.deepEqual(plain(api.liveMultipliers(payload, { hour: 7, weekday: 5 })), {},
    'and neither is another day — zai peaks Mon-Fri only');
  assert.deepEqual(plain(api.liveMultipliers(payload, null)), {}, 'no clock, no claim');
  assert.deepEqual(plain(api.liveMultipliers(null, { hour: 7, weekday: 0 })), {});
  // A multiplier that is not a positive number is not an answer either: reporting 1.0
  // for it would claim the base rate had been checked.
  const junk = livenessPayload([
    { model: 'a', price_multiplier: 0 }, { model: 'b', price_multiplier: null },
    { model: 'c', price_multiplier: 'two' }, { model: 'd', price_multiplier: 1.5 },
  ], { iso: '', hour: 7, weekday: 0 });
  assert.deepEqual(Object.keys(plain(api.liveMultipliers(junk, { hour: 7, weekday: 0 }))), ['d']);
});

test('the tier chains price an elo with liveness\'s number, not their own arithmetic', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.clock = PEAK;                 // Monday 07:14 UTC
  api.state.policy = tierPolicy();
  // The catalogue publishes glm-4.7's rate; liveness publishes what it costs NOW.
  api.state.capabilities = api.capabilityRegistry(catalogue('glm-4.7', 'gpt-5.6-luna', 'mimo-v2.5'));
  api.state.liveness = livenessPayload([
    { model: 'glm-4.7', provider: 'zai', state: 'alive', in_expensive_window: true, price_multiplier: 2.0,
      next_window_change: { hour: 10, weekday: 0, hours_ahead: 3, multiplier: 1.0 } },
  ], { iso: '2026-08-17T07:14:00+00:00', hour: 7, weekday: 0 });
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /2× em hora de pico · \$1\.20 entrada \/ \$4\.40 saída por 1M/,
    'the server\'s multiplier against the catalogue\'s base rate');

  // WITH NO CATALOGUE the console can read no window at all, and liveness is then the
  // only answer to "what does this cost now". It is read, and it is the router's own:
  // the multiplier appears with no price beside it, because no rate was published to
  // this console and none is invented.
  api.state.capabilities = null;
  api.renderLadder();
  const blind = flat(dom.get('ladder'));
  assert.match(blind, /2× em hora de pico/, 'liveness answered, so the peak is still reported');
  assert.doesNotMatch(blind, /per 1M/, 'and no rate is claimed that nothing published');

  // A liveness read from ANOTHER hour is not used. With no catalogue and no usable
  // read there is nothing to say about price, and nothing is said — a 07:00 peak
  // asserted from an 18:00 measurement is exactly as wrong as an invented one.
  api.state.liveness = livenessPayload([
    { model: 'glm-4.7', provider: 'zai', state: 'alive', in_expensive_window: false, price_multiplier: 1.0 },
  ], { iso: '2026-08-17T18:00:00+00:00', hour: 18, weekday: 0 });
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /peak|cheap window/,
    'a stale read is discarded rather than believed against the current hour');
});

// ── CA9: the model field is a pre-filtered <select>, not a place to type ──
// The defect under test: a model field was an <input> where the operator
// typed an id from memory. The question they were actually answering was
// "which models CAN serve this group" — answerable only from the catalogue,
// and only with the group's own requirements applied. These tests pin the
// picker the spec §2.5 describes: a select grouped by provider, a count
// that renders even at zero, the two escapes (show-all and free-id), the
// §3.4(c) fallback when the catalogue did not come, and the provider rail
// syncing from the catalogue entry.

function byLabel(node, label) {
  return (node.children || []).find((c) => (c.children[0] || {}).textContent === label
    && String(c.className || '').includes('field'));
}

test('a model field is a <select> grouped by provider, with the count always visible', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  policy.tiers.T2.model = 'glm-4.7';
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', context_window: 200000 },
    'gpt-5.6-luna': { provider: 'openai-codex', context_window: 1000000 },
    'mimo-v2.5': { provider: 'xiaomi', context_window: 1048576 },
    'sem-rail': {},
  };
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');

  const modelWrap = byLabel(box, 'Modelo');
  assert.ok(modelWrap, 'the model field exists and is labelled');
  const select = modelWrap.children.find((c) => c.tagName === 'select');
  assert.ok(select, 'a model field is a <select>, not an <input> (CA9)');
  const groups = select.children.filter((c) => c.tagName === 'optgroup');
  assert.equal(groups.length, 4, 'one optgroup per provider, none hidden');
  assert.ok(groups.some((g) => g.label === 'Sem provedor informado'),
    'a model with no provider is visible, never dropped');
  const optionIds = [];
  groups.forEach((g) => g.children.forEach((o) => optionIds.push(o.value)));
  assert.deepEqual(optionIds.sort(), ['glm-4.7', 'gpt-5.6-luna', 'mimo-v2.5', 'sem-rail'].sort(),
    'every catalogue model is present');

  const note = modelWrap.children.find((c) => String(c.className).includes('field-note'));
  assert.ok(note, 'the count line renders');
  assert.match(note.textContent, /^4 modelos atendem à exigência deste grupo/,
    'no requirements declared, so every catalogue model is eligible');
  assert.doesNotMatch(note.textContent, /tokens/, 'no floor claimed where none was declared');

  // CA9 word for word: with the catalogue up, no free-text model field is
  // born — typing is the escape hatch, not the default.
  const freeInputs = [];
  modelWrap.children.forEach((c) => { if (c.tagName === 'input' && c.hidden === false) freeInputs.push(c); });
  assert.equal(freeInputs.length, 0, 'no free-text model field in the initial editing state');
});

test('the count applies the group min_context floor and renders zero as a diagnosis', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  policy.tiers.T3 = { requirements: { min_context: 200000 } };
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', context_window: 200000 },
    'gpt-5.6-luna': { provider: 'openai-codex', context_window: 1000000 },
  };
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T3', name: 'T3', bind: 'tier', tier: 'T3' });
  let modelWrap = byLabel(dom.get('inspector'), 'Modelo');
  let note = modelWrap.children.find((c) => String(c.className).includes('field-note'));
  assert.match(note.textContent, /^2 modelos atendem à exigência deste grupo \(≥ 200,000 tokens\)/,
    'the floor is applied and printed in requirementChips format');

  // gpt-5.6-luna holds 1M, so a floor above every window is what zero means.
  policy.tiers.T3.requirements = { min_context: 2000000 };
  api.renderInspector({ id: 'tier:T3', name: 'T3', bind: 'tier', tier: 'T3' });
  modelWrap = byLabel(dom.get('inspector'), 'Modelo');
  note = modelWrap.children.find((c) => String(c.className).includes('field-note'));
  assert.match(note.textContent, /^0 modelos atendem à exigência deste grupo/,
    'zero is a rendered count, not a silent empty select');
  assert.match(flat(modelWrap), /Nenhum modelo do seu catálogo declara 2,000,000 tokens ou mais/,
    'the zero case names the two ways out (§3.4(d))');
  assert.ok(modelWrap.children.find((c) => c.tagName === 'select'),
    'the select remains a select even at zero eligible');
});

test('a model field without a catalogue falls back to free text with the §3.4(c) note', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = tierPolicy();
  api.state.loading = false;
  api.state.capabilities = null;
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const modelWrap = byLabel(box, 'Modelo');
  assert.ok(modelWrap, 'the model field exists');
  const input = modelWrap.children.find((c) => c.tagName === 'input');
  assert.ok(input, 'no catalogue: the model field is an input (text fallback)');
  assert.match(flat(modelWrap), /Sem catálogo, não há lista para escolher\./,
    'the §3.4(c) note is present');
  assert.equal(box.children.filter((c) => c.tagName === 'select').length, 0,
    'no select is born from a missing catalogue');
  // and the ladder note says the §3.4(c) thing, not just "unverified"
  api.renderLadder();
  assert.match(dom.get('ladderNote').textContent, /não tem catálogo de modelos/,
    'the ladder names the missing catalogue when the whole thing is absent');
});

test('choosing from the select syncs the provider rail from the catalogue entry', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = tierPolicy();
  api.state.loading = false;
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', context_window: 200000 },
    'gpt-5.6-luna': { provider: 'openai-codex', context_window: 1000000 },
  };
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const modelWrap = byLabel(dom.get('inspector'), 'Modelo');
  const select = modelWrap.children.find((c) => c.tagName === 'select');
  assert.ok(select, 'the select exists');
  select.value = 'gpt-5.6-luna';
  select._listeners.change();
  assert.equal(api.state.draft.tiers.T2.model, 'gpt-5.6-luna', 'the draft writes the id');
  assert.equal(api.state.draft.tiers.T2.provider, 'openai-codex',
    'choosing a model fills the provider rail from the catalogue');
  select.value = 'glm-4.7';
  select._listeners.change();
  assert.equal(api.state.draft.tiers.T2.model, 'glm-4.7');
  assert.equal(api.state.draft.tiers.T2.provider, 'zai',
    'and re-choosing re-syncs the rail');
});

test('the escape hatch writes an off-catalogue id and says what the console stops knowing', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = tierPolicy();
  api.state.loading = false;
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', context_window: 200000 },
  };
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const modelWrap = byLabel(dom.get('inspector'), 'Modelo');
  const escapeBtn = modelWrap.children.find((c) => /Usar um id que não está na lista/.test(c.textContent || ''));
  assert.ok(escapeBtn, 'the escape hatch button exists');
  escapeBtn._listeners.click();
  const freeInput = modelWrap.children.find((c) => c.tagName === 'input' && c.hidden === false);
  assert.ok(freeInput, 'clicking it reveals the free-text input');
  freeInput.value = 'claude-999';
  freeInput._listeners.input();
  assert.equal(api.state.draft.tiers.T2.model, 'claude-999',
    'the off-catalogue id is written to the draft');
  assert.match(flat(modelWrap), /Este id não está no catálogo\. Ele vai rodar/,
    'and the literal warning reads');
});

test('the show-all toggle widens the select and warns about runtime filtering', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  policy.tiers.T2.model = 'gpt-5.6-luna';
  policy.tiers.T2.requirements = { min_context: 500000 };
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', context_window: 200000 },
    'gpt-5.6-luna': { provider: 'openai-codex', context_window: 1000000 },
    'mimo-v2.5': { provider: 'xiaomi', context_window: 1048576 },
  };
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const modelWrap = byLabel(dom.get('inspector'), 'Modelo');
  const toggle = modelWrap.children.find((c) => /mostrar todos/.test(c.textContent || ''));
  assert.ok(toggle, 'the toggle button exists');
  assert.equal(toggle.hidden, false, 'visible while models are filtered out');
  assert.match(toggle.textContent, /mostrar todos os 3/);
  const warn = modelWrap.children.find((c) => /ficam de fora da fila/.test(c.textContent || ''));
  assert.ok(warn, 'the runtime-filter warning exists under the count');
  assert.equal(warn.hidden, true, 'hidden until show-all is used');
  toggle._listeners.click();
  assert.equal(warn.hidden, false, 'clicking show-all reveals the runtime warning');
  const select = modelWrap.children.find((c) => c.tagName === 'select');
  const values = [];
  select.children.forEach((g) => (g.children || [g]).forEach((o) => values.push(o.value)));
  assert.equal(values.length, 3, 'show-all lists every catalogue model');
  assert.ok(values.indexOf('glm-4.7') !== -1, 'the ineligible one is reachable');
});

// ── CA4: the "Ordem:" line — five knobs of a group, none of them blank ──────
// The criterion: opening Modelos with no click inside the panel, each group block
// lists `1 + tier.fallback.length` attempts AND prints an effective value for the
// five strategy keys, with `(padrão do motor)` on every key the policy does not
// declare. What it protects against is a screen that prints a knob only when the
// file declares one — silence then reads as "this group has no ceiling", while it
// means "the router's default applies". They are different facts.

test('the Ordem line names all FIVE knobs, and marks every one the policy left out', () => {
  const { api } = loadConsole();
  // A group that declares nothing: every part must still carry a value, and every
  // part must say the value came from the engine.
  const parts = api.orderLineParts({});
  assert.equal(parts.length, 5, 'five keys is the contract, not four');
  parts.forEach((part, i) => {
    assert.notEqual(String(part).trim(), '', `part ${i} is blank, which is what CA4 forbids`);
    assert.match(part, /\(padrão do motor\)$/,
      `part ${i} came from the engine and has to say so — "sem teto de preço" alone reads as a fact about the file`);
  });
  // And the words are the router's own defaults: declared order, first one fixed.
  assert.match(parts[0], /na ordem escrita/);
  assert.match(parts[1], /o primeiro fica fixo/);
  assert.match(parts[2], /sem teto de preço/);
  assert.match(parts[3], /sem política de horário/);
  assert.match(parts[4], /sem exigência de contexto/);
});

test('a fully declared group prints its own values and claims no default', () => {
  const { api } = loadConsole();
  const parts = api.orderLineParts({
    fallback_strategy: 'cheapest_now',
    pin_primary: false,
    time_cap: { max_multiplier: 1.5 },
    time_policy: { avoid_peak: ['deepseek', 'zai'] },
    requirements: { min_context: 200000 },
  }, { when: { hour: 9, weekday: 2 } });
  assert.equal(parts.length, 5);
  parts.forEach((part, i) => assert.doesNotMatch(part, /padrão do motor/,
    `part ${i} is declared in the file, so crediting the engine would be a lie`));
  assert.match(parts[0], /pelo mais barato agora/);
  assert.match(parts[1], /o primeiro pode ser trocado/, 'pin_primary false is a value, not an absence');
  assert.match(parts[2], /teto de preço 1\.5×/);
  assert.match(parts[3], /evita pico de deepseek e zai/);
  assert.match(parts[4], /pelo menos 200,000 tokens/, 'the floor is said in the same chips a rule uses');
});

test('a ceiling written in a form the router cannot read says THAT, not "no ceiling"', () => {
  // `time_cap: 1.5` is a form the lint refuses (§5.4). Printing "sem teto de preço"
  // for it would tell an operator the file is fine while nothing can be saved.
  const { api } = loadConsole();
  const parts = api.orderLineParts({ time_cap: 1.5 });
  assert.match(parts[2], /formato que o roteador não lê/);
  assert.doesNotMatch(parts[2], /sem teto de preço/);
  assert.doesNotMatch(parts[2], /padrão do motor/, 'a declared value is not a default');
});

test('renderLadder prints one Ordem line per group, with 1 + fallback.length attempts', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.renderLadder();

  const blocks = findAll(dom.get('ladder'), 'tier');
  assert.equal(blocks.length, 2, 'two groups in the fixture');
  const names = Object.keys(api.state.policy.tiers);
  blocks.forEach((block, i) => {
    const tier = api.state.policy.tiers[names[i]];
    const text = flat(block);
    const line = (text.match(/Ordem: [^]*?(?=Sem reserva|As tentativas|$)/) || [''])[0];
    assert.match(text, /Ordem: /, `group ${names[i]} has no Ordem line`);
    // The five parts survive the join: four separators between five values.
    const head = line.split('Ordem: ')[1] || '';
    assert.ok(head.split(' · ').length >= 5,
      `group ${names[i]} prints ${head.split(' · ').length} knobs, and the contract is five`);
    // CA4's other half, counted rather than assumed.
    const attempts = findAll(block, 'hop');
    assert.equal(attempts.length, 1 + (tier.fallback || []).length,
      `group ${names[i]} must list the primary plus every reserve`);
  });
});

test('the multiplier is stated once: the Ordem line owns it, the cap sentence explains it', () => {
  // DESIGN.md §2 rule 2 — one authority per fact. The consequence sentence used to
  // repeat the number ("acima de 1,5×"), so an operator reading two numbers had to
  // check they were the same one.
  const { api } = loadConsole();
  const tier = { model: 'x', provider: 'p', billing_mode: 'metered', time_cap: { max_multiplier: 1.5 } };
  const said = plain(api.timeKnobWords(tier)).join(' ');
  assert.match(said, /acima do teto/, 'the sentence says what the ceiling does');
  assert.doesNotMatch(said, /1\.5×/, 'and leaves the value to the line that owns it');
  assert.match(api.orderLine(tier), /teto de preço 1\.5×/);
});

// ── The chip prints its own sentence once ──────────────────────────────────
// The family used to be printed as a one-word label in front of the text, from
// when a chip's text was a fragment ("400.000 tokens"). With the text a whole
// sentence in pt-BR the label produced "contexto contexto estimado passa de
// 400.000 tokens" — the word twice, and once in English.
test('a condition chip reads as one phrase, with the family in the class and not in the text', () => {
  const { api } = loadConsole();
  const chips = plain(api.requirementChips({ min_context: 200000 }));
  const list = api.chipList(chips);
  const item = list.children[0];
  assert.equal(flat(list), 'pelo menos 200,000 tokens', 'the text, once, with no label in front of it');
  assert.match(String(item.className), /context/, 'the family survives where it is read: the class');
  // And the DATA keeps the family word — its own test pins it, and the plan's
  // rejection reasons read it.
  assert.equal(chips[0].kind, 'context');

  // The case the duplication was measured on: a rule condition whose text is a
  // sentence already containing the word.
  const chip = plain(api.predicateChip('est_input_tokens', { gt: 400000 }));
  assert.equal(flat(api.chipList([chip])), 'o contexto estimado passa de 400.000 tokens');
});
