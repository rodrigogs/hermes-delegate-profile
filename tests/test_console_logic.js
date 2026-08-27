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
// Button-driven async flows (renderInspector's Apply) do not return the promise
// they start, so a test drains the microtask+macrotask queue before asserting.
const tick = () => new Promise((resolve) => setImmediate(resolve));

const sourcePath = 'webui_extension/hermes-smart-router/console.html';

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
      // A detached node comes back WHERE IT WAS, so a test can assert the order the
      // flow reads (see what changes, then save) and not merely that it is present.
      insertBefore(k, ref) {
        const at = node.children.indexOf(ref);
        if (at < 0) node.children.push(k); else node.children.splice(at, 0, k);
        return k;
      },
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

// Comment-free view of console.html for the single-source literal count: string
// literals are preserved WHOLE (a literal may legitimately contain "//"), while
// JS comments, HTML comments and the <style> block go away. The console keeps
// its design rationale in comments — several discuss the write labels on
// purpose — so a count over the raw file would flag those, not a real copy.
function stripCommentsForCounting(src) {
  let out = '';
  let i = 0;
  const size = src.length;
  while (i < size) {
    const c = src[i];
    const n = src[i + 1];
    if (c === '<' && n === '!' && src.slice(i, i + 4) === '<!--') {
      i += 4;
      while (i + 2 < size && !(src[i] === '-' && src[i + 1] === '-' && src[i + 2] === '>')) i += 1;
      i += 3;
      continue;
    }
    if (c === '<' && src.slice(i, i + 7) === '<style>') {
      const end = src.indexOf('</style>', i);
      i = end < 0 ? size : end + 8;
      continue;
    }
    if (c === '/' && n === '/') {
      while (i < size && src[i] !== '\n') i += 1;
      continue;
    }
    if (c === '/' && n === '*') {
      i += 2;
      while (i + 1 < size && !(src[i] === '*' && src[i + 1] === '/')) i += 1;
      i += 2;
      continue;
    }
    if (c === "'" || c === '"' || c === '`') {
      const q = c;
      out += c;
      i += 1;
      while (i < size) {
        if (src[i] === '\\') { out += src[i] + (src[i + 1] || ''); i += 2; continue; }
        out += src[i];
        if (src[i] === q) { i += 1; break; }
        i += 1;
      }
      continue;
    }
    out += c;
    i += 1;
  }
  return out;
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
  assert.equal(api.say(true), 'sim');
  assert.equal(api.say(false), 'não');
  assert.equal(api.say(''), '—');
  assert.equal(api.say(null), '—');
  assert.equal(api.say(['a', 'b']), '2', 'a list reports its size, not its JSON');
  // Timestamps become elapsed time; an operator cares how stale, not the epoch.
  // The instant is the caller's to resolve, so the clock is PINNED instead of
  // inherited from the machine (DESIGN.md §7): the same text at any hour. This
  // once called Date.now() inside ago() and passed by luck whenever the suite ran.
  api.state.clock = PEAK;
  const now = Math.floor(PEAK.getTime() / 1000);
  assert.equal(api.ago(now - 5, api.state.clock), 'há 5s');
  assert.equal(api.ago(now - 600, api.state.clock), 'há 10m');
  assert.equal(api.ago(null, api.state.clock), '—');
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
  assert.equal(dom.get('editLabel').textContent, 'Editar',
    'reading mode offers the next action');
  assert.match(button.title, /Editar a política de roteamento/,
    'and the title says what gets edited');

  api.setMode('editing');
  assert.equal(dom.get('editLabel').textContent, 'Concluir');
  assert.equal(button.title, 'Parar de editar', 'the armed title says what ends the mode');
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
  assert.equal(api.absence('Nenhum modelo roteável informado.'), 'Carregando…');

  api.state.loading = false;
  api.state.unreachable = true;
  assert.match(api.absence('Nenhum modelo roteável informado.'), /não for possível falar com o roteador/);

  api.state.unreachable = false;
  assert.equal(api.absence('Nenhum modelo roteável informado.'), 'Nenhum modelo roteável informado.');
});

test('a failed reading is not rendered as an empty result for its consumer', async () => {
  const { api } = loadConsole({
    fetch: () => Promise.resolve({
      ok: false,
      status: 503,
      text: () => Promise.resolve(JSON.stringify({ error: 'sidecar token not provisioned' })),
    }),
  });
  const result = await api.call('/policy');
  api.state.loading = false;
  assert.equal(result.error, true);
  assert.match(api.absence('Este serviço do roteador não tem lista de regras.', '/policy'),
    /não encontrou o token que o WebUI escreveu/);
  assert.equal(api.absence('Este serviço do roteador não tem lista de regras.', '/routes'),
    'Este serviço do roteador não tem lista de regras.',
    'a failure in one reading must not erase a genuinely empty other reading');
});

test('a failed policy reading makes every policy absence claim actionable', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.readFailures['/policy'] = { status: 503, data: { error: 'sidecar token not provisioned' } };
  api.renderPresets();
  api.renderSheet();
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('presetOptions')), /não tem tabela de grupos/);
  assert.doesNotMatch(flat(dom.get('sheet')), /não tem lista de regras/);
  assert.doesNotMatch(flat(dom.get('ladder')), /Nenhum grupo/);
  assert.match(dom.get('pipelineNote').textContent, /não encontrou o token/);
  api.state.readFailures['/routes'] = { status: 503, data: { error: 'sidecar token not provisioned' } };
  api.renderRoutes();
  assert.doesNotMatch(flat(dom.get('routes')), /Nenhuma decisão gravada ainda/);
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
  // The screen shows a snapshot the file still matches, so the plan is reached.
  api.state.policy = { valid: false, errors: ['fail_safe missing'] };
  await api.doApply('/apply', msg, { rules: [] });

  assert.equal(posted.length, 2, 'the staleness read, then the plan — nothing else');
  assert.match(posted[0], /\/policy$/, 'the freshness guard reads before planning');
  assert.match(posted[1], /\/plan$/);
  assert.match(msg.textContent, /fail_safe missing/, 'the reason comes from the plan');
  assert.match(msg.className, /bad/);
});

test('a valid draft is planned and written in one action', async () => {
  const posted = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      posted.push(url);
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      const body = url.endsWith('/plan')
        ? { valid: true, policy: { rules: [] }, base_hash: 'abc' }
        : { ok: true };
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) });
    },
  });
  api.state.policy = {};
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { rules: [] });

  // The operator pressed one button; the console did the bookkeeping. A refresh
  // follows the write (load() re-reads every screen), so assert the ORDER of the
  // two that matter rather than the whole traffic — pinning the full list would
  // make this test fail the next time a screen is added.
  const paths = posted.map((u) => u.replace(/^.*sidecar/, ''));
  assert.equal(paths[1], '/plan', 'the plan comes first, unasked');
  assert.equal(paths[2], '/apply', 'and the write follows it immediately');
  assert.match(msg.textContent, /Vale para as próximas tarefas/, '§2.7: a written save says the temporal scope');
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
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '- old\n+ new' })),
      });
    },
  });
  api.state.policy = {};
  const msg = { textContent: '', className: '' };
  const diff = { hidden: true, textContent: '' };
  await api.doPreview({ rules: [] }, msg, diff);

  assert.equal(posted.length, 3, 'preview must not write: lint, freshness, plan');
  assert.match(posted[0], /\/lint$/, '§5.5: the preview revalidates first');
  assert.match(posted[1], /\/policy$/, 'then the staleness guard reads');
  assert.match(posted[2], /\/plan$/);
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
  assert.deepEqual(labels, ['Roteamento', 'classifier'],
    `only the two facts that exist nowhere else, got ${JSON.stringify(labels)}`);
  const text = flat(dom.get('healthFacts'));
  assert.match(text, /glm-4\.7/);
  assert.doesNotMatch(text, /error/, 'the lint banner owns the invalid count');
  assert.doesNotMatch(text, /rules/, 'the sheet owns the rules count');
});

test('the routing fact says Roteamento ligado/desligado, the operator words (§3.2)', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], classifier: { model: 'glm-4.7' } };
  api.state.status = { enabled: true, validation_errors: [] };
  api.renderHealth();
  let text = flat(dom.get('healthFacts'));
  assert.match(text, /Roteamento/, 'the label is the §3.2 word, not the raw key');
  assert.match(text, /ligado/);
  api.state.status = { enabled: false, validation_errors: [] };
  api.renderHealth();
  text = flat(dom.get('healthFacts'));
  assert.match(text, /desligado/);
  assert.doesNotMatch(text, /\bon\b|\boff\b/, 'the English value pair never reaches the Health column');
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
  const failSafe = rows.find((c) => /último recurso/i.test(c.textContent));
  assert.ok(failSafe, 'a fail-safe row is rendered');
  assert.match(failSafe.title, /rede de segurança/);
  const veto = rows.find((c) => /bloqueio/i.test(c.textContent));
  assert.ok(veto, 'a veto row is rendered');
  assert.match(veto.title, /recusou/);
});

// ── the cause is read in Portuguese, over the closed vocabulary ─────────
// Card t_e10949c5: the column rendered the raw log enum ("PROFILE IGNORED",
// "HAS CODE RULE") on a screen that is otherwise all pt-BR — and the most
// frequent label asserted the OPPOSITE of the mechanism: profile_ignored
// read as "the profile was ignored" when the router's choice was refused
// BECAUSE it would move the worker's role and the profile prevailed. The
// closed set lives in router/decision_log.py (VALID_CAUSES); the Python
// suite pins this map against that set member-for-member, so what these
// tests pin is the WORDS and the rendering path.

// The exact phrases, transcribed once so the assertions below and the
// static parity test stay in agreement with the map.
const CAUSE_WORDS_EXPECTED = {
  blocklist_veto: 'Bloqueio',
  breaker_cooldown: 'Auto-bloqueio',
  keyword_match: 'Por palavra',
  size_rule: 'Por tamanho',
  has_code_rule: 'Por ter código',
  hard_rule: 'Verbo difícil',
  classifier: 'Classificador',
  session_pin: 'Piso da sessão',
  default_fallthrough: 'Nenhuma casou',
  fail_safe_strong: 'Último recurso',
  profile_ignored: 'Valeu o perfil',
  role_out_of_scope: 'Só o modelo',
  selection_vetoed: 'Seleção vetada',
  unknown_cause: 'Desconhecida',
};

test('every closed-set cause renders as its pt-BR word', () => {
  const { api } = loadConsole();
  // The whole set, one by one: a member rendered raw (or as the wrong word)
  // must fail HERE, not in front of an operator.
  for (const [cause, word] of Object.entries(CAUSE_WORDS_EXPECTED)) {
    assert.equal(api.causeWord(cause), word, `${cause} must read as the operator's word`);
  }
  // The two mechanisms the card names explicitly, checked as WORDS because
  // they were the misread: profile_ignored says who PREVAILED (the profile),
  // role_out_of_scope says which half applied (the model's).
  assert.match(api.causeWord('profile_ignored'), /perfil/i);
  assert.match(api.causeWord('role_out_of_scope'), /modelo/i);
  assert.doesNotMatch(api.causeWord('profile_ignored'), /ignorado/i,
    'the word that asserted the opposite of the mechanism must be gone');
});

test('the two words the operator misread are pinned verbatim', () => {
  const { api } = loadConsole();
  // profile_ignored (135 of 158 measured decisions) read as "the profile was
  // ignored" while the mechanism was the opposite: the router's choice was
  // refused for wanting to move the role, so the card's profile prevailed.
  assert.equal(api.causeWord('profile_ignored'), 'Valeu o perfil');
  // role_out_of_scope: the rule's model half applied; the role half was
  // never this path's to move — whoever creates the card already chose it.
  assert.equal(api.causeWord('role_out_of_scope'), 'Só o modelo');
});

test('a cause outside the closed set renders raw, underscores and all', () => {
  const { api } = loadConsole();
  // The screen must not hide a caller that invented vocabulary: an unknown
  // cause stays visible AS IT CAME, '_' for ' ', exactly as before.
  assert.equal(api.causeWord('caboose_invented'), 'caboose invented');
  assert.equal(api.causeWord('TWO_WORDS'), 'TWO WORDS');
  // Empty/absent cause is the row's em dash, never a translated word.
  assert.equal(api.causeWord(''), '');
  assert.equal(api.causeWord(null), '');
  assert.equal(api.causeWord(undefined), '');
});

test('both rendering points go through the one map', () => {
  // The row's column and the replay step's chip are the two surfaces a
  // cause reaches; if either kept its own replace, the screen would speak
  // two vocabularies for the same fact. Both are asserted on the DOM the
  // console itself built, not on the function in isolation.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [
    { id: 'r1', cause: 'profile_ignored', model: 'glm-5.3', provider: 'zai', task: 't', ts: 1 },
  ];
  api.renderRoutes();
  const column = findAll(dom.get('routesTable'), 'cause')[0];
  assert.equal(column.textContent, 'Valeu o perfil',
    'the row column reads the map');

  api.state.replay = {
    id: 'r1', at: 0,
    steps: [{ stage: 'rules', cause: 'profile_ignored', out: { rule_id: 'hard-verbs' } }],
  };
  api.drawPath();
  const chip = findAll(dom.get('replayPath'), 'cause')[0];
  assert.equal(chip.textContent, 'Valeu o perfil',
    'the replay chip reads the same map');
});

test('a raw cause stays raw on the rendered row too', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [
    { id: 'r1', cause: 'caboose_invented', model: 'glm-5.3', provider: 'zai', task: 't', ts: 1 },
  ];
  api.renderRoutes();
  const column = findAll(dom.get('routesTable'), 'cause')[0];
  assert.equal(column.textContent, 'caboose invented',
    'the invented vocabulary is visible, not translated away');
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
  assert.match(value.textContent, /há \d+[smhd] · 03:20 UTC$/,
    'the age and the hour ride the same column, in the unit windows are declared in');
});

test("a decision row's age is priced off the pinned clock, not the machine", () => {
  // §7: the row used to call ago() with one argument, which read Date.now()
  // inside — the exact class of error that makes a rendering test pass at
  // 05:00 UTC and fail at 07:00. With the clock pinned, the whole row is the
  // same text at any machine hour.
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;                 // 2026-08-17 07:14 UTC
  api.state.loading = false;
  api.state.routes = [{ id: 'r1', cause: 'hard_rule', model: 'gpt-5.6-terra', task: 't', ts: TRACE_AT }];
  api.renderRoutes();
  const value = dom.get('routesTable').children[0].children[2];
  assert.equal(value.textContent, 'há 4h · 03:20 UTC',
    'the age comes from the injected clock — identical text whatever hour the suite runs at');
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
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '', base_hash: 'h' })),
      });
    },
  });
  api.state.policy = {};
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { rules: [] });

  assert.equal(posted.length, 2, 'the staleness read, then the plan; the write must not');
  assert.match(posted[0], /\/policy$/);
  assert.match(posted[1], /\/plan$/);
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
        url.endsWith('/policy')
          ? {} // the disk matches the snapshot the screen rendered
          : (url.endsWith('/plan')
            ? { valid: true, policy: {}, base_hash: 'h', diff: '-  - id: URGENT-block-prod\n' }
            : { ok: true }),
      )),
    }),
  });
  api.state.policy = {};
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
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      return new Promise((resolve) => {
        setTimeoutReal(() => resolve({
          ok: true, status: 200,
          text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, base_hash: 'h', diff: '+x' })),
        }), 5);
      });
    },
  });
  api.state.policy = {};
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

test('the probe verdict says Roteando… in flight, in pt-BR (§3.2)', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { api, dom } = loadConsole({
    fetch: (url) => {
      if (String(url).includes('/explain')) {
        return gate.then(() => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({
          mode: 'deterministic_dry_run',
          decision: { matched_rule_id: 'r1', output: { model: 'gpt-5.6-terra', provider: 'openai-codex' } },
        })) }));
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.loading = false;
  api.state.policy = { rules: [{ id: 'r1', then: { model: 'T4' } }], tiers: { T4: {} } };
  const run = api.probe('Debug a race condition in the cache');
  await tick();
  assert.match(flat(dom.get('probeResult')), /Roteando…/, 'the in-flight verdict is the pt-BR word');
  release();
  await run;
});

test('the iOS zoom guard names the classes, or it does nothing at all', () => {
  // This block existed and was DECORATIVE. `input, textarea, select` scores
  // (0,0,1); every input in this console is reached by a class — .probe-input,
  // .editor, .field input — which scores (0,1,0) and wins. Measured in a real
  // iPhone 13 context (the only way (pointer:coarse) genuinely matches): the probe
  // field and the decision filter computed 14px with
  // the guard present, so iOS would zoom in on focus and never zoom back out.
  // After naming the classes, they measure 16px.
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
    capability_unknown: /capacidades não verificadas/,
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
  assert.match(text, /capacidades não verificadas/);
  assert.match(dom.get('ladderNote').textContent, /capacidades não verificadas/,
    'and the group head counts them, so the gap is visible without reading every row');
});

test('a tier with one elo and no fallback names the consequence (§2.8)', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: { model: 'glm-4.7', provider: 'zai', billing_mode: 'plan' } } };
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /Este grupo tem só uma opção\. Se ela falhar, a tarefa vai direto para o último recurso\./);
  assert.match(text, /zai/, 'the chain row still shows the rail');
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

// ── §2.3: who uses each group (the inverted index of rules[].then.model) ────

function usedByPolicy() {
  return {
    rules: [
      { id: 'a', when: { keywords: { contains: 'audit' } }, then: { model: 'T2' } },
      { id: 'b', when: { has_code: { eq: true } }, then: { model: 'T1' } },
      { id: 'c', when: { keywords: { contains: 'review' } }, then: { model: 'T2' } },
    ],
    default: {},
    tiers: {
      T1: { model: 'glm-4.7', provider: 'zai' },
      T2: { model: 'glm-5.3', provider: 'zai', fallback: [{ model: 'gpt-5.5', provider: 'openai-codex' }] },
      T3: { model: 'mimo-v2.5', provider: 'xiaomi' },
      T4: { model: 'gpt-5.6-terra', provider: 'openai-codex' },
    },
  };
}

test('each group names who uses it — one, two, or nobody (nobody gets no line)', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = usedByPolicy();
  api.renderLadder();

  const text = flat(dom.get('ladder'));
  // The titles are the sheet's own (ruleTitle), so the same rule reads the same
  // on both surfaces. File order inside the line: a then T2, c then T2.
  assert.match(text, /Usado por: Trabalho de código padrão/, 'T1 is used by one rule');
  assert.match(text, /Usado por: Pedido de auditoria, Pedido de revisão/, 'T2 by two, in file order');
  // T3 and T4 are used by nobody: "Usado por: ninguém" is a frame around nothing.
  assert.doesNotMatch(text, /Usado por: ninguém/);
  const lines = findAll(dom.get('ladder'), 'tier-fact')
    .filter((n) => String(n.textContent || '').startsWith('Usado por:'));
  assert.equal(lines.length, 2, 'exactly the two groups with consumers carry the line');
});

test('the default enters the Usado por line named as the destino padrão', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  const policy = usedByPolicy();
  policy.default = { model: 'T2' };
  api.state.policy = policy;
  api.renderLadder();

  const text = flat(dom.get('ladder'));
  assert.match(text, /Usado por: Pedido de auditoria, Pedido de revisão, e o destino padrão/,
    'the default follows the rules, named as what it is');
  // The inverted index must not put the default under the WRONG group.
  assert.doesNotMatch(text, /Usado por: [^.]*e o destino padrão[^.]*Trabalho de código padrão/,
    'the default belongs to T2, not to T1');
});

test('a group used only by the default says just that', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [],
    default: { model: 'T1' },
    tiers: { T1: { model: 'glm-4.7', provider: 'zai' }, T2: { model: 'glm-5.3', provider: 'zai' } },
  };
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /Usado por: o destino padrão/, 'no leading comma, no "e" for an empty rule list');
  assert.doesNotMatch(text, /e o destino padrão/);
});

// ── §2.6 + §5.4: o último recurso como bloco próprio na aba Modelos ─────────

test('the last-resort block draws the fail_safe chain with the same chain renderer', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [], default: {},
    fail_safe: {
      model: 'glm-4.7', provider: 'zai', billing_mode: 'plan',
      fallback: [
        { model: 'gpt-5.6-luna', provider: 'openai-codex', billing_mode: 'subscription' },
        { model: 'mimo-v2.5', provider: 'xiaomi', billing_mode: 'metered' },
      ],
    },
    tiers: {},
  };
  api.renderFailSafe();

  const box = dom.get('failSafeBox');
  const text = flat(box);
  assert.match(text, /Último recurso/, 'the block carries the §2.6 heading');
  assert.match(text, /glm-4\.7/);
  assert.match(text, /gpt-5\.6-luna/);
  assert.match(text, /mimo-v2\.5/);
  assert.match(text, /Esta fila não passa pelos grupos\. É a última coisa que o roteador tenta\./);
  const lists = findAll(box, 'hops');
  assert.equal(lists.length, 1, 'one chain, drawn by the same chainList the groups use');
  assert.match(lists[0].className, /ordered/, 'a fixed queue is drawn in the order it runs');
  assert.deepEqual(findAll(lists[0], 'hop-ord').map((n) => n.textContent), ['1', '2', '3'],
    'and numbered like a group chain — no surface mints its own chain vocabulary');
});

test('a missing fail_safe is NO block at all — only the §5.4 phrase where it would be', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.renderFailSafe();

  const box = dom.get('failSafeBox');
  const text = flat(box);
  assert.match(text, /Não há último recurso configurado\. Se todas as filas falharem, a tarefa falha\./);
  // "bloco ausente" (DESIGN.md rule 1): no heading (the phrase's own lowercase
  // "último recurso" is the sentence, not the §2.6 heading), no group frame,
  // no chain.
  assert.doesNotMatch(text, /Último recurso/, 'the block heading does not exist');
  assert.equal(findAll(box, 'group').length, 0, 'no block frame around nothing');
  assert.equal(findAll(box, 'hops').length, 0, 'no chain is drawn for a non-config');

  // An empty fail_safe object is the same fact as an absent one — and so is a
  // fail_safe with reserves but no primary (the sheet's own presence test).
  api.state.policy = { rules: [], default: {}, fail_safe: {}, tiers: {} };
  api.renderFailSafe();
  assert.match(flat(box), /Não há último recurso configurado/, 'an empty fail_safe is not configured');
  api.state.policy = { rules: [], default: {}, fail_safe: { fallback: [{ model: 'glm-4.7', provider: 'zai' }] }, tiers: {} };
  api.renderFailSafe();
  assert.match(flat(box), /Não há último recurso configurado/, 'no primary means no last resort');
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
  assert.match(text, /Não é possível salvar enquanto houver erro\. 1 erro\(s\) no arquivo\./);
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
  assert.match(text, /Não é possível salvar enquanto houver erro\. 1 erro\(s\) no arquivo\./);
  assert.match(text, /Ir para a regra 7/, 'the button names the row by its sheet ordinal (index 6 + 1), in the one label this jump has');
});

test('a config-level error (no target) stays dead text — no invented button', () => {
  const { api, dom } = loadConsole();
  api.state.status = { validation_errors: ["missing mandatory 'default' routing"], error_targets: [null] };
  api.renderWarnings();
  assert.doesNotMatch(flat(dom.get('warnings')), /Ir para a regra/, 'no rule exists to jump to');
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
  assert.match(gate, /Ir para a regra 7/, 'the fix path rides in the result space too');
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
  assert.match(flat(dom.get('probeResult')), /Ir para a regra 7/);
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
  const btn = line.children.find((c) => c.textContent === 'Ir para a regra 7');
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
    .children.find((c) => c.textContent === 'Ir para a regra 3')._listeners.click();
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

// ── THE REASON: the clause that proves a rule never decides ────────────
// The lint says a pair is shadowed; this says WHY — which predicate of the
// later rule is a subset of which predicate of the earlier — and only when
// the screen can vouch for it. The detector is a pure function over
// rules[].when (no server field, no heuristic). The three provable shapes
// — one numeric bound on the same key, one membership set on the same key,
// exact equality — are each pinned here as a positive, and the silences
// (disjoint, different condition types, a key on only one side, equal
// values on different keys, an indirect substring cover, an empty when, a
// disabled row) as negatives. Uncertainty must not become text.

test('the detector proves a pair only by one of the three shapes — 15 pinned cases', () => {
  const { api } = loadConsole();
  const cases = [
    // Positives — one per provable shape.
    { name: 'gte floor: the later threshold is higher', rules: [
      { id: 'a', when: { est_input_tokens: { gte: 128000 } } },
      { id: 'b', when: { est_input_tokens: { gte: 200000 } } },
    ], expect: [{ earlier_index: 0, later_index: 1, earlier_id: 'a', later_id: 'b', key: 'est_input_tokens', op: 'gte', v_earlier: 128000, v_later: 200000 }] },
    { name: 'lte ceiling: the later threshold is lower', rules: [
      { id: 'a', when: { est_input_tokens: { lte: 200000 } } },
      { id: 'b', when: { est_input_tokens: { lte: 100000 } } },
    ], expect: [{ earlier_index: 0, later_index: 1, earlier_id: 'a', later_id: 'b', key: 'est_input_tokens', op: 'lte', v_earlier: 200000, v_later: 100000 }] },
    { name: 'gt floor', rules: [
      { id: 'a', when: { est_input_tokens: { gt: 128000 } } },
      { id: 'b', when: { est_input_tokens: { gt: 200000 } } },
    ], expect: [{ earlier_index: 0, later_index: 1, earlier_id: 'a', later_id: 'b', key: 'est_input_tokens', op: 'gt', v_earlier: 128000, v_later: 200000 }] },
    { name: 'lt ceiling', rules: [
      { id: 'a', when: { est_input_tokens: { lt: 200000 } } },
      { id: 'b', when: { est_input_tokens: { lt: 100000 } } },
    ], expect: [{ earlier_index: 0, later_index: 1, earlier_id: 'a', later_id: 'b', key: 'est_input_tokens', op: 'lt', v_earlier: 200000, v_later: 100000 }] },
    { name: 'set: the later membership fits inside the earlier', rules: [
      { id: 'a', when: { lang: { in: ['py', 'ts'] } } },
      { id: 'b', when: { lang: { in: ['py'] } } },
    ], expect: [{ earlier_index: 0, later_index: 1, earlier_id: 'a', later_id: 'b', key: 'lang', op: 'in', v_earlier: ['py', 'ts'], v_later: ['py'] }] },
    { name: 'exact equality', rules: [
      { id: 'a', when: { needs_vision: { eq: true } } },
      { id: 'b', when: { needs_vision: { eq: true } } },
    ], expect: [{ earlier_index: 0, later_index: 1, earlier_id: 'a', later_id: 'b', key: 'needs_vision', op: 'eq', v_earlier: true, v_later: true }] },
    { name: 'the later rule may add clauses — narrower is still dead', rules: [
      { id: 'a', when: { est_input_tokens: { gte: 128000 } } },
      { id: 'b', when: { est_input_tokens: { gte: 200000 }, lang: { in: ['py'] } } },
    ], expect: [{ earlier_index: 0, later_index: 1, earlier_id: 'a', later_id: 'b', key: 'est_input_tokens', op: 'gte', v_earlier: 128000, v_later: 200000 }] },

    // Negatives — each is a silence, never a warning.
    { name: 'disjoint bounds on the same key', rules: [
      { id: 'a', when: { est_input_tokens: { gte: 200000 } } },
      { id: 'b', when: { est_input_tokens: { lte: 100000 } } },
    ], expect: [] },
    { name: 'the later bound is looser — the subset runs the wrong way', rules: [
      { id: 'a', when: { est_input_tokens: { gte: 200000 } } },
      { id: 'b', when: { est_input_tokens: { gte: 128000 } } },
    ], expect: [] },
    { name: 'same key, different condition types', rules: [
      { id: 'a', when: { est_input_tokens: { in: ['py'] } } },
      { id: 'b', when: { est_input_tokens: { gte: 200000 } } },
    ], expect: [] },
    { name: 'a key only on one side', rules: [
      { id: 'a', when: { est_input_tokens: { gte: 128000 }, needs_vision: { eq: true } } },
      { id: 'b', when: { est_input_tokens: { gte: 200000 } } },
    ], expect: [] },
    { name: 'equal values on different keys', rules: [
      { id: 'a', when: { num_files: { gte: 3 } } },
      { id: 'b', when: { size_lines: { gte: 3 } } },
    ], expect: [] },
    { name: 'indirect cover (substring) — the three shapes refuse', rules: [
      { id: 'a', when: { keywords: { contains: 'aud' } } },
      { id: 'b', when: { keywords: { contains: 'audit' } } },
    ], expect: [] },
    { name: 'an empty when proves nothing', rules: [
      { id: 'a', when: {} },
      { id: 'b', when: { est_input_tokens: { gte: 200000 } } },
    ], expect: [] },
    { name: 'a disabled rule neither shadows nor is shadowed', rules: [
      { id: 'a', enabled: false, when: { est_input_tokens: { gte: 128000 } } },
      { id: 'b', when: { est_input_tokens: { gte: 200000 } } },
    ], expect: [] },
  ];
  cases.forEach((c) => {
    assert.deepEqual(plain(api.shadowPairs(c.rules)), c.expect, c.name);
  });
});

test('with the rules in hand the warning names the covering rule, not just its number', () => {
  const { api } = loadConsole();
  const rules = [
    { id: 'huge-context-read', when: { est_input_tokens: { gte: 128000 } } },
    { id: 'dead', when: { est_input_tokens: { gte: 200000 } } },
  ];
  const pairs = api.shadowPairs(rules);
  // Índice E frase: o número localiza a linha, a frase a identifica. Um operador que
  // só recebe "a regra 1" tem de contar linhas para saber quem o atropelou.
  assert.equal(api.shadowReasonWords(pairs[0], rules),
    'a regra 2 nunca decide: a regra 1, Leitura de contexto enorme, já cobre todo caso desta (est_input_tokens >= 200.000 é subconjunto de >= 128.000); mova-a acima da 1');
  // Sem as regras em mão, a sentença é a de sempre — o enriquecimento é aditivo e
  // nunca inventa um nome que o chamador não forneceu.
  assert.equal(api.shadowReasonWords(pairs[0]),
    'a regra 2 nunca decide: a regra 1 já casa tudo que ela pede (est_input_tokens >= 200.000 é subconjunto de >= 128.000); mova-a acima da 1');
});

test('the warning is one sentence naming the pair, the clause and the remedy', () => {
  const { api } = loadConsole();
  const pairs = api.shadowPairs([
    { id: 'broad', when: { est_input_tokens: { gte: 128000 } } },
    { id: 'dead', when: { est_input_tokens: { gte: 200000 } } },
  ]);
  assert.equal(api.shadowReasonWords(pairs[0]),
    'a regra 2 nunca decide: a regra 1 já casa tudo que ela pede (est_input_tokens >= 200.000 é subconjunto de >= 128.000); mova-a acima da 1');

  const sets = api.shadowPairs([
    { id: 'a', when: { lang: { in: ['py', 'ts'] } } },
    { id: 'b', when: { lang: { in: ['py'] } } },
  ]);
  assert.equal(api.shadowReasonWords(sets[0]),
    'a regra 2 nunca decide: a regra 1 já casa tudo que ela pede (lang ∈ {py} é subconjunto de ∈ {py, ts}); mova-a acima da 1');

  const eqs = api.shadowPairs([
    { id: 'a', when: { needs_vision: { eq: true } } },
    { id: 'b', when: { needs_vision: { eq: true } } },
  ]);
  assert.equal(api.shadowReasonWords(eqs[0]),
    'a regra 2 nunca decide: a regra 1 já casa tudo que ela pede (needs_vision = sim é subconjunto de = sim); mova-a acima da 1');
});

test('the sheet warns with the exact reason and two jumps only when the detector proves a pair', () => {
  const { api, dom } = loadConsole();
  // No provable pair → no warning text, no jump, and the count says all rules count.
  api.state.policy = {
    rules: [
      { id: 'a', when: { est_input_tokens: { gte: 200000 } } },
      { id: 'b', when: { est_input_tokens: { gte: 128000 } } },
    ],
  };
  api.renderSheet();
  assert.doesNotMatch(flat(dom.get('sheet')), /nunca decide/);
  assert.match(dom.get('pipelineNote').textContent, /Todas valem/);

  // A provable pair → the dead row carries the one-line cause+remedy and
  // BOTH destinations under the single jump label.
  api.state.policy = {
    rules: [
      { id: 'a', when: { est_input_tokens: { gte: 128000 } } },
      { id: 'b', when: { est_input_tokens: { gte: 200000 } } },
    ],
  };
  api.renderSheet();
  assert.match(dom.get('pipelineNote').textContent, /1 está sem efeito/);
  const dead = dom.get('sheet').children.find((c) => c.dataset.ruleId === 'b');
  assert.ok(dead, 'the dead row exists');
  const warn = dead.children.find((c) => c.className === 'step-when');
  assert.ok(warn, 'the reason rides the row itself');
  assert.match(flat(warn), /a regra 2 nunca decide: a regra 1, Leitura de contexto enorme, já cobre todo caso desta \(est_input_tokens >= 200\.000 é subconjunto de >= 128\.000\); mova-a acima da 1/);
  const buttons = findAll(warn, 'btn');
  assert.equal(buttons.length, 2, 'the shadower AND the dead rule are both one click away');
  assert.deepEqual(buttons.map((b) => b.textContent), ['Ir para a regra 2', 'Ir para a regra 1']);

  // Each jump opens its own inspector: the dead rule's (where the move
  // button lives) and the shadower's.
  buttons[0]._listeners.click();
  assert.equal(api.state.selected, 'rule:b');
  buttons[1]._listeners.click();
  assert.equal(api.state.selected, 'rule:a');
});

test('when two rules shadow the same row, the warning names the EARLIEST and counts the row once', () => {
  const { api, dom } = loadConsole();
  api.state.policy = {
    rules: [
      { id: 'a', when: { est_input_tokens: { gte: 128000 } } },
      { id: 'b', when: { est_input_tokens: { gte: 200000 } } },
      { id: 'c', when: { est_input_tokens: { gte: 300000 } } },
    ],
  };
  api.renderSheet();
  const dead = dom.get('sheet').children.find((c) => c.dataset.ruleId === 'c');
  const warn = dead.children.find((c) => c.className === 'step-when');
  assert.match(flat(warn), /a regra 3 nunca decide: a regra 1, Leitura de contexto enorme, já cobre todo caso desta/);
  assert.deepEqual(findAll(warn, 'btn').map((b) => b.textContent),
    ['Ir para a regra 3', 'Ir para a regra 1'],
    'moving before the earliest shadower resolves every finding at once');
  // Two dead rows (b and c), not three findings: the count is rules, and
  // the plural agrees.
  assert.match(dom.get('pipelineNote').textContent, /2 estão sem efeito/);
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
    // The well-formed control: a whole-hour cheap window, and the answer both
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
  // peaks and glm-4.7 the weekday-gated one. No registry entry declares a CHEAP
  // window any more, so that exemplar is declared inline below.
  const deepseek = api.eloWindows(catalogueEntry('deepseek-v4-flash'));
  const zai = api.eloWindows(catalogueEntry('glm-4.7'));
  // O exemplar de janela BARATA e sem porta de dia é declarado aqui, não lido do
  // registry: o 0,8× do xiaomi era escopado ao Token Plan pré-pago e saiu das
  // entradas em 2026-08-26 (este install é pay-as-you-go). Um teste de mecanismo
  // não pode depender da promoção vigente de um fornecedor para ter exemplo.
  const desconto = api.eloWindows({ price_windows: [{ hours_utc: [16, 24], multiplier: 0.8 }] });

  // deepseek: both peaks, Mon-Fri. The vendor narrowed it to weekdays on
  // 2026-08-22 (silent edit of the pricing page, absent from the changelog).
  assert.equal(api.priceMultiplier(deepseek, { hour: 1, weekday: 0 }), 2);
  assert.equal(api.priceMultiplier(deepseek, { hour: 3, weekday: 4 }), 2, 'Friday still peaks');
  assert.equal(api.priceMultiplier(deepseek, { hour: 3, weekday: 6 }), 1, 'Sunday bills off-peak');
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
  // The ungated exemplar is xiaomi's night discount: it carries no `weekdays`, so
  // an unknown day cannot block it. deepseek used to play this role and stopped
  // being ungated on 2026-08-22 — a test that needs "ungated" must not depend on
  // a vendor's current calendar to still have one.
  assert.equal(api.priceMultiplier(desconto, { hour: 18, weekday: null }), 0.8,
    'an ungated window still matches — an unknown day only blocks a gated one');
  assert.equal(api.priceMultiplier(deepseek, { hour: 7, weekday: null }), 1,
    'and deepseek is gated now, so an unknown day blocks it too');

  // The other direction — a discount, not a peak. And the real xiaomi entry is
  // flat now, which is the fact this pair of assertions keeps honest.
  assert.equal(api.priceMultiplier(desconto, { hour: 18, weekday: 0 }), 0.8);
  assert.equal(api.priceMultiplier(desconto, { hour: 23, weekday: 0 }), 0.8);
  assert.equal(api.priceMultiplier(desconto, { hour: 0, weekday: 0 }), 1, 'half-open at midnight, so no wrap-around');
  assert.equal(api.priceMultiplier(api.eloWindows(catalogueEntry('mimo-v2.5')), { hour: 18, weekday: 0 }), 1,
    'xiaomi bills flat: the 0.8x is a prepaid Token Plan credit rate, not a metered one');

  // The two primary rails share the 06:00-10:00 peak, which is the fact that
  // makes overnight cron traffic pay double on both at once.
  assert.equal(api.priceMultiplier(deepseek, { hour: 8, weekday: 2 }), 2);
  assert.equal(api.priceMultiplier(zai, { hour: 8, weekday: 2 }), 2);
});

test('the next change is a real hour, so "until when" is not invented', () => {
  const { api } = loadConsole();
  const deepseek = api.eloWindows(catalogueEntry('deepseek-v4-flash'));
  const zai = api.eloWindows(catalogueEntry('glm-4.7'));
  // O exemplar de janela BARATA e sem porta de dia é declarado aqui, não lido do
  // registry: o 0,8× do xiaomi era escopado ao Token Plan pré-pago e saiu das
  // entradas em 2026-08-26 (este install é pay-as-you-go). Um teste de mecanismo
  // não pode depender da promoção vigente de um fornecedor para ter exemplo.
  const desconto = api.eloWindows({ price_windows: [{ hours_utc: [16, 24], multiplier: 0.8 }] });

  const out = plain(api.nextWindowChange(deepseek, { hour: 7, weekday: 0 }));
  assert.equal(out.hour, 10, 'the peak ends at 10:00 UTC');
  assert.equal(out.hoursAhead, 3);
  assert.equal(out.multiplier, 1);
  // From base, the next change is the peak OPENING.
  assert.equal(api.nextWindowChange(deepseek, { hour: 5, weekday: 0 }).hour, 6);
  assert.equal(api.nextWindowChange(desconto, { hour: 12, weekday: 0 }).hour, 16);
  assert.equal(api.nextWindowChange(desconto, { hour: 18, weekday: 0 }).hour, 0, 'the discount ends at midnight');
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
  assert.equal(api.nextWindowChange(deepseek, { hour: 7, weekday: null }), null,
    'deepseek is weekday-gated since 2026-08-22, so it answers null too');
  assert.equal(api.nextWindowChange(desconto, { hour: 18, weekday: null }).hour, 0,
    'an ungated window still answers — the discount ends at midnight');
});

test('a rail says what it costs now and until when, in one clause', () => {
  const { api } = loadConsole();
  const deepseek = api.eloWindows(catalogueEntry('deepseek-v4-flash'));
  // O exemplar de janela BARATA e sem porta de dia é declarado aqui, não lido do
  // registry: o 0,8× do xiaomi era escopado ao Token Plan pré-pago e saiu das
  // entradas em 2026-08-26 (este install é pay-as-you-go). Um teste de mecanismo
  // não pode depender da promoção vigente de um fornecedor para ter exemplo.
  const desconto = api.eloWindows({ price_windows: [{ hours_utc: [16, 24], multiplier: 0.8 }] });
  assert.equal(api.windowWords(deepseek, { hour: 7, weekday: 0 }), '2× em hora de pico até 10:00 UTC');
  assert.equal(api.windowWords(deepseek, { hour: 12, weekday: 0 }), 'tarifa base até 01:00 UTC, depois 2×');
  assert.equal(api.windowWords(desconto, { hour: 18, weekday: 0 }), '0.8× em hora barata até 00:00 UTC');
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

// ── Card t_eed59abb: the price ceiling bites per hour, and the row says who ──
// A ceiling is not a property of the elo, it is a property of THE HOUR: the
// same group shows four attempts of which one is affordable at 14:00 UTC and
// all four at 03:00. The mark reads the multiplier at the hour in use against
// the group's max_multiplier — strictly above, the same boundary
// capabilities.apply_time_cap draws (multiplier - cap <= 1e-9 is eligible).

// Four attempts with windows that put three of them over a 1.5× ceiling at
// 14:00 UTC and none over at 03:00 UTC. The plan carries no hour of its own,
// so the console reads the declared windows at the hour state.clock pins —
// the one authority the injected clock exists to be (DESIGN.md §3c).
function capChain(extra) {
  return chainPlan(Object.assign({
    time_cap: { max_multiplier: 1.5 },
    chain: [
      { model: 'm-before', provider: 'deepseek', price_windows: [{ hours_utc: [6, 10], multiplier: 2 }] },
      { model: 'm-noon', provider: 'zai', price_windows: [{ hours_utc: [10, 16], multiplier: 2 }] },
      { model: 'm-after', provider: 'xiaomi', price_windows: [{ hours_utc: [12, 18], multiplier: 2 }] },
      { model: 'm-late', provider: 'openai-codex', price_windows: [{ hours_utc: [13, 17], multiplier: 2 }] },
    ],
  }, extra || {}));
}

test('the cap mark counts the attempts above the ceiling at the hour in use', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  const at = (hour) => {
    api.state.clock = new Date(Date.UTC(2026, 7, 17, hour, 0));
    api.renderChainPlan(capChain());
  };
  at(14);
  const box = dom.get('chainPlan');
  assert.equal(findAll(box, 'hop-cap').length, 3,
    'at 14:00 UTC three of the four attempts bill above the 1.5× ceiling');
  assert.match(flat(box), /acima do teto agora/);
  assert.match(flat(box), /entra no teto às 16:00 UTC/,
    "m-noon's window ends at 16:00, when the multiplier falls back within the cap");
  at(3);
  assert.equal(findAll(dom.get('chainPlan'), 'hop-cap').length, 0,
    'at 03:00 UTC every attempt is at base — the ceiling is not biting');
});

test('no time_cap means no cap mark anywhere (rule 4)', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.clock = new Date(Date.UTC(2026, 7, 17, 14, 0));
  // Same windows, same hour — three of the four would bill over 1.5× — but the
  // plan declares no ceiling, so the console must not invent one: a cap line
  // without a cap would assert a policy the file lacks (DESIGN.md rule 1).
  api.renderChainPlan(capChain({ time_cap: undefined }));
  assert.equal(findAll(dom.get('chainPlan'), 'hop-cap').length, 0);
  // The pure contract, called directly: Number(null) is 0, and a 0× ceiling
  // would mark every attempt above it — the exact failure rule 4 forbids.
  assert.equal(api.legibleCap({ max_multiplier: 1.5 }), 1.5, 'the documented mapping is legible');
  assert.equal(api.legibleCap(1.5), null, 'a bare number the lint refuses is not a ceiling');
  assert.equal(api.legibleCap(undefined), null, 'absent is no cap');
  assert.equal(api.capOverWords(null, 2, [], { hour: 14, weekday: 0 }), '',
    'a null ceiling marks nothing, even called directly');
  assert.equal(api.capOverWords(undefined, 2, [], { hour: 14, weekday: 0 }), '');
});

test('a ceiling the lint refuses earns no mark — the Ordem line owns that sentence', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.clock = PEAK;   // 07:14 UTC: both elos inside their 2.0× window
  api.state.policy = {
    rules: [], default: {},
    tiers: {
      T1: {
        model: 'glm-4.7', provider: 'zai', billing_mode: 'plan',
        fallback: [{ model: 'gpt-5.6-luna', provider: 'openai-codex', billing_mode: 'subscription' }],
        time_cap: 1.5,
      },
    },
  };
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', billing_mode: 'plan', price_windows: [{ hours_utc: [6, 10], multiplier: 2 }] },
    'gpt-5.6-luna': { provider: 'openai-codex', billing_mode: 'subscription', price_windows: [{ hours_utc: [6, 10], multiplier: 2 }] },
  };
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /formato que o roteador não lê/, 'the §5.4 sentence stays');
  assert.doesNotMatch(text, /sem teto de preço/, 'a refused form is not an absent cap');
  assert.equal(findAll(dom.get('ladder'), 'hop-cap').length, 0,
    'a ceiling the router cannot enforce marks nothing');
});

test('the group line says the cap turned itself off only when the plan says it did', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  const said = () => flat(dom.get('chainPlan'));
  api.renderChainPlan(chainPlan({ time_cap_bypassed: true, time_cap: { max_multiplier: 1.5 } }));
  assert.match(said(), /o teto se desligou nesta decisão: aplicá-lo deixaria a fila vazia/);
  api.renderChainPlan(chainPlan({ time_cap_bypassed: false, time_cap: { max_multiplier: 1.5 } }));
  assert.doesNotMatch(said(), /o teto se desligou nesta decisão/,
    'a false bypass never earns the sentence (DESIGN.md §2 rule 2)');
});

test('a multiplier exactly AT the ceiling is not above it', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  // The plan's own multipliers are the numbers the ordering decision ran on,
  // so the mark reads them — and 1.5× against a 1.5× ceiling is eligible.
  api.renderChainPlan(chainPlan({
    time_cap: { max_multiplier: 1.5 },
    utc_hour: 14, utc_weekday: 0,
    multipliers: { 'm-at': 1.5, 'm-over': 2.0 },
    chain: [{ model: 'm-at', provider: 'zai' }, { model: 'm-over', provider: 'deepseek' }],
  }));
  const caps = findAll(dom.get('chainPlan'), 'hop-cap');
  assert.equal(caps.length, 1, '2.0× is above the ceiling, 1.5× is not');
  assert.match(caps[0].textContent, /acima do teto agora/);
});

test('the mark says when the attempt comes back under the ceiling — and only then', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  // At 06:00 both attempts bill 2.0×, over the 1.5× ceiling. m-drop's window
  // ends at 10:00, back at base; m-hike's window hands over to ANOTHER peak at
  // 10:00 (3.0×), so it never comes back under: claiming "entra no teto" there
  // would state a future that never happens.
  api.renderChainPlan(chainPlan({
    time_cap: { max_multiplier: 1.5 },
    utc_hour: 6, utc_weekday: 0,
    chain: [
      { model: 'm-drop', provider: 'zai', price_windows: [{ hours_utc: [6, 10], multiplier: 2 }] },
      { model: 'm-hike', provider: 'deepseek', price_windows: [{ hours_utc: [6, 10], multiplier: 2 }, { hours_utc: [10, 14], multiplier: 3 }] },
    ],
  }));
  const caps = findAll(dom.get('chainPlan'), 'hop-cap').map((n) => n.textContent);
  assert.deepEqual(caps,
    ['acima do teto agora · entra no teto às 10:00 UTC', 'acima do teto agora'],
    "the next turn ends m-drop's peak but keeps m-hike over the ceiling");
});

test('the ladder marks the attempts above the group ceiling at the console hour', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.clock = new Date(Date.UTC(2026, 7, 17, 14, 0));
  api.state.policy = {
    rules: [], default: {},
    tiers: {
      T1: {
        model: 'glm-4.7', provider: 'zai', billing_mode: 'plan',
        fallback: [{ model: 'gpt-5.6-luna', provider: 'openai-codex', billing_mode: 'subscription' }],
        time_cap: { max_multiplier: 1.5 },
      },
    },
  };
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', billing_mode: 'plan', price_windows: [{ hours_utc: [12, 16], multiplier: 2 }] },
    'gpt-5.6-luna': { provider: 'openai-codex', billing_mode: 'subscription', price_windows: [{ hours_utc: [6, 10], multiplier: 2 }] },
  };
  api.renderLadder();
  const marks = findAll(dom.get('ladder'), 'hop-cap');
  assert.equal(marks.length, 1,
    'at 14:00 the primary bills 2.0× (over 1.5×) and the fallback is at base');
  assert.match(marks[0].textContent, /acima do teto agora/);
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
  // The ceiling's value joins the sentence with the pt-BR preposition; the
  // English 'of' this replaces rode inside a nested template, invisible to
  // the static extractors, so it is pinned here where it renders.
  assert.match(text, /do teto de preço do grupo de 1\.5×/);
  assert.doesNotMatch(text, / of /, 'no English preposition survives in the sentence');

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
  // Recursive since §2.5: the tier editor nests every attempt's fields one
  // level down in .chain-row, so a label is no longer always a direct child
  // of the inspector box. First match in document order is the primary
  // row's field; reserve rows come later.
  const scan = (n) => {
    const kids = n.children || [];
    for (let i = 0; i < kids.length; i += 1) {
      const c = kids[i];
      if (String(c.className || '').includes('field')
          && (c.children[0] || {}).textContent === label) return c;
      const hit = scan(c);
      if (hit) return hit;
    }
    return null;
  };
  return scan(node);
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
  // §3.4(c): the fallback field is labelled for what it is — an id being
  // typed, not a choice being made.
  const modelWrap = byLabel(box, 'Id do modelo');
  assert.ok(modelWrap, 'the model field exists');
  const input = modelWrap.children.find((c) => c.tagName === 'input');
  assert.ok(input, 'no catalogue: the model field is an input (text fallback)');
  assert.match(flat(modelWrap), /Sem catálogo, não há lista para escolher\./,
    'the §3.4(c) note is present');
  // Billing selects exist without a catalogue (a mode is policy, not
  // registry), so the CA9 count is scoped to the MODEL field: no model
  // select is born from a missing catalogue.
  assert.equal(modelWrap.children.filter((c) => c.tagName === 'select').length, 0,
    'no model select is born from a missing catalogue');
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

// ── CA8: a destination naming a group the table does not have ──────────────
// §3.4(a). Three things at once, and the third is the one a screen usually gets
// wrong: the warning in the operator's words, the row marked, and NO Salvar in the
// DOM — absent, not disabled, because pressing your way out of an error state is
// exactly the wrong lesson.
// The DOM stub has no markup, so a test that measures what is IN the actions box has
// to seed it the way console.html does. `test_webui_extension.py::test_json_actions_markup`
// asserts the file really carries these three ids inside #jsonActions — unlabelled,
// because §4.7 keeps the labels in the WRITE map and boot stamps them — so this mirror
// cannot drift from the markup (or the map) it stands for.
function seedJsonActions(dom) {
  const box = dom.get('jsonActions');
  box.children = [];
  [['jsonApply', 'Salvar'], ['jsonPreview', 'Ver o que muda'], ['jsonRevert', 'Voltar à versão anterior']]
    .forEach(([id, label]) => {
      const node = dom.get(id);
      node.textContent = label;
      box.append(node);
    });
  return box;
}

function missingGroupState(api) {
  api.state.loading = false;
  api.state.policy = {
    rules: [
      { id: 'audit', when: { keywords: { contains: 'audit' } }, then: { model: 'T9', profile: 'reviewer' } },
      { id: 'code', when: { has_code: { eq: true } }, then: { model: 'T2' } },
    ],
    default: { action: 'classify' },
    classifier: { model: 'glm-4.7' },
    fail_safe: { model: 'glm-4.7', provider: 'zai' },
    tiers: { T2: { model: 'glm-5.3', provider: 'zai' } },
  };
  api.state.status = {
    validation_errors: ["rule 'audit': 'then.model' references unknown tier 'T9'"],
    error_targets: [null],
    enabled: true,
  };
}

test('a missing group is named in the operator words, with the jump and the consequence', () => {
  const { api, dom } = loadConsole();
  missingGroupState(api);
  api.renderWarnings();
  api.renderSheet();

  const said = flat(dom.get('warnings'));
  assert.match(said, /Não é possível salvar enquanto houver erro\. 1 erro\(s\) no arquivo\./);
  // WHICH rule and WHICH group, by the rule's own title rather than its id: the id is
  // what the raw lint message already gave, and it is not what the row shows.
  assert.match(said, /A regra “Pedido de auditoria” manda para o Grupo T9, que não existe na sua tabela de grupos\./);
  // The consequence is the part no raw message carries: "unknown tier" reads as
  // ignored, and it is not.
  assert.match(said, /o roteador tenta chamar um modelo chamado “T9”, a chamada falha, e a tarefa cai no último recurso/);
  // And the server's English is NOT forwarded when the console can say it in pt-BR.
  assert.doesNotMatch(said, /unknown tier/);
  // One jump, once — two buttons for one jump is the duplication DESIGN.md forbids.
  const jumps = (dom.get('warnings').children || []).flatMap((line) => (line.children || [])
    .filter((k) => /Ir para a regra/.test(String(k.textContent || ''))));
  assert.equal(jumps.length, 1, 'exactly one jump button');
  assert.equal(jumps[0].textContent, 'Ir para a regra 1', 'the ordinal the operator reads on the sheet');

  // The row carries the fifth destination prefix of §4.3.
  assert.match(flat(dom.get('sheet')), /⚠ Grupo T9 — não existe/);
});

test('while the file carries an error there is NO Salvar in the DOM, and it comes back in its place', () => {
  const { api, dom } = loadConsole();
  seedJsonActions(dom);
  missingGroupState(api);
  api.setMode('editing');
  api.renderWarnings();

  const labels = () => (dom.get('jsonActions').children || []).map((k) => String(k.textContent || ''));
  assert.equal(labels().indexOf('Salvar'), -1,
    `Salvar must be ABSENT while the file has an error, got ${JSON.stringify(labels())}`);
  // The other two stay: seeing what would change writes nothing, and Voltar à versão
  // anterior restores a backup rather than saving a draft.
  assert.ok(labels().indexOf('Ver o que muda') >= 0);
  assert.ok(labels().some((l) => /Voltar à versão anterior/.test(l)));

  // Errors cleared: the button is back, BEFORE "Ver o que muda"… no — after it, in the
  // order the flow reads: see what changes, then save.
  api.state.status = { validation_errors: [], error_targets: [], enabled: true };
  api.renderWarnings();
  const back = labels();
  assert.ok(back.indexOf('Salvar') >= 0, 'a fixed file gets its Salvar back');
  assert.ok(back.indexOf('Salvar') < back.indexOf('Ver o que muda'),
    `Salvar returns to its own place, got ${JSON.stringify(back)}`);
  // And it is the SAME node it always was — detached and re-attached, never rebuilt.
  // A rebuilt button is a button whose click handler (wired once at boot) can quietly
  // go missing, and the failure would be a Salvar that looks alive and writes nothing.
  const box = dom.get('jsonActions');
  assert.equal(box.children[box.children.map((k) => k.textContent).indexOf('Salvar')],
    dom.get('jsonApply'), 'the button that came back is the button that left');
});

test('renderWarnings is the ONE place that decides whether a Salvar exists', () => {
  // The gate rides the render every path already calls, so a new caller cannot forget
  // it. Toggling only the error set — no mode change — has to move the button.
  const { api, dom } = loadConsole();
  seedJsonActions(dom);
  missingGroupState(api);
  api.state.status = { validation_errors: [], error_targets: [], enabled: true };
  api.renderWarnings();
  const labels = () => (dom.get('jsonActions').children || []).map((k) => String(k.textContent || ''));
  assert.ok(labels().indexOf('Salvar') >= 0, 'clean file, button present');
  missingGroupState(api);
  api.renderWarnings();
  assert.equal(labels().indexOf('Salvar'), -1, 'error appears, button leaves');
});

test('a concrete model id at a destination is NOT a missing group — it is the fixed-model case', () => {
  // §4.3 has five destination prefixes and these are two different ones: a `Tn` the
  // table does not have is an error the lint refuses, while a model id is legal and
  // runs (with no reserve, which is its own warning). Reading the second as the first
  // would tell an operator to fix a file the router accepts.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [{ id: 'fixed', when: { has_code: { eq: true } }, then: { model: 'glm-5.3', provider: 'zai' } }],
    default: { action: 'classify' }, classifier: { model: 'm' }, fail_safe: { model: 'm' },
    tiers: { T2: { model: 'glm-5.3', provider: 'zai' } },
  };
  // plain(): the array comes from the VM realm, and strict deepEqual compares prototypes.
  assert.deepEqual(plain(api.missingGroupFindings()), [], 'a model id names no group');

  // And with an unrelated error on the file, the banner keeps the server's own message
  // instead of inventing a missing group for it.
  api.state.status = {
    validation_errors: ["tier 'T2': 'model' must be a non-empty string"],
    error_targets: [null], enabled: true,
  };
  api.renderWarnings();
  const said = flat(dom.get('warnings'));
  assert.doesNotMatch(said, /não existe na sua tabela de grupos/);
  assert.match(said, /Primeiro erro: tier 'T2'/, 'the raw path is what an untranslated error gets');
});

test('a default pointing at a missing group gets ITS OWN phrase and a jump to the default panel', () => {
  // §3.4(a) applied to the default (the review card t_1064aa8c left this case in
  // the server's raw text): the lint says `default: 'model' references unknown
  // tier 'T9'`, and the console translates it — written FOR the default, never by
  // reusing the rule sentence with an empty title.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [{ id: 'code', when: { has_code: { eq: true } }, then: { model: 'T2' } }],
    default: { model: 'T9' },
    classifier: { model: 'glm-4.7' },
    fail_safe: { model: 'glm-4.7', provider: 'zai' },
    tiers: { T2: { model: 'glm-5.3', provider: 'zai' } },
  };
  api.state.status = {
    validation_errors: ["default: 'model' references unknown tier 'T9'"],
    error_targets: [null],
    enabled: true,
  };
  api.renderWarnings();

  const said = flat(dom.get('warnings'));
  assert.match(said, /O destino padrão manda para o Grupo T9, que não existe na sua tabela de grupos\./);
  assert.match(said, /o roteador tenta chamar um modelo chamado “T9”, a chamada falha, e a tarefa cai no último recurso/);
  // The default's phrase must not be the rule's sentence with an empty title —
  // the mutation the review card named.
  assert.doesNotMatch(said, /A regra “” manda/);
  assert.doesNotMatch(said, /unknown tier/, 'the server English is not forwarded');
  // NO [ Ir para a regra ] anywhere: there is no rule row to jump to, and a
  // button claiming one would lie about the file.
  const toRule = (dom.get('warnings').children || []).flatMap((line) => (line.children || [])
    .filter((k) => /Ir para a regra/.test(String(k.textContent || ''))));
  assert.equal(toRule.length, 0, 'no rule jump exists for a default finding');
  const toDefault = (dom.get('warnings').children || []).flatMap((line) => (line.children || [])
    .filter((k) => String(k.textContent || '') === 'Ir para o destino padrão'));
  assert.equal(toDefault.length, 1, 'the jump to the default panel is there');

  // The jump goes to the DEFAULT — not to rule 0 (the mutation: a default jump
  // that lands on a rule). The row is the synthetic "__default" one, marked and
  // scrolled, and the inspector opens on the default.
  toDefault[0]._listeners.click();
  assert.equal(api.state.selected, 'default', 'the inspector opened on the default, not on a rule');
  const row = dom.get('sheet').children.find((c) => c.dataset.ruleId === '__default');
  assert.ok(row, 'the synthetic default row is on the sheet');
  assert.deepEqual(plain(row._scrolledTo), { block: 'center' }, 'and it was scrolled into view');
  assert.match(flat(dom.get('inspector')), /default/, 'the inspector names the default');

  // The sheet row itself carries the §4.3 fifth prefix, like a rule's would.
  api.renderSheet();
  assert.match(flat(dom.get('sheet')), /⚠ Grupo T9 — não existe/);
});

test('a rule AND the default pointing at missing groups get two phrases, each with its own jump', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [{ id: 'audit', when: { keywords: { contains: 'audit' } }, then: { model: 'T9' } }],
    default: { model: 'T8' },
    classifier: { model: 'glm-4.7' },
    fail_safe: { model: 'glm-4.7', provider: 'zai' },
    tiers: { T2: { model: 'glm-5.3', provider: 'zai' } },
  };
  api.state.status = {
    validation_errors: [
      "rule 'audit': 'then.model' references unknown tier 'T9'",
      "default: 'model' references unknown tier 'T8'",
    ],
    error_targets: [null, null],
    enabled: true,
  };
  api.renderWarnings();

  const said = flat(dom.get('warnings'));
  // Two DISTINCT phrases — the rule's names the rule, the default's names the
  // destino padrão; neither is the other with an empty title.
  assert.match(said, /A regra “Pedido de auditoria” manda para o Grupo T9/);
  assert.match(said, /O destino padrão manda para o Grupo T8/);
  assert.doesNotMatch(said, /A regra “” manda/);
  // Each finding is matched to ITS OWN error: the rule phrase names T9 under the
  // rule error, the default phrase names T8 under the default error.
  const toRule = (dom.get('warnings').children || []).flatMap((line) => (line.children || [])
    .filter((k) => /Ir para a regra/.test(String(k.textContent || ''))));
  const toDefault = (dom.get('warnings').children || []).flatMap((line) => (line.children || [])
    .filter((k) => String(k.textContent || '') === 'Ir para o destino padrão'));
  assert.equal(toRule.length, 1, 'one rule jump');
  assert.equal(toDefault.length, 1, 'one default jump');

  toRule[0]._listeners.click();
  assert.equal(api.state.selected, 'rule:audit', 'the rule jump lands on the rule');
  toDefault[0]._listeners.click();
  assert.equal(api.state.selected, 'default', 'the default jump lands on the default');
});

// ── CA5 + CA6: the write path of the presets, and the one write that asks ───
// CA6: the body of the POST /plan a preset fires has EXACTLY ONE top-level key in
// `policy`, and it is `tiers`. CA5: no POST /apply without a POST /plan immediately
// before it in the same interaction, except /apply/revert, which only goes out after
// a confirmation.
function presetPolicy() {
  return {
    rules: [{ id: 'code', when: { has_code: { eq: true } }, then: { model: 'T2' } }],
    default: { action: 'classify' },
    classifier: { model: 'glm-4.7', provider: 'zai' },
    fail_safe: { model: 'glm-4.7', provider: 'zai' },
    tiers: {
      T1: { model: 'glm-4.7', provider: 'zai', billing_mode: 'plan', fallback: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }] },
      T2: { model: 'glm-5.3', provider: 'zai', billing_mode: 'metered' },
      T3: { model: 'deepseek-v4-pro', provider: 'deepseek', billing_mode: 'metered' },
      T4: { model: 'gpt-5.6-luna', provider: 'openai-codex', billing_mode: 'subscription' },
      // A group of the operator's own: legal (rules.py accepts a key outside T1..T4)
      // and the factory preset must not touch it.
      Titan: { model: 'titan-70b', provider: 'local', billing_mode: 'free' },
    },
  };
}

test('every preset plans a body with exactly ONE top-level key, and it is tiers', () => {
  const { api } = loadConsole();
  const policy = presetPolicy();
  api.state.policy = policy;
  plain(api.PRESETS).forEach((preset) => {
    const patch = plain(api.presetPatch(preset.key, policy));
    assert.deepEqual(Object.keys(patch), ['tiers'],
      `${preset.key} must write groups and nothing else: the order of rules is the semantics, and a preset that rewrote rules could create or delete dead rules invisibly`);
  });
});

test('Economizar pins the first option only where the group is paid from an allowance', () => {
  // A plan or a subscription is drawn off something already bought, so reordering it
  // by dollar price buys nothing and loses the option the operator put first.
  const { api } = loadConsole();
  const patch = plain(api.presetPatch('economizar', presetPolicy())).tiers;
  assert.equal(patch.T1.pin_primary, true, 'plan');
  assert.equal(patch.T4.pin_primary, true, 'subscription');
  assert.equal(patch.T2.pin_primary, false, 'metered');
  Object.keys(patch).forEach((name) => {
    assert.equal(patch[name].fallback_strategy, 'cheapest_now');
    assert.deepEqual(patch[name].time_cap, { max_multiplier: 1.5 });
    // It never touches what a model IS or which models are in the queue.
    assert.deepEqual(Object.keys(patch[name]).sort(), ['fallback_strategy', 'pin_primary', 'time_cap']);
  });
  // Every group in the file, including the operator's own.
  assert.ok(Object.keys(patch).indexOf('Titan') >= 0);
});

test('Equilíbrio restores the four factory groups and leaves an extra one alone', () => {
  const { api } = loadConsole();
  const patch = plain(api.presetPatch('equilibrio', presetPolicy())).tiers;
  assert.deepEqual(Object.keys(patch).sort(), ['T1', 'T2', 'T3', 'T4'],
    'a group the arquivo de exemplo never described is not something a preset may reset');
  assert.equal(patch.T2.fallback_strategy, 'cheapest_now');
  assert.equal(patch.T2.pin_primary, true);
  assert.deepEqual(patch.T1.time_cap, { max_multiplier: 1.5 });
  assert.deepEqual(patch.T3.time_policy, { avoid_peak: ['deepseek', 'zai'] });
  assert.deepEqual(patch.T3.requirements, { min_context: 200000 });
  // A key the factory leaves undeclared is sent as null — the server's own way of
  // removing a key (service.py: a null removes, {} removes nothing).
  assert.equal(patch.T4.fallback_strategy, null);
  assert.equal(patch.T1.pin_primary, null);
});

test('Priorizar qualidade only ever loosens: order kept, no ceiling', () => {
  const { api } = loadConsole();
  const patch = plain(api.presetPatch('qualidade', presetPolicy())).tiers;
  Object.keys(patch).forEach((name) => {
    assert.deepEqual(patch[name], { fallback_strategy: 'sequential', pin_primary: null, time_cap: null });
  });
});

test('the preset in force is READ off the groups, with no key remembering it', () => {
  // §5.1 adds nothing to router.yaml, so "which preset is active" is content, not a
  // marker. A→B→C is the order that resolves a tie, and no match is a real answer.
  const { api } = loadConsole();
  const policy = presetPolicy();
  assert.equal(api.activePreset(policy), null, 'a hand-written file matches none of the three');

  const applied = (key) => {
    const next = JSON.parse(JSON.stringify(policy));
    const patch = plain(api.presetPatch(key, policy)).tiers;
    Object.keys(patch).forEach((name) => Object.assign(next.tiers[name], patch[name]));
    return next;
  };
  assert.equal(api.activePreset(applied('economizar')), 'economizar');
  assert.equal(api.activePreset(applied('qualidade')), 'qualidade');
  // Equilíbrio is detected on the four factory groups; the extra group is untouched
  // and must not stop the detection.
  assert.equal(api.activePreset(applied('equilibrio')), 'equilibrio');
  assert.equal(api.activePreset({ tiers: {} }), null, 'no groups, no preset');
});

test('a knob the policy omits and the preset\'s explicit null are the same absence', () => {
  // §7/P3: the comparator was duplicated in activePreset and tierPresetOf, and
  // two copies of a fact drift. Its one semantic: a group that never declared
  // a knob (undefined) and a patch that spells removal (null) read as the same
  // knob — or a hand-written file matches no preset the moment one group omits
  // a key the preset removes. Both readers must agree on it.
  const { api } = loadConsole();
  const policy = presetPolicy();
  const next = JSON.parse(JSON.stringify(policy));
  const patch = plain(api.presetPatch('qualidade', policy)).tiers;
  Object.keys(patch).forEach((name) => Object.assign(next.tiers[name], patch[name]));
  // Delete the keys Qualidade spells as null: the group now OMITS them
  // (undefined) instead of carrying the patch's explicit null.
  delete next.tiers.T1.pin_primary;
  delete next.tiers.T1.time_cap;
  assert.equal(api.activePreset(next), 'qualidade',
    'the whole-table reader treats undefined and null as the same absence');
  assert.equal(api.tierPresetOf(next.tiers.T1, 'T1', next).key, 'qualidade',
    '...and the per-group reader agrees');
});

test('a preset writes NOTHING until a plan for that same choice is on screen (CA5)', async () => {
  const calls = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      // The console posts to the sidecar path; the ROUTE is its tail, and that is what
      // the criterion is about (which endpoint, in which order).
      calls.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: opts && opts.body ? JSON.parse(opts.body) : null });
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(presetPolicy())) });
      }
      const planned = { valid: true, diff: '-a\n+b', policy: { tiers: { T2: { fallback_strategy: 'cheapest_now' } } } };
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(planned)) });
    },
  });
  api.state.loading = false;
  api.state.policy = presetPolicy();
  api.state.preset = 'economizar';
  api.renderPresets();

  const labels = () => (dom.get('presetActions').children || []).map((k) => String(k.textContent || ''));
  assert.ok(labels().indexOf('Ver o que muda') >= 0, 'the preview is always offered');
  assert.equal(labels().indexOf('Salvar'), -1, 'and nothing can be written before it');

  // Clicking Salvar before a plan is refused by the function itself, not only hidden.
  await api.applyPreset();
  assert.deepEqual(calls, [], 'no request at all without a plan');
  assert.match(dom.get('presetMsg').textContent, /Veja o que muda antes de salvar/);

  await api.previewPreset();
  // §5.5 first: the preview revalidates, then the staleness guard reads, then it plans.
  assert.deepEqual(calls.map((c) => c.url), ['/lint', '/policy', '/plan'], 'the preview revalidates and plans, and writes nothing');
  const planCall = calls.find((c) => c.url === '/plan');
  assert.deepEqual(Object.keys(planCall.body.policy), ['tiers'], 'CA6, on the wire');
  assert.match(dom.get('presetMsg').textContent, /Isto vai mudar 2 linhas do arquivo/);
  assert.ok(labels().indexOf('Salvar') >= 0, 'only now does a Salvar exist');

  await api.applyPreset();
  /*
   * CA5, read off the wire: every /apply has a /plan immediately before it. doApply
   * re-plans for the base_hash the server refuses to write without, so the sequence is
   * plan, plan, apply — and after a successful write the console reloads its reads,
   * which is why the assertion is about the ORDER of the write pair and not the tail
   * of the whole list.
   */
  const routes = calls.map((c) => c.url);
  const applyAt = routes.indexOf('/apply');
  assert.ok(applyAt > 0, `an /apply went out, got ${JSON.stringify(routes)}`);
  assert.equal(routes[applyAt - 1], '/plan', 'the plan immediately before it');
  assert.equal(routes.filter((r) => r === '/apply').length, 1, 'written once');
  // And what gets planned for the write is the SAME body the operator previewed. A
  // patch rebuilt at click time would read the policy again — so a file that changed
  // under the screen (a CLI edit, another tab) would be written from a patch nobody
  // saw, with the diff on screen describing the previous one.
  // What is planned for the write is the policy the PREVIEW's plan handed back — not a
   // patch rebuilt at click time. A rebuilt patch reads the policy again, so a file that
   // changed under the screen (a CLI edit, another tab) would be written from a body
   // nobody saw, with the diff on screen describing the previous one.
  assert.deepEqual(calls[applyAt - 1].body.policy, { tiers: { T2: { fallback_strategy: 'cheapest_now' } } },
    'the write plans the planned policy, not a fresh patch');
  assert.deepEqual(Object.keys(planCall.body.policy), ['tiers'], 'and the preview planned the preset patch');
  // The write itself carries the planned policy the server handed back.
  assert.deepEqual(calls[applyAt].body.policy, { tiers: { T2: { fallback_strategy: 'cheapest_now' } } });
});

test('a preset already in force refuses to save instead of writing a no-op', async () => {
  // Applying a no-op destroys the only copy "Voltar à versão anterior" could restore
  // (the server snapshots to .bak before every write), so the screen refuses.
  const calls = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      calls.push(String(url).replace(/^.*\/sidecar/, ''));
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(presetPolicy())) });
      }
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: true, diff: '', policy: {} })),
      });
    },
  });
  api.state.loading = false;
  api.state.policy = presetPolicy();
  api.state.preset = 'qualidade';
  api.renderPresets();
  await api.previewPreset();

  assert.match(dom.get('presetMsg').textContent, /Este preset já está em vigor\. Nada a salvar\./);
  const labels = (dom.get('presetActions').children || []).map((k) => String(k.textContent || ''));
  assert.equal(labels.indexOf('Salvar'), -1, 'no Salvar for a no-op');
  await api.applyPreset();
  assert.deepEqual(calls, ['/lint', '/policy', '/plan'], 'and nothing was written');
});

test('choosing another preset drops the plan the diff on screen belonged to', async () => {
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(presetPolicy())) });
      }
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: true, diff: '-a\n+b', policy: { tiers: {} } })),
      });
    },
  });
  api.state.loading = false;
  api.state.policy = presetPolicy();
  api.state.preset = 'economizar';
  api.renderPresets();
  await api.previewPreset();
  const labels = () => (dom.get('presetActions').children || []).map((k) => String(k.textContent || ''));
  assert.ok(labels().indexOf('Salvar') >= 0);

  // The other preset's button: the diff below it is not this preset's, so a Salvar
  // left over from the first would write a body the operator is no longer reading.
  const other = (dom.get('presetOptions').children || [])
    .flatMap((row) => row.children || [])
    .find((k) => String(k.dataset && k.dataset.preset) === 'qualidade');
  assert.ok(other, 'each preset has its own control');
  other._listeners.click();
  assert.equal(labels().indexOf('Salvar'), -1, 'the plan went with the choice');
  assert.equal(dom.get('presetDiff').hidden, true);
  // And the plan is really GONE, not merely unreachable from this box: doApply falls
  // back to `state.plan.policy` when it is handed no draft, so a plan left behind here
  // is a body the JSON editor's own Salvar would write without anybody previewing it.
  assert.equal(api.state.plan, null, 'a dropped choice leaves no plan for another surface to write');
});

test('Voltar à versão anterior asks first, and says what is missing: the preview', async () => {
  // CA5's exception. It is the one write with nothing to diff against — the server
  // restores whatever the .bak holds — so the question names that rather than being a
  // generic "are you sure".
  const calls = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      calls.push(String(url).replace(/^.*\/sidecar/, ''));
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.loading = false;

  await api.requestRevert();
  assert.deepEqual(calls, [], 'the first click writes nothing');
  assert.match(dom.get('jsonMsg').textContent,
    /Isto substitui o arquivo atual pela cópia que o roteador guardou antes da última gravação\. Diferente de Salvar, aqui não há prévia: você não vê o que vai mudar antes\. Continuar\?/);
  assert.equal(dom.get('jsonRevert').textContent, 'Confirmar: voltar à versão anterior',
    'the button says what the next click does');

  await api.requestRevert();
  // A successful write reloads the eight reads, so the claim is about the WRITES.
  const writes = () => calls.filter((c) => /^\/apply/.test(c));
  assert.deepEqual(writes(), ['/apply/revert'], 'the second click is the one that acts');
  assert.equal(dom.get('jsonRevert').textContent, 'Voltar à versão anterior', 'and it disarms');

  // Leaving edit mode disarms too: an armed destructive button that outlives the
  // question on screen is a button the next click executes silently.
  await api.requestRevert();
  api.setMode('reading');
  assert.equal(dom.get('jsonRevert').textContent, 'Voltar à versão anterior');
  await api.requestRevert();
  assert.deepEqual(writes(), ['/apply/revert'], 'still one write: the arming was dropped');
});

test('the preset box names the metric and the consequence of each option, and what is in force', () => {
  // §2.4: a preset named without its metric asks the operator to trust a number nobody
  // showed them, and one without its consequence hides that a cost control can take an
  // option out of the queue.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = presetPolicy();
  api.renderPresets();

  const said = flat(dom.get('presetOptions'));
  assert.match(said, /Economizar/);
  assert.match(said, /Escolhe pelo menor preço de saída por 1M de tokens na hora atual\./);
  assert.match(said, /Se isso deixasse o grupo sem nenhuma opção, o teto se desliga sozinho e a fila original volta\./,
    'a cost control that could cause an outage is the one thing this must not be');
  assert.match(said, /Equilíbrio \(o que veio de fábrica\)/);
  assert.match(said, /Sem métrica: são os valores que vieram no arquivo de exemplo\./);
  assert.match(said, /Priorizar qualidade/);
  assert.match(said, /nunca tira uma opção da fila por causa de preço\./);

  // A hand-written file matches none of the three, and that is said as an answer.
  assert.equal(dom.get('presetActive').textContent, 'Em vigor agora: Personalizado');
  assert.match(dom.get('presetNote').textContent, /Escolher um substitui as suas em todos os grupos/);

  // With a preset in force, the note is the other one: no preset ever adds a model.
  const next = JSON.parse(JSON.stringify(api.state.policy));
  const patch = plain(api.presetPatch('qualidade', next)).tiers;
  Object.keys(patch).forEach((name) => Object.assign(next.tiers[name], patch[name]));
  api.state.policy = next;
  api.renderPresets();
  assert.equal(dom.get('presetActive').textContent, 'Em vigor agora: Priorizar qualidade');
  assert.match(dom.get('presetNote').textContent, /Nenhum preset adiciona um modelo que você não tem/);
  assert.match(dom.get('presetNote').textContent, /Nenhum preset mexe em qual grupo cada tarefa usa/);
});

test('doApply plans immediately before every write, whichever surface called it (CA5)', async () => {
  // The guarantee is structural rather than per-button: all three write surfaces (the
  // presets, the per-rule inspector, the JSON editor) go through this one function, so
  // a new surface cannot forget the plan. The plan is not a courtesy either — it
  // returns the base_hash the server refuses to write without.
  const routes = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      routes.push(String(url).replace(/^.*\/sidecar/, ''));
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: true, diff: '-a\n+b', policy: { tiers: {} } })),
      });
    },
  });
  api.state.loading = false;
  api.state.policy = {};

  await api.doApply('/apply', dom.get('jsonMsg'), { tiers: { T2: { pin_primary: true } } }, dom.get('jsonDiff'));
  assert.equal(routes[1], '/plan', 'the staleness read comes first, then the plan');
  assert.equal(routes[2], '/apply', 'and the write immediately after it');

  // A plan that says the draft is invalid stops there: no write at all.
  const refused = [];
  const second = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      refused.push(String(url).replace(/^.*\/sidecar/, ''));
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: false, errors: ['nope'], diff: '-a' })),
      });
    },
  });
  second.api.state.loading = false;
  second.api.state.policy = {};
  await second.api.doApply('/apply', second.dom.get('jsonMsg'), { tiers: {} }, second.dom.get('jsonDiff'));
  assert.deepEqual(refused, ['/policy', '/plan'], 'an invalid draft never reaches /apply');
});

// ── CA2: the list is complete, and no row has an empty destination ──────────
// `#sheet` has `rules.length + 3` items when there is a manual ban and
// `rules.length + 2` when there is not — the two synthetic rows are "when no rule
// matches" and "when everything fails", and the ban row exists only when it has
// something in it (render nothing for nothing). And every row's destination begins
// with one of the five prefixes of §4.3: a row whose destination is blank is a row
// that tells the operator nothing about where the work goes.
const DEST_PREFIXES = [
  /^→\s*Grupo \S+ · /,
  /^→\s*Perguntar ao classificador/,
  /^→\s*Recusar a tarefa/,
  /^→\s*.+ @ .+ — modelo fixo, sem reserva/,
  /^→\s*⚠ Grupo \S+ — não existe/,
];

function sheetPolicy(extra) {
  return Object.assign({
    rules: [
      { id: 'audit', when: { keywords: { contains: 'audit' } }, then: { model: 'T4', profile: 'reviewer' } },
      { id: 'ask', when: { keywords: { contains: 'review' } }, then: { action: 'classify' } },
      { id: 'no', when: { has_code: { eq: false } }, then: { deny: true } },
      { id: 'fixed', when: { needs_vision: { eq: true } }, then: { model: 'glm-5.3', provider: 'zai' } },
      { id: 'gone', when: { num_files: { gte: 3 } }, then: { model: 'T9' } },
    ],
    default: { action: 'classify' },
    classifier: { model: 'glm-4.7', provider: 'zai' },
    fail_safe: { model: 'glm-4.7', provider: 'zai' },
    tiers: { T2: { model: 'glm-5.3', provider: 'zai' }, T4: { model: 'gpt-5.6-luna', provider: 'openai-codex' } },
  }, extra || {});
}

test('the sheet counts rules + the two synthetic rows, plus the ban row only when there is a ban', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const rows = () => (dom.get('sheet').children || []).length;
  const rules = api.state.policy.rules.length;
  assert.equal(rows(), rules + 2, 'no manual ban, so no ban row: render nothing for nothing');

  api.state.policy = sheetPolicy({ blocklist: { manual_ban: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }] } });
  api.renderSheet();
  assert.equal(rows(), rules + 3, 'a ban is the first thing that decides, so it is the first row');
});

test('every row on the sheet has a destination, and it is one of the five (CA2)', () => {
  // The fixture exercises all five on purpose: a group, the classifier, a refusal, a
  // fixed model id, and a group that does not exist.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy({ blocklist: { manual_ban: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }] } });
  api.renderSheet();

  const dests = findAll(dom.get('sheet'), 'step-dest').map((node) => flat(node).replace(/\s+/g, ' ').trim());
  assert.ok(dests.length >= 6, `every row draws a destination, got ${dests.length}`);
  dests.forEach((text) => {
    assert.notEqual(text, '', 'a row with no destination says nothing about where the work goes');
    assert.ok(DEST_PREFIXES.some((prefix) => prefix.test(text)),
      `"${text}" is not one of the five destinations of §4.3`);
  });
  // And all five really appear, so the test is not passing on one prefix five times.
  const hit = DEST_PREFIXES.filter((prefix) => dests.some((text) => prefix.test(text)));
  assert.equal(hit.length, 5, `all five prefixes exercised, got ${hit.length}`);
});

test('the preset control says its state to assistive technology, not only in ASCII', () => {
  // It is drawn as a radio — "( )" and "(•)" — because DESIGN.md allows no svg and no
  // innerHTML. Two characters of text are not state: aria-pressed is.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  const next = JSON.parse(JSON.stringify(presetPolicy()));
  const patch = plain(api.presetPatch('qualidade', next)).tiers;
  Object.keys(patch).forEach((name) => Object.assign(next.tiers[name], patch[name]));
  api.state.policy = next;
  api.renderPresets();

  const buttons = (dom.get('presetOptions').children || []).flatMap((row) => (row.children || [])
    .filter((k) => k.dataset && k.dataset.preset));
  assert.equal(buttons.length, 3, 'one control per preset');
  const pressed = buttons.filter((b) => b.getAttribute('aria-pressed') === 'true');
  assert.deepEqual(pressed.map((b) => b.dataset.preset), ['qualidade'],
    'exactly the one in force is pressed');
});

test('an armed Voltar à versão anterior does not survive a refresh', async () => {
  // The question is about the file the screen was showing; after a re-read that is a
  // different file, and a button still armed from before would write against it.
  const routes = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      routes.push(String(url).replace(/^.*\/sidecar/, ''));
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.loading = false;
  await api.requestRevert();
  assert.equal(dom.get('jsonRevert').textContent, 'Confirmar: voltar à versão anterior');

  await api.load();
  assert.equal(dom.get('jsonRevert').textContent, 'Voltar à versão anterior', 'the refresh disarmed it');
  await api.requestRevert();
  assert.deepEqual(routes.filter((r) => /^\/apply/.test(r)), [],
    'the click after a refresh asks again instead of writing');
});

test('the refresh button says Recarregando… in flight and Recarregar after (§4.1)', async () => {
  // load() overwrites the button on every read, so the markup could not own
  // the label — this is exactly how 'Refreshing…'/'Refresh' escaped §4.7.
  // Both words now ride the WRITE map; this pins the in-flight swap.
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { api, dom } = loadConsole({
    fetch: (url) => {
      if (String(url).endsWith('/health')) {
        return gate.then(() => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') }));
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  const btn = dom.get('refresh');
  const run = api.load();
  await tick();
  assert.equal(btn.textContent, 'Recarregando…', 'the loader says what the read is doing, in pt-BR');
  release();
  await run;
  assert.equal(btn.textContent, 'Recarregar', 'and returns to the §4.1 label when the read is over');
});
// ── write path: the patch is minimal, and the staleness guard is the gate ──
// The audit t_873f43b9 blocked the redesign with a HIGH data-integrity defect:
// a write erased a concurrent CLI/other-tab edit in silence, because the screen
// sent its whole stale snapshot to /plan and the base_hash anchored to the
// plan's own read — never to the read the screen was showing. Two halves close
// it (§5.2 + §4.7): the inspector sends ONLY the touched fragment, and plan()
// refuses (with a reload) when GET /policy no longer matches state.policy.

test('the staleness comparison is JSON-deep: reordered keys are equal, different values are not', () => {
  const { api } = loadConsole();
  assert.equal(api.samePolicy({ a: 1, b: 2 }, { b: 2, a: 1 }), true, 'key order is serializer noise');
  assert.equal(api.samePolicy({ a: 1 }, { a: 2 }), false);
  assert.equal(api.samePolicy([1, 2], [2, 1]), false, 'array order is the semantics');
  assert.equal(api.samePolicy({ rules: [{ id: 'a' }] }, { rules: [{ id: 'a' }] }), true);
  assert.equal(api.samePolicy(null, {}), false);
  assert.equal(api.samePolicy({ tiers: { T2: { model: 'x' } } }, { tiers: { T2: { model: 'x', provider: 'zai' } } }), false);
});

test('a write is refused when the file changed since the screen read it', async () => {
  // The disk now carries a rule this screen has never seen (a CLI edit or
  // another tab since load()). The refusal is a reload, not a "are you sure":
  // the message is the §4.7 literal — grown to NAME the rule that appeared —
  // and neither /plan nor /apply ever fires.
  const applied = [];
  const planned = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (/\/apply$/.test(url)) applied.push(url);
      if (opts && opts.method === 'POST' && /\/plan$/.test(url)) planned.push(url);
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ rules: [{ id: 'concurrent' }] })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = { rules: [] };
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { rules: [{ id: 'mine' }] });

  assert.equal(msg.textContent,
    'O arquivo mudou por fora desde que esta tela leu: a regra 1 mudou. Recarreguei tudo; confira e tente de novo.',
    'the refusal names the object that drifted, in the §4.7 sentence');
  assert.match(msg.className, /bad/);
  assert.deepEqual(planned, [], 'a refused write must not reach /plan');
  assert.deepEqual(applied, [], 'a refused write must not reach /apply');
  assert.equal(api.state.plan, null, 'the stale plan must not survive to be applied');
  assert.equal(api.state.draft, null, 'the stale draft must not survive to be re-applied');
  // And the reload really happened: the snapshot is now the file as it is.
  assert.deepEqual(api.state.policy, { rules: [{ id: 'concurrent' }] });
});

test('a write proceeds when the file is exactly the snapshot the screen shows', async () => {
  // The guard's other polarity: refusing on "nothing changed" would turn every
  // save into a conflict. Same read, same file, write goes through.
  const applied = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (/\/apply$/.test(url)) applied.push(url);
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ rules: [] })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = { rules: [] };
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { rules: [{ id: 'mine' }] });

  assert.equal(applied.length, 1, 'the write happens when nothing changed underneath');
  assert.match(msg.textContent, /Vale para as próximas tarefas/, '§2.7: the write names its scope');
});

// ── the staleness guard names the object that drifted (§4.7) ────────────
// The refusal used to say "the file changed" and stop; the operator replanned
// in the dark. diffObjects names the four projections /policy carries — rules
// by POSITION (order IS the semantics), tiers by key, the two fallbacks as
// single objects — and the clause the refusal grows names them with the words
// this console already uses for them.

test('diffObjects names rules by position, tiers by key, and the two fallbacks', () => {
  const { api } = loadConsole();
  const base = {
    rules: [
      { id: 'a', status: 'stable', when: { size_lines: { gt: 10 } }, then: { model: 'glm-5.3', provider: 'zai' } },
      { id: 'b', status: 'stable', when: { size_lines: { gt: 400 } }, then: { model: 'glm-4.7', provider: 'zai' } },
    ],
    default: { model: 'glm-5.3', provider: 'zai' },
    tiers: {
      T1: { model: 'glm-4.7', provider: 'zai', fallback: ['glm-4.6'] },
      T2: { model: 'glm-5.2', provider: 'zai' },
    },
    fail_safe: { model: 'glm-4.6', provider: 'zai' },
  };
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const cases = [
    { name: 'só then.model de uma regra', mutate: (p) => { p.rules[1].then.model = 'glm-5.2'; }, expect: [{ kind: 'rule', index: 1 }] },
    { name: 'só a ordem de duas regras — posição é a semântica', mutate: (p) => { p.rules.reverse(); }, expect: [{ kind: 'rule', index: 0 }, { kind: 'rule', index: 1 }] },
    { name: 'grupo com fallback diferente', mutate: (p) => { p.tiers.T1.fallback = ['glm-4.5']; }, expect: [{ kind: 'tier', key: 'T1' }] },
    { name: 'fail_safe removido', mutate: (p) => { delete p.fail_safe; }, expect: [{ kind: 'fail_safe' }] },
    { name: 'default alterado', mutate: (p) => { p.default.model = 'glm-5.4'; }, expect: [{ kind: 'default' }] },
    { name: 'nada mudou', mutate: () => {}, expect: [] },
  ];
  cases.forEach((c) => {
    const lido = clone(base);
    const atual = clone(base);
    c.mutate(atual);
    assert.deepEqual(plain(api.diffObjects(lido, atual)), c.expect, c.name);
  });
});

test('inserting a rule in the middle names the inserted AND the shifted ones', () => {
  // Position is the semantics: a rule inserted at index 1 moves everything
  // after it, so the diff names the insertion and both displaced rules. The
  // phrase does not try to be smarter than the comparison is.
  const { api } = loadConsole();
  const lido = { rules: [{ id: 'a' }, { id: 'b' }, { id: 'c' }], tiers: {}, default: {}, fail_safe: {} };
  const atual = { rules: [{ id: 'a' }, { id: 'x' }, { id: 'b' }, { id: 'c' }], tiers: {}, default: {}, fail_safe: {} };
  assert.deepEqual(plain(api.diffObjects(lido, atual)),
    [{ kind: 'rule', index: 1 }, { kind: 'rule', index: 2 }, { kind: 'rule', index: 3 }]);
});

test('the staleness clause names 1, 2 and 4 objects, exact', () => {
  const { api } = loadConsole();
  assert.equal(api.staleClause([{ kind: 'rule', index: 0 }]), 'a regra 1 mudou');
  assert.equal(api.staleClause([{ kind: 'rule', index: 2 }, { kind: 'tier', key: 'T2' }]),
    'a regra 3 e o grupo T2 mudaram');
  assert.equal(api.staleClause([
    { kind: 'rule', index: 2 }, { kind: 'tier', key: 'T2' },
    { kind: 'default' }, { kind: 'fail_safe' },
  ]), 'a regra 3, o grupo T2, o destino padrão e o último recurso mudaram');
  assert.equal(api.staleClause([]), 'mudou em algo que esta tela não sabe nomear',
    'an empty diff is the "cannot name" clause, never a green light');
});

test('a refusal still happens when the drift is in something the screen cannot name', async () => {
  // Mutation 1: the guard must not relax to "refuse only when the diff names
  // something". Two snapshots that differ OUTSIDE the four /policy projections
  // (here a key the route does not project today — the point is the comparison
  // sees the file changed and diffObjects finds nothing to name) still refuse,
  // with the "cannot name" clause, and never reach /plan.
  const planned = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.method === 'POST' && /\/plan$/.test(url)) planned.push(url);
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ rules: [], future_projection: 1 })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = { rules: [] };
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { rules: [{ id: 'mine' }] });

  assert.equal(msg.textContent,
    'O arquivo mudou por fora desde que esta tela leu: mudou em algo que esta tela não sabe nomear. Recarreguei tudo; confira e tente de novo.');
  assert.match(msg.className, /bad/);
  assert.deepEqual(planned, [], 'a refused write never reaches /plan');
  assert.equal(api.state.plan, null);
});

test('the inspector Apply sends the touched fragment, never the whole policy (§5.2)', async () => {
  // Mutation 1 + 2, on the wire: the /plan body is the minimal patch — exactly
  // the keys the operator touched, and nothing else. Sending fullPolicy()
  // again would fail this on the body, not merely on the effect.
  const posted = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(tierPolicy())) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = tierPolicy();
  api.state.capabilities = { 'glm-5.3': { provider: 'zai', context_window: 200000 } };
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });

  // The operator changes ONLY the provider; the model select is never touched.
  const provider = byLabel(dom.get('inspector'), 'Provedor').children.find((c) => c.tagName === 'input');
  provider.value = 'deepseek';
  provider._listeners.input();

  const apply = findAll(dom.get('inspector'), 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();

  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, `an /plan went out, got ${JSON.stringify(posted.map((p) => p.url))}`);
  assert.deepEqual(planCall.body.policy, { tiers: { T2: { provider: 'deepseek' } } },
    'the body is the fragment, not the snapshot');
  assert.deepEqual(Object.keys(planCall.body.policy.tiers.T2), ['provider'],
    'a field the operator never touched does not ride along');
  assert.ok(!('fallback' in planCall.body.policy.tiers.T2), 'no untouched list travels with the patch');
});

test('a classifier edit never carries read-only keys back to the server (§5.2)', async () => {
  // classifier.temperature / max_tokens / timeout_seconds / chain are read and
  // never rewritten; a write of the whole block would put the snapshot's value
  // back over whatever a CLI edit set meanwhile.
  const posted = [];
  const policy = {
    rules: [], tiers: {},
    classifier: { model: 'glm-4.7', provider: 'zai', temperature: 0.2, max_tokens: 128, timeout_seconds: 15 },
  };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai' },
    'gpt-5.6-luna': { provider: 'openai-codex' },
  };
  api.setMode('editing');
  api.renderInspector({ id: 'classifier', name: 'classifier', bind: 'classifier' });

  const model = byLabel(dom.get('inspector'), 'Modelo').children.find((c) => c.tagName === 'select');
  model.value = 'gpt-5.6-luna';
  model._listeners.change();

  const apply = findAll(dom.get('inspector'), 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();

  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.deepEqual(planCall.body.policy, { classifier: { model: 'gpt-5.6-luna', provider: 'openai-codex' } },
    'only the two editable keys leave the screen');
  assert.ok(!('temperature' in planCall.body.policy.classifier), 'temperature is read-only');
  assert.ok(!('max_tokens' in planCall.body.policy.classifier), 'max_tokens is read-only');
});

test('a rule edit sends the WHOLE rules list, because lists replace wholesale (§5.2)', async () => {
  // Mutation 5: there is no partial rules patch — the server replaces the list
  // (service.py:422-434), so a patch with one rule missing would delete it on
  // disk. This is exactly why the staleness guard is mandatory, not optional.
  const posted = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(rulePolicy())) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = rulePolicy();
  api.setMode('editing');
  api.renderInspector({ id: 'rule:dead', name: 'dead', bind: 'rule', ruleIndex: 1 });

  const toggle = findAll(dom.get('inspector'), 'btn').find((b) => /Desativar/.test(b.textContent || ''));
  toggle._listeners.click();
  const apply = findAll(dom.get('inspector'), 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();

  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.deepEqual(Object.keys(planCall.body.policy), ['rules'], 'the list is the only surface a rule edit touches');
  assert.deepEqual(planCall.body.policy.rules.map((r) => r.id), ['broad', 'dead', 'r3'],
    'the WHOLE list, every rule — a partial list would delete the rest');
  assert.equal(planCall.body.policy.rules[1].enabled, false, 'the toggle rides the list');
});

test('"Ver o que muda" revalidates /lint exactly once before planning (§5.5)', async () => {
  // Mutation 6: one lint read per preview click — zero means the console plans
  // against a lint state the file left behind, two means it lints twice for one
  // question.
  const lints = [];
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/lint')) lints.push(url);
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, errors: [], error_targets: [], policy: {}, diff: '-a\n+b', base_hash: 'h' })) });
    },
  });
  api.state.policy = {};
  const msg = { textContent: '', className: '' };
  const diff = { hidden: true, textContent: '' };
  await api.doPreview({ rules: [] }, msg, diff);

  assert.equal(lints.length, 1, 'exactly one lint read per preview click');
  assert.equal(diff.hidden, false, 'and the plan still renders its diff');
  assert.match(msg.textContent, /Nenhum problema encontrado/);
});

test('a preview is refused when /lint finds an error in the CURRENT file, with the jump (§5.5)', () => {
  // The lint error is the file's, not this draft's: the preview does not plan,
  // the banner carries the error, and the [ Ir para a regra ] jump exists when
  // the error names a row.
  const planned = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (/\/plan$/.test(url)) planned.push(url);
      if (url.endsWith('/lint')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: false, errors: ['rule references unknown tier T9'], error_targets: [{ later_index: 0, later_id: 'r1' }] })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
    },
  });
  api.state.policy = {};
  const msg = { textContent: '', className: '' };
  const diff = { hidden: true, textContent: '' };
  return api.doPreview({ rules: [] }, msg, diff).then(() => {
    assert.deepEqual(planned, [], 'no plan for a file that fails lint');
    assert.equal(msg.textContent, 'Não é possível salvar enquanto houver erro. 1 erro(s) no arquivo.',
      '§4.7: the lint gate says the count, not the first raw error');
    assert.match(msg.className, /bad/);
    assert.equal(diff.hidden, true, 'no diff is shown for a plan that was never made');
    assert.match(flat(dom.get('warnings')), /Ir para a regra 1/, 'the banner carries the jump');
  });
});

test('a preset preview is refused by the same lint gate, before any plan', async () => {
  const planned = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (/\/plan$/.test(url)) planned.push(url);
      if (url.endsWith('/lint')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: false, errors: ['fail_safe missing'], error_targets: [] })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
    },
  });
  api.state.loading = false;
  api.state.policy = presetPolicy();
  api.state.preset = 'economizar';
  api.renderPresets();
  await api.previewPreset();

  assert.deepEqual(planned, [], 'no plan while the file fails lint');
  assert.equal(dom.get('presetMsg').textContent, 'Não é possível salvar enquanto houver erro. 1 erro(s) no arquivo.',
    '§4.7: the preset gate says the count, not the first raw error');
  const labels = (dom.get('presetActions').children || []).map((k) => String(k.textContent || ''));
  assert.equal(labels.indexOf('Salvar'), -1, 'no Salvar while the file carries an error (CA8)');
});

test('a refused write rebuilds the inspector from the reloaded policy, dropping the stale draft', async () => {
  // The refusal reloads AND re-renders the open panel: a stale draft left in
  // place would be exactly the stale list the guard just refused, one click
  // away from being applied against the fresh snapshot.
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ tiers: { T2: { model: 'new', provider: 'zai' } } })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = { tiers: { T2: { model: 'old', provider: 'zai' } } };
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });

  const provider = byLabel(dom.get('inspector'), 'Provedor').children.find((c) => c.tagName === 'input');
  provider.value = 'deepseek';
  provider._listeners.input();
  assert.equal(api.state.draft.tiers.T2.provider, 'deepseek', 'the draft accepted the edit');

  const apply = findAll(dom.get('inspector'), 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();

  assert.equal(dom.get('nodeMsg').textContent,
    'O arquivo mudou por fora desde que esta tela leu: o grupo T2 mudou. Recarreguei tudo; confira e tente de novo.',
    'the rebuilt panel names the group that drifted, on its message line');
  assert.deepEqual(api.state.policy, { tiers: { T2: { model: 'new', provider: 'zai' } } },
    'the reload replaced the snapshot');
  const fresh = byLabel(dom.get('inspector'), 'Provedor').children.find((c) => c.tagName === 'input');
  assert.equal(fresh.value, 'zai', 'the rebuilt panel shows the reloaded value, not the stale draft');
});

// ── §4.7 + §2.7: one vocabulary, one map, one temporal scope ─────────────
// The write surfaces used to speak two languages on one screen: the inspector
// said Apply/Preview/Revert with Rejected/Invalid/Checking…/Written. while the
// editor and the presets said the §4.7 literals. Every phrase now lives in the
// WRITE map; these tests pin the exact literals, that the map is the only
// copy, and the §2.7 line that follows a written save (and only a written one).

test('the §4.7 write literals are exact, and each lives in exactly one place: the WRITE map', () => {
  const { api } = loadConsole();
  assert.equal(api.WRITE.plan, 'Ver o que muda');
  assert.equal(api.WRITE.save, 'Salvar');
  assert.equal(api.WRITE.remove, 'Remover o bloqueio');
  assert.equal(api.WRITE.removing, 'Removendo o bloqueio…');
  assert.equal(api.WRITE.saving, 'Salvando…');
  assert.equal(api.WRITE.revert, 'Voltar à versão anterior');
  assert.equal(api.WRITE.revertConfirm, 'Confirmar: voltar à versão anterior');
  assert.equal(api.WRITE.checking, 'Verificando…');
  assert.equal(api.WRITE.noDraft, 'Não há o que salvar.');
  assert.equal(api.WRITE.noop, 'Nada mudou em relação ao arquivo atual, então não há o que salvar. Salvar do mesmo jeito apagaria a cópia que "Voltar à versão anterior" restauraria.');
  assert.equal(api.WRITE.lintError, 'Não é possível salvar enquanto houver erro. {N} erro(s) no arquivo.');
  assert.equal(api.WRITE.conflict, 'O arquivo mudou por fora desde que esta tela leu. Recarreguei tudo; confira e tente de novo.');
  assert.equal(api.WRITE.inFlight, 'Uma gravação já está em andamento.');
  assert.equal(api.WRITE.saved, 'Salvo. Vale para as próximas tarefas. Tarefas já em execução continuam no modelo que já escolheram. Não precisa reiniciar nada.');
  assert.equal(api.WRITE.httpError, 'Falhou (HTTP {st}).');
  assert.equal(api.WRITE.invalid, 'Não é possível salvar: {reasons}.');

  // The map is the ONLY copy: no surface re-spells a literal. The short labels
  // are counted in their quoted form so the no-op's internal quote of
  // "Voltar à versão anterior" does not read as a duplicate — it is the spec's
  // own sentence, quoting the button it refuses to press.
  const code = stripCommentsForCounting(fs.readFileSync(sourcePath, 'utf8'));
  const once = [
    `'${api.WRITE.plan}'`, `'${api.WRITE.save}'`, `'${api.WRITE.remove}'`, `'${api.WRITE.removing}'`, `'${api.WRITE.saving}'`,
    `'${api.WRITE.revert}'`, `'${api.WRITE.revertConfirm}'`, `'${api.WRITE.checking}'`,
    `'${api.WRITE.noDraft}'`, `'${api.WRITE.noop}'`, `'${api.WRITE.lintError}'`,
    `'${api.WRITE.conflict}'`, `'${api.WRITE.inFlight}'`, `'${api.WRITE.saved}'`,
    // This card's additions — the named staleness refusal and its "cannot
    // name" fallback live in the map, once each, like the sentence they grow.
    `'${api.WRITE.conflictNamed}'`, `'${api.WRITE.conflictUnknown}'`,
    `'${api.WRITE.httpError}'`, `'${api.WRITE.invalid}'`,
    'Não é possível {action} com esta tela aberta fora do Hermes One: o navegador não manda a credencial da sessão. Abra o Hermes One e volte aqui pelo menu lateral.',
    // This card's additions — every new surface word lives in the map, once.
    `'${api.WRITE.refresh}'`, `'${api.WRITE.refreshing}'`, `'${api.WRITE.done}'`,
    `'${api.WRITE.stopEditing}'`, `'${api.WRITE.routing}'`, `'${api.WRITE.routingOn}'`,
    `'${api.WRITE.routingOff}'`, `'${api.WRITE.routingVerdict}'`, `'${api.WRITE.banned}'`,
    `'${api.WRITE.cooldownLeft}'`, `'${api.WRITE.textEdit}'`, `'${api.WRITE.loading}'`,
  ];
  once.forEach((lit) => {
    const n = code.split(lit).length - 1;
    assert.equal(n, 1, `the literal ${lit.slice(0, 44)}… must appear exactly once (the map), found ${n}`);
  });
  // The lint-error sentence's HEAD — up to the interpolation — once. The full
  // literal above cannot catch a re-spelled copy ("2 erros no arquivo" instead
  // of "2 erro(s) no arquivo"), which is exactly how the second copy was born.
  const head = 'Não é possível salvar enquanto houver erro';
  assert.equal(code.split(head).length - 1, 1,
    `the lint sentence's head must exist once (the map), found ${code.split(head).length - 1}`);
});

test('the save button says Salvando… in flight and returns to Salvar (§4.7)', async () => {
  let releaseWrite;
  const gate = new Promise((resolve) => { releaseWrite = resolve; });
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      if (url.endsWith('/plan')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
      }
      if (url.endsWith('/apply')) {
        return gate.then(() => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) }));
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.policy = {};
  const msg = { textContent: '', className: '' };
  const btn = dom.get('jsonApply');
  const run = api.doApply('/apply', msg, { rules: [] }, null, btn);
  await tick();
  assert.equal(btn.textContent, 'Salvando…', 'the button that pressed says what the write is doing');
  releaseWrite();
  await run;
  assert.equal(btn.textContent, 'Salvar', 'and returns to the §4.7 label when the write is over');
  assert.equal(btn.hidden, true, '§2.7: the line takes the button’s place for eight seconds');
  assert.match(msg.textContent, /Vale para as próximas tarefas/);
});

test('a no-op save says both §4.7 sentences, and never claims the §2.7 scope', async () => {
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '', base_hash: 'h' })) });
    },
  });
  api.state.policy = {};
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { rules: [] });
  assert.match(msg.textContent, /Nada mudou em relação ao arquivo atual, então não há o que salvar\./);
  assert.match(msg.textContent, /Salvar do mesmo jeito apagaria a cópia que "Voltar à versão anterior" restauraria\./,
    '§4.7: the no-op says the whole consequence — the second sentence is the reason there is no Salvar');
  assert.match(msg.className, /ok/);
  assert.doesNotMatch(msg.textContent, /Vale para as próximas tarefas/,
    '§2.7: nothing was written, so nothing claims a temporal scope');
});

test('a failed write never shows the §2.7 line, and the button is back to Salvar', async () => {
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      if (url.endsWith('/plan')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: false, status: 500, text: () => Promise.resolve('{}') });
    },
  });
  api.state.policy = {};
  const msg = { textContent: '', className: '' };
  const btn = dom.get('jsonApply');
  await api.doApply('/apply', msg, { rules: [] }, null, btn);
  assert.match(msg.textContent, /Falhou \(HTTP 500\)/);
  assert.match(msg.className, /bad/);
  assert.doesNotMatch(msg.textContent, /Vale para as próximas tarefas/,
    'a failed write must not claim the §2.7 scope');
  assert.equal(btn.textContent, 'Salvar',
    'the button is back to the label after the refusal');
  assert.equal(btn.hidden, false, 'and nothing sits in its place');
});

test('the lint gate counts the real N instead of quoting the first error raw', async () => {
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/lint')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: false, errors: ['one', 'two'], error_targets: [] })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  const msg = { textContent: '', className: '' };
  await api.doPreview({ rules: [] }, msg, null);
  assert.equal(msg.textContent, 'Não é possível salvar enquanto houver erro. 2 erro(s) no arquivo.',
    'the {N} is the real count — a hard-coded 1 would pass a single-error test');
  assert.equal(msg.textContent.indexOf('one'), -1, 'the raw error text is not forwarded');
  assert.equal(msg.textContent.indexOf('two'), -1);
});

// ── §2.2: one destination control, five closed options ──────────────────
// The rule's destination and the default's used to be different controls
// with different vocabularies: a <select> of tiers keys for the rule, two
// free-text fields (Destino/Ação) for the default. §2.2 makes it ONE
// component with five closed options — the four classifier anchors (then
// every other tiers key alphabetically), the classifier, a refusal, and a
// fixed model — and every option writes EXACTLY the three keys of §2.2,
// the other two as null.

function destPolicy() {
  return {
    rules: [
      { id: 'r1', when: {}, then: { model: 'T4' } },
      { id: 'r2', when: {}, then: { model: 'glm-5.3', provider: 'zai' } },
      { id: 'r3', when: {}, then: { action: 'classify' } },
      { id: 'r4', when: {}, then: { deny: true } },
      { id: 'r5', when: {}, then: { model: 'T9' } },
    ],
    default: { action: 'classify' },
    fail_safe: { model: 'glm-4.7', provider: 'zai' },
    tiers: {
      T1: { model: 'a', provider: 'zai' },
      T2: { model: 'b', provider: 'zai', fallback: [{ model: 'c', provider: 'zai' }, { model: 'd', provider: 'zai' }] },
      T3: { model: 'e', provider: 'zai' },
      T4: { model: 'f', provider: 'zai', fallback: [{ model: 'g', provider: 'zai' }] },
      zeta: { model: 'h', provider: 'zai' },
      alpha: { model: 'i', provider: 'zai' },
    },
  };
}

const DEST_OPTION_VALUES = ['T1', 'T2', 'T3', 'T4', 'alpha', 'zeta', '', '__classify', '__deny', '__fixed'];

test('the destination control offers the five closed options of §2.2, in order', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = destPolicy();
  api.state.capabilities = { 'glm-5.3': { provider: 'zai' } };
  api.setMode('editing');
  api.renderInspector({ id: 'rule:r1', name: 'r1', bind: 'rule', ruleIndex: 0 });
  const wrap = byLabel(dom.get('inspector'), 'Destino');
  const select = wrap.children.find((c) => c.tagName === 'select');
  assert.ok(select, 'the destination is a <select>, not free text');
  const values = select.children.map((o) => o.value);
  assert.deepEqual(values, DEST_OPTION_VALUES,
    'the four anchors, the other keys alphabetically, the separator, then the three remaining options');
  const texts = select.children.map((o) => o.textContent);
  assert.equal(texts[0], 'Grupo T1 · Trivial');
  assert.equal(texts[3], 'Grupo T4 · Difícil');
  assert.equal(texts[4], 'Grupo alpha · sem descrição');
  assert.equal(texts[5], 'Grupo zeta · sem descrição');
  assert.equal(select.children[6].disabled, true, 'the separator cannot be picked');
  assert.equal(texts[7], 'Perguntar ao classificador qual grupo usar');
  assert.equal(texts[8], 'Recusar a tarefa');
  assert.equal(texts[9], 'Um modelo fixo, sem reserva (avançado)');
  assert.equal(select.value, 'T4', 'the current destination is selected');

  // A destination the table no longer names still shows — the lint finds
  // the missing group, the select must not blank the value.
  api.renderInspector({ id: 'rule:r5', name: 'r5', bind: 'rule', ruleIndex: 4 });
  const wrap5 = byLabel(dom.get('inspector'), 'Destino');
  const select5 = wrap5.children.find((c) => c.tagName === 'select');
  assert.equal(select5.value, 'T9', 'the missing group stays selected');
  assert.equal(select5.children.map((o) => o.value)[6], 'T9',
    'and it is present as an option, appended after the table keys, before the separator');
});

test('each destination option writes exactly the three §2.2 keys, the others null', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = destPolicy();
  api.state.capabilities = { 'gpt-5.6-luna': { provider: 'openai-codex', context_window: 1000000 } };
  api.setMode('editing');
  api.renderInspector({ id: 'rule:r1', name: 'r1', bind: 'rule', ruleIndex: 0 });
  const wrap = byLabel(dom.get('inspector'), 'Destino');
  const select = wrap.children.find((c) => c.tagName === 'select');
  const then = () => api.state.draft.rules[0].then;

  select.value = 'T2'; select._listeners.change();
  assert.deepEqual(plain(then()), { model: 'T2', action: null, deny: null },
    'grupo → then.model, and action/deny as null');

  // Picked straight from a model-bearing state, so a "keeps the model"
  // mutation cannot hide behind a previous option having already nulled it.
  select.value = '__deny'; select._listeners.change();
  assert.deepEqual(plain(then()), { model: null, action: null, deny: true },
    'recusar → then.deny, and model/action as null — no model left behind');

  select.value = 'T2'; select._listeners.change();
  select.value = '__classify'; select._listeners.change();
  assert.deepEqual(plain(then()), { model: null, action: 'classify', deny: null },
    'classificador → then.action, and model/deny as null — no model left behind');

  select.value = '__fixed'; select._listeners.change();
  assert.deepEqual(plain(then()), { model: null, action: null, deny: null },
    'fixo → nothing written until a model is picked');
  const fixedWrap = wrap.children.find((c) => c.tagName !== 'label' && c.tagName !== 'select'
    && !String(c.className).includes('field-note'));
  const modelWrap = byLabel(fixedWrap, 'Modelo');
  assert.ok(modelWrap, 'the fixed option reveals the one model picker');
  const modelSelect = modelWrap.children.find((c) => c.tagName === 'select');
  modelSelect.value = 'gpt-5.6-luna'; modelSelect._listeners.change();
  assert.deepEqual(plain(then()), { model: 'gpt-5.6-luna', provider: 'openai-codex', action: null, deny: null },
    'fixo → model + provider come from the catalogue entry');
});

test('the default line uses the SAME destination control as a rule', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = destPolicy();
  api.setMode('editing');
  api.renderInspector({ id: 'rule:r1', name: 'r1', bind: 'rule', ruleIndex: 0 });
  const ruleWrap = byLabel(dom.get('inspector'), 'Destino');
  const ruleSelect = ruleWrap.children.find((c) => c.tagName === 'select');

  api.renderInspector({ id: 'default', name: 'default', bind: 'default' });
  const box = dom.get('inspector');
  const defWrap = byLabel(box, 'Destino');
  assert.ok(defWrap, 'the default has a destination field');
  const defSelect = defWrap.children.find((c) => c.tagName === 'select');
  assert.ok(defSelect, 'and it is a <select> — the free-text Destino is gone');
  assert.deepEqual(defSelect.children.map((o) => o.value), ruleSelect.children.map((o) => o.value),
    'identical options to the rule\'s control — the same component');
  assert.equal(defSelect.value, '__classify', 'the current action:classify maps to the classifier option');
  assert.ok(!box.children.some((c) => (c.children[0] || {}).textContent === 'Ação'),
    'the free-text Ação field is gone — classify is one of the five options now');
  assert.equal(box.children.filter((c) => c.tagName === 'input').length, 0,
    'no free-text field remains in the default panel');
});

test('a default destination edit writes the three keys through the same patch path', async () => {
  const posted = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(destPolicy())) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = destPolicy();
  api.setMode('editing');
  api.renderInspector({ id: 'default', name: 'default', bind: 'default' });
  const wrap = byLabel(dom.get('inspector'), 'Destino');
  const select = wrap.children.find((c) => c.tagName === 'select');
  select.value = 'T2';
  select._listeners.change();
  const apply = findAll(dom.get('inspector'), 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();

  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.deepEqual(planCall.body.policy, { default: { model: 'T2', action: null, deny: null } },
    'the default patch carries exactly the three §2.2 keys, nothing else');
});

test('profile is a <select> with the policy union plus Outro papel… (§2.2)', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = destPolicy();
  policy.rules[0].then.profile = 'coder';
  policy.rules[1].then.profile = 'reviewer';
  policy.fail_safe.profile = 'coder';
  api.state.policy = policy;
  api.setMode('editing');
  api.renderInspector({ id: 'rule:r1', name: 'r1', bind: 'rule', ruleIndex: 0 });
  const wrap = byLabel(dom.get('inspector'), 'Papel');
  assert.ok(wrap, 'the Papel field exists');
  const select = wrap.children.find((c) => c.tagName === 'select');
  assert.ok(select, 'Papel is a <select>, not a text input');
  const values = select.children.map((o) => o.value);
  assert.ok(values.includes('coder') && values.includes('reviewer'),
    'the union of the profile values present in the policy');
  assert.equal(values.filter((v) => v === 'coder').length, 1, 'union, no duplicates');
  assert.ok(values.includes('__other'), 'Outro papel… is the escape hatch');
  assert.equal(select.value, 'coder', 'the current profile is selected');

  select.value = 'reviewer'; select._listeners.change();
  assert.equal(api.state.draft.rules[0].then.profile, 'reviewer', 'picking a union value writes it');

  select.value = '__other'; select._listeners.change();
  const input = wrap.children.find((c) => c.tagName === 'input');
  assert.ok(input && input.hidden === false, 'Outro papel… reveals a text input');
  input.value = 'security'; input._listeners.input();
  assert.equal(api.state.draft.rules[0].then.profile, 'security', 'typing writes the new role');

  select.value = ''; select._listeners.change();
  assert.equal(api.state.draft.rules[0].then.profile, null, 'the blank option removes the role');
});

test('fixo-sem-reserva shows only on the fixed option, with the N of the group being left', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = destPolicy();
  api.state.capabilities = { 'glm-5.3': { provider: 'zai' } };
  api.setMode('editing');
  api.renderInspector({ id: 'rule:r1', name: 'r1', bind: 'rule', ruleIndex: 0 });
  const wrap = byLabel(dom.get('inspector'), 'Destino');
  const select = wrap.children.find((c) => c.tagName === 'select');
  const note = wrap.children.find((c) => String(c.className).includes('field-note'));
  assert.equal(note.hidden, true, 'no warning while a group is selected');

  select.value = '__fixed'; select._listeners.change();
  assert.equal(note.hidden, false, 'the warning appears when the fixed option is chosen');
  assert.match(note.textContent, /Um grupo, no lugar dele, teria 2 tentativas\./,
    'N is the chain length of the group the rule was leaving (T4: primary + 1 reserve)');

  select.value = '__deny'; select._listeners.change();
  assert.equal(note.hidden, true, 'the warning is NOT rendered on the deny option');

  select.value = 'T2'; select._listeners.change();
  assert.equal(note.hidden, true, 'nor on a group option');

  select.value = '__fixed'; select._listeners.change();
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /teria 3 tentativas\./,
    'leaving T2 (chain of 3), the N follows the group actually in use');

  select.value = '__classify'; select._listeners.change();
  assert.equal(note.hidden, true, 'nor on the classifier option');

  select.value = '__fixed'; select._listeners.change();
  assert.equal(note.hidden, false);
  assert.doesNotMatch(note.textContent, /teria \d+ tentativas?\./,
    'leaving a non-group option: the short phrase, no N invented');
  assert.match(note.textContent, /sem tentar mais nada\./, 'the head sentence is the §4.7 literal');
});

test('a rule already on a fixed model shows the warning at mount, with the picker open', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = destPolicy();
  api.state.capabilities = { 'glm-5.3': { provider: 'zai', context_window: 200000 } };
  api.setMode('editing');
  api.renderInspector({ id: 'rule:r2', name: 'r2', bind: 'rule', ruleIndex: 1 });
  const wrap = byLabel(dom.get('inspector'), 'Destino');
  const select = wrap.children.find((c) => c.tagName === 'select');
  assert.equal(select.value, '__fixed', 'a concrete model id maps to the fixed option');
  const note = wrap.children.find((c) => String(c.className).includes('field-note'));
  assert.equal(note.hidden, false, 'the warning is present at mount, not only after a change');
  assert.match(note.textContent, /^Um modelo fixo aqui não tem reserva:/, 'the §4.7 head, literal');
  assert.doesNotMatch(note.textContent, /teria \d+ tentativas?\./, 'no N invented without a reference group');
  const fixedWrap = wrap.children.find((c) => c.tagName !== 'label' && c.tagName !== 'select'
    && !String(c.className).includes('field-note'));
  assert.ok(byLabel(fixedWrap, 'Modelo'), 'the model picker is open for the fixed option');
});

// ── §2.5: the group's chain, edited as rows ──────────────────────────
// Editing a group used to stop at the primary's model and provider; the
// reserves were read-only, which contradicted §2.5. These tests pin the
// chain editor: every attempt is a row (primary first, then each reserve)
// with model/provider/billing_mode and ↑/↓/Remover, plus Adicionar
// tentativa below the last row. Order on screen is the order saved; the
// primary lives in tiers.<chave>.{model, provider, billing_mode} and never
// leaks into fallback; removing the last reserve writes `fallback: []`.

function rowField(row, label) {
  return (row.children || []).find((c) => String(c.className || '').includes('field')
    && (c.children[0] || {}).textContent === label);
}
function rowButtons(row) {
  return (row.children || []).find((c) => String(c.className || '').includes('row-ops'));
}
function nodeMsg(box) {
  return box.children.find((c) => c.id === 'nodeMsg');
}
function capModels() {
  return {
    'glm-4.7': { provider: 'zai', context_window: 200000 },
    'gpt-5.6-luna': { provider: 'openai-codex', context_window: 1000000 },
    'mimo-v2.5': { provider: 'xiaomi', context_window: 1048576 },
    'glm-5.3': { provider: 'zai', context_window: 200000 },
    'deepseek-v4-pro': { provider: 'deepseek', context_window: 200000 },
    'gpt-5.5': { provider: 'openai-codex', context_window: 400000 },
  };
}

test('the tier editor draws the queue as rows: primary first, then each reserve in order', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = tierPolicy();
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  assert.equal(rows.length, 3, 'T2 has a primary and two reserves — three rows');
  assert.deepEqual(findAll(box, 'chain-head').map((n) => n.textContent),
    ['Primeira tentativa', 'Reserva', 'Reserva'], 'the panel shows list order, not an invented numbering');
  const models = rows.map((r) => rowField(r, 'Modelo').children.find((c) => c.tagName === 'select').value);
  assert.deepEqual(models, ['glm-5.3', 'deepseek-v4-pro', 'gpt-5.5'],
    'the rows read the file order — screen order is the queue order');
  rows.forEach((r, i) => {
    const ops = rowButtons(r);
    assert.ok(ops, `row ${i} carries its controls`);
    assert.deepEqual(ops.children.map((b) => b.textContent), ['↑', '↓', 'Remover'],
      `row ${i} has the three controls of §2.5`);
  });
  const add = findAll(box, 'btn').find((b) => b.textContent === 'Adicionar tentativa');
  assert.ok(add, 'Adicionar tentativa sits below the last row');
  assert.ok(flat(box).includes(
    "Como você paga por esta opção. Não é etiqueta: 'pelo mais barato agora' ordena por isso, e o teto de preço só tira da fila as opções pagas em dinheiro."),
  'the §2.5 billing support text is literal');
});

test('Adicionar tentativa creates the fallback list with a blank reserve; the primary is not removable from an empty queue', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  delete policy.tiers.T1.fallback;
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  assert.equal(findAll(box, 'chain-row').length, 1, 'no fallback declared → one row');

  // Removing the primary from a single-attempt group would leave the group
  // with no first attempt at all — the button says so and moves nothing.
  const primaryOps = rowButtons(findAll(box, 'chain-row')[0]);
  primaryOps.children.find((b) => b.textContent === 'Remover')._listeners.click();
  assert.match(nodeMsg(box).textContent, /sem nenhuma opção/);
  assert.ok(!api.state.draft.tiers.T1.fallback, 'no list was born from a refused removal');

  const add = findAll(box, 'btn').find((b) => b.textContent === 'Adicionar tentativa');
  add._listeners.click();
  const draft = api.state.draft.tiers.T1;
  assert.ok(Array.isArray(draft.fallback), 'clicking add creates the fallback list');
  assert.equal(draft.fallback.length, 1, 'with one reserve');
  assert.deepEqual(Object.keys(draft.fallback[0]), [], 'the new reserve is blank — the operator fills it');
  assert.equal(findAll(box, 'chain-row').length, 2, 'and the row renders');
  assert.equal(findAll(box, 'chain-head')[1].textContent, 'Reserva');
});

test('removing the last reserve writes fallback: [] — a declaration, not absence', async () => {
  const posted = [];
  const policy = tierPolicy();
  policy.tiers.T1.fallback = [{ model: 'mimo-v2.5', provider: 'xiaomi', billing_mode: 'metered' }];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  assert.equal(rows.length, 2, 'primary + one reserve');
  const reserveOps = rowButtons(rows[1]);
  reserveOps.children.find((b) => b.textContent === 'Remover')._listeners.click();
  assert.deepEqual(api.state.draft.tiers.T1.fallback, [],
    'the reserve is gone and the list is EXPLICITLY empty');
  assert.equal(findAll(box, 'chain-row').length, 1, 'the row left the screen');

  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.ok('fallback' in planCall.body.policy.tiers.T1, 'fallback rides the patch');
  assert.deepEqual(planCall.body.policy.tiers.T1.fallback, [],
    'the empty list is what is written — not null, not absence');
});

test('↑ on the first row and ↓ on the last row move nothing and say so', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = tierPolicy();
  api.state.loading = false;
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  assert.equal(rows.length, 3);
  const before = plain(api.state.draft.tiers.T2);

  rowButtons(rows[0]).children.find((b) => b.textContent === '↑')._listeners.click();
  assert.match(nodeMsg(box).textContent, /nada a mover/,
    'the head control says the row is already first');
  // plain() both sides: the draft now carries the panel's stable floor object
  // (requirements: {} when the file declares none), and a nested empty object
  // from the VM realm never deepEquals one from this realm.
  assert.deepEqual(plain(api.state.draft.tiers.T2), before,
    '↑ on the head does not rotate the queue — no wrap-around');

  rowButtons(rows[2]).children.find((b) => b.textContent === '↓')._listeners.click();
  assert.match(nodeMsg(box).textContent, /nada a mover/,
    'the tail control says the row is already last');
  assert.deepEqual(plain(api.state.draft.tiers.T2), before,
    '↓ on the tail moves nothing either');
});

test('moving the first reserve up promotes it: order on screen is the order saved', async () => {
  const posted = [];
  const policy = tierPolicy();
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  rowButtons(rows[1]).children.find((b) => b.textContent === '↑')._listeners.click();

  const draft = api.state.draft.tiers.T1;
  assert.equal(draft.model, 'gpt-5.6-luna', 'the promoted reserve is now the primary');
  assert.equal(draft.provider, 'openai-codex');
  assert.equal(draft.billing_mode, 'subscription');
  assert.deepEqual(draft.fallback, [
    { model: 'glm-4.7', provider: 'zai', billing_mode: 'plan' },
    { model: 'mimo-v2.5', provider: 'xiaomi', billing_mode: 'metered' },
  ], 'the old primary became the first reserve');
  const freshModels = findAll(box, 'chain-row')
    .map((r) => rowField(r, 'Modelo').children.find((c) => c.tagName === 'select').value);
  assert.deepEqual(freshModels, ['gpt-5.6-luna', 'glm-4.7', 'mimo-v2.5'],
    'the rows re-render in the new order — screen order is the queue order');

  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  const tier = planCall.body.policy.tiers.T1;
  assert.equal(tier.model, 'gpt-5.6-luna', 'the promoted reserve is written as the primary');
  assert.deepEqual(tier.fallback.map((e) => e.model), ['glm-4.7', 'mimo-v2.5'],
    'the saved order is the on-screen order');
});

test('Remover on the primary promotes the first reserve instead of emptying the group', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = tierPolicy();
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  assert.equal(rows.length, 3, 'primary + two reserves');
  rowButtons(rows[0]).children.find((b) => b.textContent === 'Remover')._listeners.click();
  const draft = api.state.draft.tiers.T1;
  assert.equal(draft.model, 'gpt-5.6-luna', 'the first reserve becomes the primary');
  assert.deepEqual(draft.fallback.map((e) => e.model), ['mimo-v2.5'],
    'the removed attempt LEAVES the queue — keeping it as the last reserve would be a rotation');
  assert.equal(findAll(box, 'chain-row').length, 2, 'two rows remain');
});

test('the first attempt lives on the tier, never inside fallback', async () => {
  const posted = [];
  const policy = tierPolicy();
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  const primaryModel = rowField(rows[0], 'Modelo');
  const select = primaryModel.children.find((c) => c.tagName === 'select');
  select.value = 'gpt-5.6-luna';
  select._listeners.change();
  const draft = api.state.draft.tiers.T2;
  assert.equal(draft.model, 'gpt-5.6-luna', 'the primary model edits the tier key');
  assert.equal(draft.fallback.length, 2, 'fallback keeps its own two entries');
  assert.ok(!draft.fallback.some((e) => e.model === 'gpt-5.6-luna'),
    'the first attempt never leaks into the reserve list');

  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  const tier = planCall.body.policy.tiers.T2;
  assert.equal(tier.model, 'gpt-5.6-luna', 'the tier key carries the first attempt');
  assert.ok(!('fallback' in tier), 'a primary edit never touches the reserve list in the patch');
});

test('a billing mode the console has not learned renders as written, and the select refuses out-of-vocabulary values', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  policy.tiers.T1.billing_mode = 'barter';
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  const billing = rowField(rows[0], 'Modo de pagamento');
  const select = billing.children.find((c) => c.tagName === 'select');
  const values = [];
  select.children.forEach((o) => values.push(o.value));
  assert.ok(values.indexOf('barter') !== -1, 'the unknown mode is an option, not swallowed');
  assert.equal(select.value, 'barter', 'and it is the selected one — the file is shown as it is');
  const note = billing.children.find((c) => String(c.className).includes('field-note'));
  assert.match(note.textContent, /'pelo mais barato agora' ordena por isso/, 'the §2.5 support text is literal');

  // The vocabulary is closed (capabilities.BILLING_MODES): a scripted select
  // cannot push an invented mode into the draft.
  select.value = 'credit_voucher';
  select._listeners.change();
  assert.equal(api.state.draft.tiers.T1.billing_mode, 'barter',
    'a value outside the option set never lands in the draft');
  select.value = 'metered';
  select._listeners.change();
  assert.equal(api.state.draft.tiers.T1.billing_mode, 'metered', 'choosing a known mode writes it');
  select.value = '';
  select._listeners.change();
  assert.equal(api.state.draft.tiers.T1.billing_mode, null,
    'clearing the mode is an explicit null — removing a key is a declaration (§2.1)');
});

test("choosing a model in a reserve row fills THAT entry's provider, not the primary's", () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = tierPolicy();
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  const reserveModel = rowField(rows[1], 'Modelo');
  const select = reserveModel.children.find((c) => c.tagName === 'select');
  select.value = 'mimo-v2.5';
  select._listeners.change();
  const entry = api.state.draft.tiers.T2.fallback[0];
  assert.equal(entry.model, 'mimo-v2.5');
  assert.equal(entry.provider, 'xiaomi', "the reserve entry's provider follows its model");
  assert.equal(api.state.draft.tiers.T2.provider, 'zai', "the primary's provider is untouched");
});

test('editing a reserve\'s billing writes the WHOLE fallback list — lists replace wholesale', async () => {
  const posted = [];
  const policy = tierPolicy();
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  const billing = rowField(rows[2], 'Modo de pagamento');
  const select = billing.children.find((c) => c.tagName === 'select');
  select.value = 'free';
  select._listeners.change();

  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  const tier = planCall.body.policy.tiers.T2;
  assert.ok('fallback' in tier, 'the list rides the patch');
  assert.deepEqual(tier.fallback.map((e) => e.billing_mode), ['metered', 'free'],
    'the whole list, with the edit in place');
  assert.ok(!('model' in tier) && !('provider' in tier) && !('billing_mode' in tier),
    'a reserve edit touches only the list — no primary keys ride along');
});

// ── §2.8: the warnings that name the consequence, never the block ─────

test('a group with a single option warns that a failure goes to the last resort', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: { model: 'glm-4.7', provider: 'zai' } } };
  api.renderLadder();
  assert.match(flat(dom.get('ladder')),
    /Este grupo tem só uma opção\. Se ela falhar, a tarefa vai direto para o último recurso\./);
});

test('a group with not-exactly-one option never claims it has one', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  // Zero options: "só uma opção" over no option at all would be a false claim,
  // so the degenerate group keeps the older, accurate line instead.
  api.state.policy = { rules: [], default: {}, tiers: { T1: {} } };
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /só uma opção/);
  assert.match(flat(dom.get('ladder')), /Sem reserva declarada/);
  // Two options: the row is simply absent — render nothing for nothing.
  api.state.policy = { rules: [], default: {}, tiers: { T1: { model: 'glm-4.7', provider: 'zai', fallback: [{ model: 'mimo-v2.5', provider: 'xiaomi' }] } } };
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /só uma opção/);
});

test('all attempts on one provider warn that the whole group falls with it', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai',
    fallback: [{ model: 'glm-5.3', provider: 'zai' }, { model: 'glm-5.5', provider: 'zai' }],
  } } };
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /As 3 opções são do mesmo provedor\. Uma cota ou uma queda dele provavelmente derruba o grupo inteiro\./);
  assert.doesNotMatch(text, /As tentativas 1 e 2 caem as duas/,
    'one fact, one line — the pair note yields to the all-same row');
});

test('two names on one upstream are still one provider (nous resells openrouter)', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'kimi-k3', provider: 'nous',
    fallback: [{ model: 'glm-5.3', provider: 'openrouter' }],
  } } };
  api.renderLadder();
  assert.match(flat(dom.get('ladder')), /As 2 opções são do mesmo provedor\./);
});

test('distinct providers do not get the one-provider warning', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai',
    fallback: [{ model: 'mimo-v2.5', provider: 'xiaomi' }],
  } } };
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /mesmo provedor/);
});

test('all attempts paid the same way warn with the real count in both sentences', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai', billing_mode: 'metered',
    fallback: [{ model: 'mimo-v2.5', provider: 'xiaomi', billing_mode: 'metered' }],
  } } };
  api.renderLadder();
  // The fixed "3" of the §2.8 text would be a false statement over 2 options.
  assert.match(flat(dom.get('ladder')),
    /As 2 opções são pagas do mesmo jeito\. Um limite de cota provavelmente atinge as 2 juntas\./);
});

test('the same-way warning stays silent when it cannot be affirmed', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  // Mixed modes.
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai', billing_mode: 'plan',
    fallback: [{ model: 'mimo-v2.5', provider: 'xiaomi', billing_mode: 'metered' }],
  } } };
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /pagas do mesmo jeito/);
  // One attempt: the single-option row is already the fact (§2.8 decision 4).
  api.state.policy = { rules: [], default: {}, tiers: { T1: { model: 'glm-4.7', provider: 'zai', billing_mode: 'plan' } } };
  api.renderLadder();
  const one = flat(dom.get('ladder'));
  assert.doesNotMatch(one, /pagas do mesmo jeito/);
  assert.match(one, /só uma opção/);
  // A mode nobody knows: "pagas do mesmo jeito" over an unreadable mode is a guess.
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai',
    fallback: [{ model: 'mimo-v2.5', provider: 'xiaomi' }],
  } } };
  api.state.capabilities = { 'glm-4.7': {}, 'mimo-v2.5': {} };
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /pagas do mesmo jeito/);
});

test('a repeated model@provider names the later option as the one never used', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai',
    fallback: [{ model: 'mimo-v2.5', provider: 'xiaomi' }, { model: 'glm-4.7', provider: 'zai' }],
  } } };
  api.renderLadder();
  assert.match(flat(dom.get('ladder')),
    /A opção 3 é igual à opção 1\. A repetida nunca vai ser usada\./);
});

test('a duplicate is model AND provider, never one of them alone', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  // Same model, different rails: two real options.
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai',
    fallback: [{ model: 'glm-4.7', provider: 'deepseek' }],
  } } };
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /nunca vai ser usada/);
  // Same rail, different models: also two real options.
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai',
    fallback: [{ model: 'glm-5.3', provider: 'zai' }],
  } } };
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /nunca vai ser usada/);
});

test('a model the catalogue never described warns, naming the id', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai',
    fallback: [{ model: 'mystery-model', provider: 'xiaomi' }],
  } } };
  api.state.capabilities = { 'glm-4.7': { context_window: 200000 } };
  api.renderLadder();
  assert.match(flat(dom.get('ladder')),
    /O catálogo não conhece este id: mystery-model\. Ele vai rodar, mas nada aqui confere capacidade, janela ou preço dele\./);
});

test('the unknown-id warning needs a catalogue to exist — 404 is not "no"', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: {
    model: 'glm-4.7', provider: 'zai',
    fallback: [{ model: 'mystery-model', provider: 'xiaomi' }],
  } } };
  // /capabilities answered 404: without a catalogue there is no way to affirm
  // that the catalogue does not know the id — do not warn.
  api.state.capabilities = null;
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /não conhece este id/);
  // Every model known: nothing to say.
  api.state.capabilities = { 'glm-4.7': { context_window: 200000 }, 'mystery-model': { context_window: 128000 } };
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /não conhece este id/);
});

test('random without pin_primary sorts only the reserves — the router default is a pin', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  const policy = tierPolicy();
  policy.tiers.T1.fallback_strategy = 'random';
  // pin_primary ABSENT: rules._pin_primary_of defaults it to true, and the
  // console reads the same file the router does.
  api.state.policy = policy;
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /Com a ordem sorteada e o primeiro fixo, só as reservas são sorteadas\./);
  assert.doesNotMatch(text, /a primeira fica fixa; as outras/, 'the generic note yields to the §2.8 row');
});

test('the pin+random row needs pin_primary true and a draw', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  // pin_primary false: the primary is sorted too, so "só as reservas" is false.
  const policy = tierPolicy();
  policy.tiers.T1.fallback_strategy = 'random';
  policy.tiers.T1.pin_primary = false;
  api.state.policy = policy;
  api.renderLadder();
  const unpinned = flat(dom.get('ladder'));
  assert.doesNotMatch(unpinned, /só as reservas são sorteadas/);
  assert.match(unpinned, /todas as tentativas são sorteadas/, 'the generic note still tells the truth for the unpinned draw');
  // Sequential: there is no draw to sort.
  api.state.policy = tierPolicy();
  api.renderLadder();
  assert.doesNotMatch(flat(dom.get('ladder')), /só as reservas são sorteadas/);
});

test('reading mode names the gesture that unlocks editing (§4.7)', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.setMode('reading');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  assert.match(flat(dom.get('inspector')),
    /Só leitura\. Aperte "Editar" no topo para poder mudar algo\./);
});

test('editing mode never claims the surface is read-only', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  assert.doesNotMatch(flat(dom.get('inspector')), /Só leitura/);
});

test('a §2.8 warning never disables or hides the write controls', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: { model: 'glm-4.7', provider: 'zai' } } };
  api.setMode('editing');
  api.renderLadder();
  assert.match(flat(dom.get('ladder')), /só uma opção/, 'the warning is on screen');
  assert.equal(dom.get('jsonApply').disabled, false,
    'Salvar stays enabled — avisar nunca é bloquear, the only gate is the server lint');
});

test('tierWarnings returns exactly the rows a group deserves — nothing for nothing', () => {
  const { api } = loadConsole();
  const good = api.tierWarnings(
    { model: 'glm-4.7', provider: 'zai', fallback: [{ model: 'mimo-v2.5', provider: 'xiaomi' }] },
    [{ model: 'glm-4.7', provider: 'zai' }, { model: 'mimo-v2.5', provider: 'xiaomi' }], {});
  assert.deepEqual(plain(good), [], 'a healthy two-provider chain gains no frame, no line, no warning');
  const one = api.tierWarnings(
    { model: 'glm-4.7', provider: 'zai' },
    [{ model: 'glm-4.7', provider: 'zai' }], {});
  assert.deepEqual(plain(one), ['Este grupo tem só uma opção. Se ela falhar, a tarefa vai direto para o último recurso.']);
  const pin = api.tierWarnings(
    { model: 'a', provider: 'zai', fallback: [{ model: 'b', provider: 'xiaomi' }] },
    [{ model: 'a', provider: 'zai' }, { model: 'b', provider: 'xiaomi' }], { pinSort: true });
  assert.deepEqual(plain(pin), ['Com a ordem sorteada e o primeiro fixo, só as reservas são sorteadas.']);
});

// ── §3.4: each missing-model state names its remedy with a control ────────
// The four states already said the problem; this slice adds the control the
// spec's wireframes carry. One literal per fact, one surface per phrase (§4.8),
// and the remedies are controls that EXIST — the row's §2.2 select, the
// editor's Salvar, the model picker, the group's own requirements field.

// ── §3.4(a): the missing-group row carries its own remedy ─────────────────
// CA8 left the row marked ("⚠ Grupo T9 — não existe") and the banner naming
// the consequence. The wireframe adds the row's own control: the SAME §2.2
// destination select, prefixed "Escolha um destino que exista:", whose choice
// lands in the DRAFT and opens the rule's editor — the write path is the
// normal Salvar, never a silent save from the row.

test('the missing-group row shows the §3.4(a) inline destination select, only in editing mode', () => {
  const { api, dom } = loadConsole();
  missingGroupState(api);
  api.setMode('editing');
  api.renderSheet();

  const fixes = findAll(dom.get('sheet'), 'step-dest-fix');
  assert.equal(fixes.length, 1, 'exactly the one broken rule carries the fix');
  const wrap = fixes[0];
  assert.match(flat(wrap), /Escolha um destino que exista:/, 'the prefix is the spec literal');
  const select = wrap.children.find((c) => c.tagName === 'select');
  assert.ok(select, 'the fix is the §2.2 <select>, not a second invention');
  assert.equal(select.value, 'T9', 'the broken destination stays selected, never blanked');
  assert.deepEqual(select.children.map((o) => o.value),
    ['T1', 'T2', 'T3', 'T4', 'T9', '', '__classify', '__deny', '__fixed'],
    'the closed option set of §2.2, the missing group kept visible');

  // Read mode: the row is marked but the fix needs a save path, so the
  // control is not born there — the banner's [ Ir para a regra ] jump opens
  // the editor, and the Editar support text says editing is allowed.
  api.setMode('reading');
  api.renderSheet();
  assert.equal(findAll(dom.get('sheet'), 'step-dest-fix').length, 0,
    'no fix control in reading mode');
});

test('choosing a destination on the §3.4(a) row writes the DRAFT and opens the rule editor', () => {
  const { api, dom } = loadConsole();
  missingGroupState(api);
  api.setMode('editing');
  api.renderSheet();

  const wrap = findAll(dom.get('sheet'), 'step-dest-fix')[0];
  const select = wrap.children.find((c) => c.tagName === 'select');
  select.value = 'T2';
  select._listeners.change();

  // The read view is untouched — the staleness guard compares the file
  // against state.policy, so mutating it here would refuse every save.
  assert.equal(api.state.policy.rules[0].then.model, 'T9',
    'state.policy is never mutated by the row fix');
  // The editor opened with the fix applied to the DRAFT; the profile key the
  // row never touched survives.
  const draft = api.state.draft.rules[0].then;
  assert.deepEqual(plain({ model: draft.model, action: draft.action, deny: draft.deny, profile: draft.profile }),
    { model: 'T2', action: null, deny: null, profile: 'reviewer' },
    'the §2.2 three keys written, the untouched keys preserved');
  // The inspector shows the new destination in the SAME control.
  const dest = byLabel(dom.get('inspector'), 'Destino');
  const destSelect = dest.children.find((c) => c.tagName === 'select');
  assert.equal(destSelect.value, 'T2', 'the editor renders the fixed destination');
  // And the row still shows the file truth until the write lands.
  assert.match(flat(dom.get('sheet')), /⚠ Grupo T9 — não existe/);
});

test('while the file lints bad the Editar button carries the §3.4(a) support text', () => {
  // The DOM stub has no markup, so the literal is pinned against the file's
  // own markup — the same way seedJsonActions mirrors #jsonActions.
  const src = fs.readFileSync(sourcePath, 'utf8');
  assert.match(src,
    /id="editNote"[^>]*>Você pode editar; só não é possível salvar até o erro acima ser corrigido\.<\/span>/,
    'the support text is the spec literal, in the markup');
  const { api, dom } = loadConsole();
  missingGroupState(api);
  api.renderWarnings();
  const note = dom.get('editNote');
  assert.equal(note.hidden, false, 'the support text is present with the error');
  assert.equal(note.textContent, '', 'the stub node is a mirror; the literal lives in the markup');

  // The note rides the error set, not the mode: it clears with the errors.
  api.state.status = { validation_errors: [], error_targets: [], enabled: true };
  api.renderWarnings();
  assert.equal(dom.get('editNote').hidden, true, 'a clean file hides the note');
});

// ── §3.4(b): an attempt outside the catalogue names its two remedies ──────
// The group head's count and the row's "capacidades não verificadas" exist;
// what the spec adds is the block with the TWO controls that exist: swap the
// model (opens THIS group's editor with THAT attempt marked) or leave it
// (hides the block until the next read, writing nothing — §3.4(b)'s literal).

function unknownModelPolicy() {
  return {
    rules: [], default: {},
    tiers: {
      T1: {
        model: 'glm-4.7', provider: 'zai',
        fallback: [{ model: 'mystery-model', provider: 'xiaomi' }],
      },
    },
  };
}

test('an off-catalogue attempt warns with the two named remedies, and Deixar writes nothing and dies at the next read', async () => {
  const calls = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      calls.push(String(url).replace(/^.*\/sidecar/, ''));
      const body = url.includes('/policy')
        ? JSON.stringify(unknownModelPolicy())
        : (url.includes('/capabilities') ? JSON.stringify(catalogue('glm-4.7').data) : '{}');
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(body) });
    },
  });
  api.state.loading = false;
  api.state.policy = unknownModelPolicy();
  api.state.capabilities = api.capabilityRegistry(catalogue('glm-4.7'));
  api.setMode('editing');
  api.renderLadder();

  const said = flat(dom.get('ladder'));
  assert.match(said, /O catálogo não conhece este id: mystery-model\. Ele vai rodar/);
  const buttons = findAll(dom.get('ladder'), 'btn');
  assert.ok(buttons.some((b) => b.textContent === 'Trocar por um modelo do catálogo'),
    'the swap remedy is named');
  assert.ok(buttons.some((b) => b.textContent === 'Deixar como está'),
    'the keep remedy is named');

  const before = calls.length;
  buttons.find((b) => b.textContent === 'Deixar como está')._listeners.click();
  assert.equal(calls.length, before, 'Deixar como está emits NO request at all');
  assert.doesNotMatch(flat(dom.get('ladder')), /não conhece este id/,
    'the warning is hidden — in state, never in the file');
  assert.deepEqual(api.state.policy, plain(unknownModelPolicy()), 'the policy is untouched');

  // §3.4(b): "até a próxima leitura" — a load() brings the warning back.
  await api.load();
  assert.match(flat(dom.get('ladder')), /não conhece este id: mystery-model/,
    'the next read resurrects the warning');
});

test('Trocar por um modelo do catálogo opens the group editor with THAT attempt marked', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = unknownModelPolicy();
  api.state.capabilities = api.capabilityRegistry(catalogue('glm-4.7'));
  api.setMode('editing');
  api.renderLadder();

  const buttons = findAll(dom.get('ladder'), 'btn');
  const swap = buttons.find((b) => b.textContent === 'Trocar por um modelo do catálogo');
  assert.ok(swap, 'the swap remedy exists');
  swap._listeners.click();

  const rows = findAll(dom.get('inspector'), 'chain-row');
  assert.equal(rows.length, 2, 'the group editor holds the primary and the reserve');
  assert.equal(rows[0].classList.contains('chain-target'), false,
    'the FIRST attempt is not the one the warning named');
  assert.equal(rows[1].classList.contains('chain-target'), true,
    'the SECOND attempt — the off-catalogue one — is the marked row');
  assert.equal(api.state.tierFix, null, 'the target is one-shot, consumed by the render');
});

// ── §3.4(d): the zero-count group names the floor as the thing to lower ──
// The count and the prose already render zero; the missing piece is the real
// control: [ Baixar a exigência do grupo ] opens THIS group's min_context
// field in the draft, with the current value, and never writes by itself —
// naming the remedy is not executing it (§6.8).

test('Baixar a exigência do grupo points at the panel floor field, preloaded, and writes nothing itself', async () => {
  const posted = [];
  const policy = tierPolicy();
  policy.tiers.T3 = { requirements: { min_context: 2000000 } };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push(String(url).replace(/^.*\/sidecar/, ''));
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', context_window: 200000 },
    'gpt-5.6-luna': { provider: 'openai-codex', context_window: 1000000 },
  };
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T3', name: 'T3', bind: 'tier', tier: 'T3' });
  const box = dom.get('inspector');
  const modelWrap = byLabel(box, 'Modelo');

  const lower = modelWrap.children.find((b) => b.textContent === 'Baixar a exigência do grupo');
  assert.ok(lower, 'the remedy button exists at zero eligible');
  lower._listeners.click();
  assert.equal(posted.length, 0, 'the button itself writes NOTHING');
  // §3.4(d): the button lands on the PANEL's floor field — the one field this
  // group's minimum lives in — and that field carries the CURRENT floor.
  const floorWrap = byLabel(box, 'Exigência de contexto (mínimo de tokens)');
  assert.ok(floorWrap, 'the panel floor field exists');
  const floorInput = floorWrap.children.find((c) => c.tagName === 'input');
  assert.ok(floorInput, 'it is a real input');
  assert.ok(floorInput._scrolledTo, 'the button scrolls to the field the operator asked for');
  assert.ok(floorWrap.classList.contains('chain-target'), 'and marks it as the target');
  assert.equal(floorInput.value, '2000000', 'the field carries the CURRENT floor');

  // Lowering the floor edits the draft and the surfaces follow live.
  floorInput.value = '1000';
  floorInput._listeners.input();
  assert.equal(api.state.draft.tiers.T3.requirements.min_context, 1000,
    'the draft floor is lowered');
  assert.equal(posted.length, 0, 'typing writes nothing either');
  const note = modelWrap.children.find((c) => String(c.className).includes('field-note'));
  assert.match(note.textContent, /^2 modelos atendem/, 'the count follows the new floor');
  assert.equal(lower.hidden, true, 'the zero block clears once a model qualifies');

  // The write still goes through the minimal patch: Salvar plans, carrying
  // only the touched surface.
  const apply = findAll(dom.get('inspector'), 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  assert.equal(posted.filter((u) => u === '/plan').length, 1, 'the save plans once');
});

// ── §3.4(b)/(c)/§2.3: the unverified-capabilities fact has ONE spelling ──
// The spec writes "capacidades não verificadas"; the screen used to say it
// three ways. The mutation to catch is a second spelling surviving anywhere.

test('the unverified-capabilities fact has ONE spelling across every surface (§2.3, §3.4(b))', () => {
  const src = fs.readFileSync(sourcePath, 'utf8');
  const matches = src.match(/capacidades não verificadas/g) || [];
  assert.ok(matches.length >= 3, `the spec literal appears on every surface, got ${matches.length}`);
  assert.doesNotMatch(src, /sem capacidade verificada/, 'the old singular spelling is gone');
  assert.doesNotMatch(src, /não foram verificadas/, 'the old sentence spelling is gone');
});

// ── a11y: every dynamic field binds its label to its control ─────────────
// Each field the inspector builds is a <label> beside an <input>/<select>.
// A screen reader names the control through the for/id link, so the generic
// rule is: no input/select without an id, no label without a for, and every
// for resolves to a control actually in the tree. Walking the tree instead of
// pinning one field covers whatever the sibling cards add later — a new
// dynamic control without the pair fails here before it reaches a reviewer.

function collectFieldNodes(box) {
  const controls = [];
  const labels = [];
  const walk = (n, path) => {
    if (n.tagName === 'input' || n.tagName === 'select') controls.push({ node: n, path });
    else if (n.tagName === 'label') labels.push({ node: n, path });
    (n.children || []).forEach((c) => walk(c, `${path}/${c.tagName || '?'}`));
  };
  walk(box, box.id || 'box');
  return { controls, labels };
}

function assertEveryFieldLinked(box, context) {
  const { controls, labels } = collectFieldNodes(box);
  controls.forEach(({ node, path }) => {
    assert.ok(node.id, `${context}: ${path} is an input/select without an id`);
  });
  const ids = controls.map((c) => c.node.id);
  assert.equal(new Set(ids).size, ids.length, `${context}: field ids must be unique`);
  const idSet = new Set(ids);
  labels.forEach(({ node, path }) => {
    assert.ok(node.htmlFor, `${context}: ${path} is a label without a for`);
    assert.ok(idSet.has(node.htmlFor), `${context}: label ${path} for= points to an id not in the tree`);
  });
}

test('every field the inspector builds binds label to control — for/id in the tree', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', context_window: 200000 },
    'gpt-5.6-luna': { provider: 'openai-codex', context_window: 1000000 },
    'mimo-v2.5': { provider: 'xiaomi', context_window: 1048576 },
  };
  api.setMode('editing');
  const binds = [
    { id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' },
    { id: 'rule:r1', name: 'r1', bind: 'rule', ruleIndex: 0 },
    { id: 'classifier', name: 'classifier', bind: 'classifier' },
    { id: 'fail_safe', name: 'fail_safe', bind: 'fail_safe' },
    { id: 'default', name: 'default', bind: 'default' },
  ];
  binds.forEach((node) => {
    api.renderInspector(node);
    assertEveryFieldLinked(dom.get('inspector'), `bind=${node.bind}`);
  });
});

test('reopening the inspector mints fresh ids — two renders never share one', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.capabilities = { 'glm-4.7': { provider: 'zai', context_window: 200000 } };
  api.setMode('editing');
  const node = { id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' };
  api.renderInspector(node);
  const first = collectFieldNodes(dom.get('inspector')).controls.map((c) => c.node.id);
  api.renderInspector(node);
  const second = collectFieldNodes(dom.get('inspector')).controls.map((c) => c.node.id);
  const all = first.concat(second);
  assert.equal(new Set(all).size, all.length,
    'a re-render must mint fresh ids — two panels open together must never collide');
});

test('the escape-hatch field carries its own label once revealed', () => {
  const { api, dom } = loadConsole();
  const policy = tierPolicy();
  policy.tiers.T1.model = 'modelo-fora-do-catalogo';
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = { 'glm-4.7': { provider: 'zai', context_window: 200000 } };
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const modelWrap = byLabel(dom.get('inspector'), 'Modelo');
  const escapeBtn = modelWrap.children.find((b) => b.textContent === 'Usar um id que não está na lista');
  assert.ok(escapeBtn, 'the escape hatch button exists');
  escapeBtn._listeners.click();
  const input = modelWrap.children.find((c) => c.tagName === 'input' && c.hidden === false);
  assert.ok(input && input.id, 'the revealed field is an input with an id');
  const label = modelWrap.children.find((c) => c.tagName === 'label' && c.htmlFor === input.id);
  assert.ok(label, 'a bound label names the revealed field');
  assert.equal(label.textContent, 'Id do modelo, escrito à mão');
});

test('the "Outro papel…" text field is named when revealed', () => {
  const { api, dom } = loadConsole();
  const policy = tierPolicy();
  policy.rules = [{ id: 'r1', when: {}, then: { model: 'T2', profile: 'coder' } }];
  api.state.policy = policy;
  api.state.loading = false;
  api.setMode('editing');
  api.renderInspector({ id: 'rule:r1', name: 'r1', bind: 'rule', ruleIndex: 0 });
  const wrap = byLabel(dom.get('inspector'), 'Papel');
  const select = wrap.children.find((c) => c.tagName === 'select');
  select.value = '__other';
  select._listeners.change();
  const input = wrap.children.find((c) => c.tagName === 'input' && c.hidden === false);
  assert.ok(input && input.id, 'the text field is revealed with an id');
  const label = wrap.children.find((c) => c.tagName === 'label' && c.htmlFor === input.id);
  assert.ok(label, 'a bound label names the revealed field');
  assert.equal(label.textContent, 'Papel, escrito à mão');
});

test('the §3.4(a) row-fix select is a bound field too', () => {
  const { api, dom } = loadConsole();
  missingGroupState(api);
  api.setMode('editing');
  api.renderSheet();
  assertEveryFieldLinked(dom.get('sheet'), 'sheet destFix row');
});

// ── The two operator constraints: min_context and max_multiplier ────────
// The presets own the three STRATEGY keys; these two are restrictions the
// operator sets on their own fleet (§3.4(d), §5.4). The group panel carries
// both fields, preloaded with the effective value, writing the shape the
// engine reads — and the /plan body carries only the touched keys.

test('the cap field preloads the effective value and writes {max_multiplier: n}, never the loose number', async () => {
  const posted = [];
  const policy = tierPolicy();
  policy.tiers.T2.time_cap = { max_multiplier: 1.5 };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');

  const capWrap = byLabel(box, 'Teto de preço (multiplicador máximo)');
  assert.ok(capWrap, 'the cap field exists in the group panel');
  const capInput = capWrap.children.find((c) => c.tagName === 'input');
  assert.equal(capInput.value, '1.5', 'the effective value is preloaded');

  capInput.value = '2.5';
  capInput._listeners.input();
  assert.deepEqual(plain(api.state.draft.tiers.T2.time_cap), { max_multiplier: 2.5 },
    'the draft carries the MAP form — the console cannot write the loose number');

  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.deepEqual(plain(planCall.body.policy), { tiers: { T2: { time_cap: { max_multiplier: 2.5 } } } },
    'the body is the minimal fragment: only time_cap');
  assert.deepEqual(Object.keys(planCall.body.policy.tiers.T2), ['time_cap'],
    'and no other key rides along');
});

test('clearing the cap field writes null — absence, never a ceiling of 0', async () => {
  const posted = [];
  const policy = tierPolicy();
  policy.tiers.T2.time_cap = { max_multiplier: 1.5 };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const capInput = byLabel(box, 'Teto de preço (multiplicador máximo)').children.find((c) => c.tagName === 'input');

  // A zero is not a ceiling: it would exclude every option at every hour.
  capInput.value = '0';
  capInput._listeners.input();
  assert.equal(api.state.draft.tiers.T2.time_cap, null, '0 is absence, not a zero cap');

  capInput.value = '';
  capInput._listeners.input();
  assert.equal(api.state.draft.tiers.T2.time_cap, null, 'an empty field is absence');

  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.equal(planCall.body.policy.tiers.T2.time_cap, null,
    'the body carries the explicit null the server reads as "remove the key"');
  assert.deepEqual(Object.keys(planCall.body.policy.tiers.T2), ['time_cap']);
  // Once the server removes the key the Ordem line reads the engine default.
  const merged = Object.assign({}, policy.tiers.T2, { time_cap: undefined });
  assert.match(api.orderLineParts(merged)[2], /sem teto de preço.*padrão do motor/,
    'absence reads as the engine default, not as a ceiling');
});

test('a loose time_cap: 1.5 keeps the §5.4 line and is rewritten in the map form when the operator saves', async () => {
  const posted = [];
  const policy = tierPolicy();
  policy.tiers.T2.time_cap = 1.5; // the form §5.4 names — legal YAML, refused by the lint
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  // The ladder keeps saying the §5.4 thing while the file is unsaved.
  api.renderLadder();
  assert.match(flat(dom.get('ladder')), /formato que o roteador não lê/,
    'the §5.4 text still names the loose form on the Ordem line');

  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const capInput = byLabel(box, 'Teto de preço (multiplicador máximo)').children.find((c) => c.tagName === 'input');
  assert.equal(capInput.value, '1.5', 'the loose number still has its value preloaded');

  // The operator saves the same value: the field round-trips it as the map.
  capInput._listeners.input();
  assert.deepEqual(plain(api.state.draft.tiers.T2.time_cap), { max_multiplier: 1.5 },
    'touching the field rewrites the loose number in the form the engine reads');
  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.deepEqual(plain(planCall.body.policy.tiers.T2.time_cap), { max_multiplier: 1.5 },
    'the saved cap is the map form, never the loose number');
});

test('the floor field preloads the effective min_context and the picker count follows it live, zero included', () => {
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
  const box = dom.get('inspector');
  const floorWrap = byLabel(box, 'Exigência de contexto (mínimo de tokens)');
  assert.ok(floorWrap, 'the floor field exists in the group panel');
  const floorInput = floorWrap.children.find((c) => c.tagName === 'input');
  assert.equal(floorInput.value, '200000', 'the effective floor is preloaded');

  const modelWrap = byLabel(box, 'Modelo');
  let note = modelWrap.children.find((c) => String(c.className).includes('field-note'));
  assert.match(note.textContent, /^2 modelos atendem à exigência deste grupo \(≥ 200,000 tokens\)/,
    'the picker count states the floor it applied');

  // A floor above every window: zero is a rendered count, not a silent list.
  floorInput.value = '2000000';
  floorInput._listeners.input();
  assert.equal(api.state.draft.tiers.T3.requirements.min_context, 2000000, 'the draft floor rises');
  note = modelWrap.children.find((c) => String(c.className).includes('field-note'));
  assert.match(note.textContent, /^0 modelos atendem à exigência deste grupo/,
    'the count follows the operator number on the spot — Padrão 3');
  assert.match(flat(modelWrap), /Nenhum modelo do seu catálogo declara 2,000,000 tokens ou mais/,
    'the zero block names the floor as the thing to lower');

  // And down again: the count is a live function of the field, not a snapshot.
  floorInput.value = '1000';
  floorInput._listeners.input();
  note = modelWrap.children.find((c) => String(c.className).includes('field-note'));
  assert.match(note.textContent, /^2 modelos atendem/, 'lowering brings the count back');
});

test('clearing the floor sends requirements: null, so the group reads (padrão do motor) again', async () => {
  const posted = [];
  const policy = tierPolicy();
  policy.tiers.T3 = { requirements: { min_context: 200000 } };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T3', name: 'T3', bind: 'tier', tier: 'T3' });
  const box = dom.get('inspector');
  const floorInput = byLabel(box, 'Exigência de contexto (mínimo de tokens)').children.find((c) => c.tagName === 'input');

  floorInput.value = '';
  floorInput._listeners.input();
  assert.equal(api.state.draft.tiers.T3.requirements.min_context, null,
    'an empty field is "no floor" — never a floor of 0');

  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.equal(planCall.body.policy.tiers.T3.requirements, null,
    'the emptied floor leaves as a KEY-LEVEL null — an empty mapping would read as "declared without conditions"');
  assert.deepEqual(Object.keys(planCall.body.policy.tiers.T3), ['requirements'],
    'only the floor rides the patch');
  // The server pops the key; the Ordem line then reads the engine default.
  const merged = Object.assign({}, policy.tiers.T3, { requirements: undefined });
  assert.match(api.orderLineParts(merged)[4], /sem exigência de contexto.*padrão do motor/);
});

test('clearing the floor beside OTHER requirement keys drops only min_context', async () => {
  const posted = [];
  const policy = tierPolicy();
  policy.tiers.T3 = { requirements: { min_context: 200000, vision: true } };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T3', name: 'T3', bind: 'tier', tier: 'T3' });
  const box = dom.get('inspector');
  const floorInput = byLabel(box, 'Exigência de contexto (mínimo de tokens)').children.find((c) => c.tagName === 'input');
  floorInput.value = '';
  floorInput._listeners.input();
  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.deepEqual(plain(planCall.body.policy.tiers.T3.requirements), { min_context: null, vision: true },
    'only the nulled key is sent — the sibling requirement survives the merge');
});

test('the preset label beside the fields follows the draft: Personalizado when the edit leaves the preset, back when it matches again', () => {
  const { api, dom } = loadConsole();
  const policy = tierPolicy();
  // A group that matches Economizar exactly: cheapest now, the first option
  // free to move (metered), ceiling at 1.5. requirements and time_policy are
  // FREE in that preset's patch, so a floor edit must NOT move the label.
  policy.tiers.T2.fallback_strategy = 'cheapest_now';
  policy.tiers.T2.pin_primary = false;
  policy.tiers.T2.billing_mode = 'metered';
  policy.tiers.T2.time_cap = { max_multiplier: 1.5 };
  api.state.policy = policy;
  api.state.loading = false;
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const noteOf = () => box.children.find((c) => String(c.className).includes('field-note')
    && /casa com o preset|Personalizado/.test(c.textContent || ''));
  assert.ok(noteOf(), 'the panel carries the preset line');
  assert.match(noteOf().textContent, /casa com o preset Economizar/,
    'a group matching Economizar says so, next to the fields');

  // Editing the FLOOR does not move the label: requirements is free in the
  // Economizar patch — the edit still "casa com um preset".
  const floorInput = byLabel(box, 'Exigência de contexto (mínimo de tokens)').children.find((c) => c.tagName === 'input');
  floorInput.value = '500000';
  floorInput._listeners.input();
  assert.match(noteOf().textContent, /casa com o preset Economizar/,
    'a floor edit inside the preset keeps the label');

  // Editing the CAP out of the preset flips it on the spot.
  const capInput = byLabel(box, 'Teto de preço (multiplicador máximo)').children.find((c) => c.tagName === 'input');
  capInput.value = '2.5';
  capInput._listeners.input();
  assert.match(noteOf().textContent, /Personalizado/,
    'a ceiling outside every preset reads Personalizado beside the field, before any save');

  // And back: the label is a live function of the draft, not a latch.
  capInput.value = '1.5';
  capInput._listeners.input();
  assert.match(noteOf().textContent, /casa com o preset Economizar/,
    'restoring the preset value restores the label');
});

test('editing floor and cap sends ONLY the two constraint keys in the plan body', async () => {
  const posted = [];
  const policy = tierPolicy();
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const floorInput = byLabel(box, 'Exigência de contexto (mínimo de tokens)').children.find((c) => c.tagName === 'input');
  floorInput.value = '400000';
  floorInput._listeners.input();
  const capInput = byLabel(box, 'Teto de preço (multiplicador máximo)').children.find((c) => c.tagName === 'input');
  capInput.value = '2';
  capInput._listeners.input();

  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  assert.deepEqual(Object.keys(planCall.body.policy.tiers.T2).sort(), ['requirements', 'time_cap'],
    'the two operator constraints, and nothing else');
  assert.deepEqual(plain(planCall.body.policy.tiers.T2.requirements), { min_context: 400000 });
  assert.deepEqual(plain(planCall.body.policy.tiers.T2.time_cap), { max_multiplier: 2 });
  // The three strategy keys stay the presets' territory — a field beside them
  // must not smuggle them back into the body.
  assert.ok(!('fallback_strategy' in planCall.body.policy.tiers.T2), 'no fallback_strategy leaks');
  assert.ok(!('pin_primary' in planCall.body.policy.tiers.T2), 'no pin_primary leaks');
  assert.ok(!('time_policy' in planCall.body.policy.tiers.T2), 'no time_policy leaks');
});

test('editing the cap reveals the preset Economizar pointer — the one authority on what a cap does', () => {
  const { api, dom } = loadConsole();
  api.state.policy = tierPolicy();
  api.state.loading = false;
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  const box = dom.get('inspector');
  const capWrap = byLabel(box, 'Teto de preço (multiplicador máximo)');
  const capInput = capWrap.children.find((c) => c.tagName === 'input');
  const note = capWrap.children.find((c) => String(c.className).includes('field-note'));
  assert.ok(note, 'the cap carries a note');
  assert.equal(note.hidden, true, 'the note stays quiet until the operator touches the cap');
  capInput.value = '2';
  capInput._listeners.input();
  assert.equal(note.hidden, false, 'the edit reveals the pointer');
  assert.match(note.textContent, /descrição do preset Economizar/,
    'it points at the existing authority — no second explanation of what a cap does');
});

test('tierPresetOf is the per-group reader: the applied preset matches that group, a hand-written one does not', () => {
  const { api } = loadConsole();
  const policy = presetPolicy();
  assert.equal(api.tierPresetOf(policy.tiers.T1, 'T1', policy), null,
    'a hand-written group matches no preset');
  const next = JSON.parse(JSON.stringify(policy));
  const patch = plain(api.presetPatch('economizar', policy)).tiers;
  Object.keys(patch).forEach((name) => Object.assign(next.tiers[name], patch[name]));
  assert.equal(api.tierPresetOf(next.tiers.T1, 'T1', next).key, 'economizar',
    'a group carrying the preset patch matches that preset');
  const factory = JSON.parse(JSON.stringify(policy));
  const eq = plain(api.presetPatch('equilibrio', policy)).tiers;
  Object.keys(eq).forEach((name) => Object.assign(factory.tiers[name], eq[name]));
  assert.equal(api.tierPresetOf(factory.tiers.T1, 'T1', factory).key, 'equilibrio',
    'a factory group matches Equilíbrio — A→B→C order resolves the tie');
});
// ── the move carries the WHOLE attempt, not three fields ─────────────
// A reserve may declare `declared` (router/capabilities.py:23 — per-elo
// overrides that WIN over the registry). Reordering or removing used to
// swap only {model, provider, billing_mode} and leave the extra keys
// behind, so an override started describing the neighbour's model. These
// tests pin the set semantics: every key rides with its attempt, the
// strategy keys never leave the tier, and a removed key leaves as an
// explicit null (§2.1).

test('↓ on the primary and ↑ on the first reserve: the declared override follows its model both ways', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  policy.tiers.T1.declared = { context_window: 123456 };
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  let rows = findAll(box, 'chain-row');
  rowButtons(rows[0]).children.find((b) => b.textContent === '↓')._listeners.click();

  let draft = api.state.draft.tiers.T1;
  assert.equal(draft.model, 'gpt-5.6-luna', 'the first reserve is promoted');
  assert.equal(draft.declared, null,
    'the promoted attempt carries no declared — the explicit null says the old override left the tier (§2.1)');
  assert.equal(draft.fallback[0].model, 'glm-4.7', 'the old primary is now the first reserve');
  assert.deepEqual(plain(draft.fallback[0].declared), { context_window: 123456 },
    'the override rode DOWN with its model into the list');

  rows = findAll(box, 'chain-row');
  rowButtons(rows[1]).children.find((b) => b.textContent === '↑')._listeners.click();
  draft = api.state.draft.tiers.T1;
  assert.equal(draft.model, 'glm-4.7', 'the first reserve is promoted back');
  assert.deepEqual(plain(draft.declared), { context_window: 123456 },
    'the override rode UP with its model onto the tier');
  assert.equal(draft.fallback[0].model, 'gpt-5.6-luna');
  assert.equal(draft.fallback[0].declared, null, 'the demoted attempt now carries no override');
  assert.equal(draft.fallback[0].billing_mode, 'subscription',
    'and the whole attempt round-tripped, billing mode included');
});

test('a promoted attempt with no billing mode leaves an explicit null on the tier, never a silent stale mode', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  delete policy.tiers.T1.fallback[0].billing_mode;
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  rowButtons(rows[0]).children.find((b) => b.textContent === '↓')._listeners.click();
  const draft = api.state.draft.tiers.T1;
  assert.equal(draft.billing_mode, null,
    'the swap SAYS the mode was removed (§2.1) — not undefined, which the patch would drop silently');
  assert.equal(draft.fallback[0].billing_mode, 'plan', 'the old primary took its own mode down with it');
});

test('↑/↓ between two reserves move the whole attempt: the declared override follows its model either way', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  policy.tiers.T1.fallback[0].declared = { context_window: 123456 };
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  let rows = findAll(box, 'chain-row');
  rowButtons(rows[1]).children.find((b) => b.textContent === '↓')._listeners.click();
  let fb = api.state.draft.tiers.T1.fallback;
  assert.equal(fb[0].model, 'mimo-v2.5', 'the second reserve came up');
  assert.ok(!('declared' in fb[0]), 'a reserve without an override gained none');
  assert.equal(fb[1].model, 'gpt-5.6-luna', 'the first reserve went down');
  assert.deepEqual(plain(fb[1].declared), { context_window: 123456 },
    'the override rode DOWN one position with its model');

  rows = findAll(box, 'chain-row');
  rowButtons(rows[2]).children.find((b) => b.textContent === '↑')._listeners.click();
  fb = api.state.draft.tiers.T1.fallback;
  assert.equal(fb[0].model, 'gpt-5.6-luna', 'the first reserve is back up');
  assert.deepEqual(plain(fb[0].declared), { context_window: 123456 },
    'the override rode UP with its model');
  assert.ok(!('declared' in fb[1]), 'the other reserve is clean again');
});

test('Remover on the primary promotes the first reserve WITH its declared override', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  policy.tiers.T1.fallback[0].declared = { context_window: 123456 };
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  rowButtons(rows[0]).children.find((b) => b.textContent === 'Remover')._listeners.click();
  const draft = api.state.draft.tiers.T1;
  assert.equal(draft.model, 'gpt-5.6-luna', 'the first reserve becomes the primary');
  assert.deepEqual(plain(draft.declared), { context_window: 123456 },
    'the promoted reserve KEEPS its declared — nothing is discarded on the way up');
  assert.deepEqual(draft.fallback.map((e) => e.model), ['mimo-v2.5'],
    'the removed attempt left the queue, override and all');
  assert.ok(!('declared' in draft.fallback[0]), 'the remaining reserve never gained an override');
});

test('the strategy keys stay on the tier through every move — they never travel into fallback', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  const t1 = policy.tiers.T1;
  t1.pin_primary = true;
  t1.time_cap = { max_multiplier: 2 };
  t1.time_policy = { avoid_peak: ['zai'] };
  t1.requirements = { min_context: 900000 };
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  const strategyKeys = ['fallback', 'fallback_strategy', 'pin_primary', 'time_cap', 'time_policy', 'requirements'];
  const assertStrategy = (label) => {
    const t = api.state.draft.tiers.T1;
    assert.equal(t.fallback_strategy, 'sequential', `${label}: fallback_strategy stays on the tier`);
    assert.equal(t.pin_primary, true, `${label}: pin_primary stays on the tier`);
    assert.deepEqual(plain(t.time_cap), { max_multiplier: 2 }, `${label}: time_cap stays on the tier`);
    assert.deepEqual(plain(t.time_policy), { avoid_peak: ['zai'] }, `${label}: time_policy stays on the tier`);
    assert.deepEqual(plain(t.requirements), { min_context: 900000 }, `${label}: requirements stays on the tier`);
    t.fallback.forEach((e, i) => {
      strategyKeys.forEach((k) => {
        assert.ok(!Object.prototype.hasOwnProperty.call(e, k),
          `${label}: fallback[${i}] carries no '${k}'`);
      });
    });
  };
  let rows = findAll(box, 'chain-row');
  rowButtons(rows[0]).children.find((b) => b.textContent === '↓')._listeners.click();
  assertStrategy('after ↓ on the primary');
  rows = findAll(box, 'chain-row');
  rowButtons(rows[1]).children.find((b) => b.textContent === '↑')._listeners.click();
  assertStrategy('after ↑ on the first reserve');
  rows = findAll(box, 'chain-row');
  rowButtons(rows[0]).children.find((b) => b.textContent === 'Remover')._listeners.click();
  assertStrategy('after Remover on the primary');
});

test('the /plan body after a move carries the queue with each override on its own model', async () => {
  const posted = [];
  const policy = tierPolicy();
  policy.tiers.T1.declared = { context_window: 111111 };
  policy.tiers.T1.fallback[0].declared = { context_window: 123456 };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (opts && opts.body) posted.push({ url: String(url).replace(/^.*\/sidecar/, ''), body: JSON.parse(opts.body) });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: '+x', base_hash: 'h' })) });
    },
  });
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  rowButtons(rows[0]).children.find((b) => b.textContent === '↓')._listeners.click();

  const apply = findAll(box, 'btn').find((b) => b.textContent === 'Salvar');
  apply._listeners.click();
  await tick();
  const planCall = posted.find((c) => c.url === '/plan');
  assert.ok(planCall, 'an /plan went out');
  const tier = planCall.body.policy.tiers.T1;
  assert.equal(tier.model, 'gpt-5.6-luna', 'the promoted reserve is the written primary');
  assert.deepEqual(tier.declared, { context_window: 123456 },
    "the promoted reserve's override rides the tier patch — never the stale primary's");
  assert.equal(tier.fallback[0].model, 'glm-4.7', 'the old primary is the first written reserve');
  assert.deepEqual(tier.fallback[0].declared, { context_window: 111111 },
    "the old primary's override followed it into the list — each override sits on its own model");
  assert.ok(!('declared' in tier.fallback[1]), 'the untouched reserve carries no override');
});

test('a reserve-reserve swap says a missing billing mode out loud, on the attempt that lacks it', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  const policy = tierPolicy();
  delete policy.tiers.T1.fallback[0].billing_mode;
  api.state.policy = policy;
  api.state.loading = false;
  api.state.capabilities = capModels();
  api.setMode('editing');
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });
  const box = dom.get('inspector');
  const rows = findAll(box, 'chain-row');
  rowButtons(rows[1]).children.find((b) => b.textContent === '↓')._listeners.click();
  const fb = api.state.draft.tiers.T1.fallback;
  assert.equal(fb[0].model, 'mimo-v2.5', 'the second reserve came up');
  assert.equal(fb[0].billing_mode, 'metered', 'the mode rode down with its own attempt');
  assert.equal(fb[1].model, 'gpt-5.6-luna', 'the first reserve went down');
  assert.equal(fb[1].billing_mode, null,
    'the attempt that has no mode says so explicitly (§2.1) — the list replaces wholesale, so an absent key would write nothing');
});
// ── §2.6 / §3.3: Fora de rotação — desbanir pela tela e a fila que substitui ──
// The block used to draw bans with no way to lift them, and never drew the
// substitute queue. The removal is a write like any other (plan → diff → apply
// with the whole list minus the item), gated on the same editing mode and the
// same staleness read — with one difference: GET /policy does not project
// blocklist, so the guard re-reads the /blocklist the screen already reads.

test('only a manual ban offers removal, and only in editing mode', () => {
  const { api, dom } = loadConsole();
  api.state.policy = {};
  api.state.blocklist = {
    manual_bans: [{ model: 'glm-5.3' }],
    breaker_cooldowns: [{ model_key: 'deepseek-v4-pro', cooldown_remaining_s: 300 }],
    fallback_chain: [],
  };
  api.renderHealth();
  assert.equal(findAll(dom.get('bans'), 'btn').length, 0,
    'reading mode: no removal control exists in the DOM at all — not disabled, absent');
  assert.match(flat(dom.get('bans')), /banido/, 'a manual ban is named with the pt-BR state word');
  assert.match(flat(dom.get('bans')), /faltam 300s/, 'a breaker cooldown says the time owed in pt-BR, unit included');
  api.setMode('editing');
  const buttons = findAll(dom.get('bans'), 'btn');
  assert.equal(buttons.length, 1, 'editing mode: the manual ban row grows the control');
  assert.equal(buttons[0].textContent, 'Remover o bloqueio');
  const breakerRow = dom.get('bans').children[1];
  assert.equal(findAll(breakerRow, 'btn').length, 0,
    'a breaker cooldown is not removable by hand: it expires on its own');
  api.setMode('reading');
  assert.equal(findAll(dom.get('bans'), 'btn').length, 0,
    'leaving editing mode removes the control from the DOM');
});

test('removing a ban plans the whole list without it, and nothing else', async () => {
  const planBodies = [];
  const server = {
    blocklist: {
      manual_bans: [{ model: 'glm-5.3' }, { model: 'deepseek-v4-pro' }],
      fallback_chain: ['gpt-5.6-terra'],
      breaker_enabled: true,
      breaker_cooldowns: [],
    },
  };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (url.endsWith('/blocklist')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(server.blocklist)) });
      }
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      if (url.endsWith('/plan')) {
        const policy = JSON.parse(opts.body).policy;
        planBodies.push(policy);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy, diff: '- glm-5.3', base_hash: 'h' })) });
      }
      if (url.endsWith('/apply')) {
        server.blocklist = Object.assign({}, server.blocklist, { manual_bans: [{ model: 'deepseek-v4-pro' }] });
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.policy = {};
  api.state.blocklist = server.blocklist;
  // The markup ships the message line hidden; the fake DOM cannot read the
  // attribute, so the test arms the state the markup declares.
  dom.get('bansMsg').hidden = true;
  api.setMode('editing');
  findAll(dom.get('bans'), 'btn')[0]._listeners.click();
  await tick();

  assert.equal(planBodies.length, 1);
  assert.deepEqual(planBodies[0], { blocklist: { manual_ban: [{ model: 'deepseek-v4-pro' }] } },
    'the plan body is the whole list WITHOUT the lifted item, and no other top-level key');
  assert.match(dom.get('bansMsg').textContent, /Vale para as próximas tarefas/,
    '§2.7: a written save says the temporal scope');
  assert.equal(dom.get('bansMsg').hidden, false, 'the message line stops hiding once a write speaks');
  assert.equal(findAll(dom.get('bans'), 'btn').length, 1,
    'after the reload one ban remains, still with its own control');
});

test('removing a ban refuses when the blocklist moved since the screen read it', async () => {
  const calls = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      calls.push(url);
      if (url.endsWith('/blocklist')) {
        // The server now bans a model this screen has never seen.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({
          manual_bans: [{ model: 'glm-5.3' }, { model: 'gpt-5.6-terra' }],
          fallback_chain: [], breaker_enabled: true, breaker_cooldowns: [],
        })) });
      }
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.policy = {};
  api.state.blocklist = {
    manual_bans: [{ model: 'glm-5.3' }],
    fallback_chain: [], breaker_enabled: true, breaker_cooldowns: [],
  };
  api.setMode('editing');
  findAll(dom.get('bans'), 'btn')[0]._listeners.click();
  await tick();

  assert.equal(dom.get('bansMsg').textContent,
    'O arquivo mudou por fora desde que esta tela leu: mudou em algo que esta tela não sabe nomear. Recarreguei tudo; confira e tente de novo.',
    '§4.7: the blocklist drift refuses with the "cannot name" clause — blocklist is not one of the four named projections');
  assert.match(dom.get('bansMsg').className, /bad/);
  assert.equal(calls.filter((u) => u.endsWith('/blocklist')).length, 2,
    'the guard re-read the blocklist, and the refusal reloaded the screen');
  assert.equal(calls.filter((u) => u.endsWith('/plan')).length, 0,
    'a stale write never reaches /plan');
  const text = flat(dom.get('bans'));
  assert.match(text, /gpt-5\.6-terra/, 'the reload drew the drifted data, not the stale snapshot');
});

test('removing a ban without a session token says the gesture, not "salvar"', () => {
  const { api, dom } = loadConsole({ csrfToken: '' });
  api.state.policy = {};
  api.state.blocklist = { manual_bans: [{ model: 'glm-5.3' }], breaker_cooldowns: [], fallback_chain: [] };
  api.setMode('editing');
  findAll(dom.get('bans'), 'btn')[0]._listeners.click();
  assert.match(dom.get('bansMsg').textContent, /Não é possível remover o bloqueio/,
    'the refusal names the button\'s own gesture');
  assert.match(dom.get('bansMsg').textContent, /Hermes One/);
});

test('the substitute queue renders in the block with its own §2.6 word', () => {
  const { api, dom } = loadConsole();
  api.state.policy = {};
  api.state.blocklist = {
    manual_bans: [{ model: 'glm-5.3' }],
    breaker_cooldowns: [],
    fallback_chain: ['deepseek-v4-flash', 'glm-5.2'],
  };
  api.renderHealth();
  const text = flat(dom.get('bans'));
  assert.match(text, /substituto da lista de reserva geral/i, 'the queue is drawn with the §2.6 word');
  assert.match(text, /deepseek-v4-flash/);
  assert.match(text, /glm-5\.2/);
  // The §2.6 word names THIS queue and no other concept on the screen.
  const code = stripCommentsForCounting(fs.readFileSync(sourcePath, 'utf8'));
  assert.equal(code.split('Substituto da lista de reserva geral').length - 1, 1,
    'the word lives once — the substitute queue\'s label, not padrão do motor / reserva / último recurso');
});

test('an empty substitute queue renders no frame and no empty phrase', () => {
  const { api, dom } = loadConsole();
  api.state.policy = {};
  api.state.blocklist = { manual_bans: [{ model: 'glm-5.3' }], breaker_cooldowns: [], fallback_chain: [] };
  api.renderHealth();
  assert.equal(findAll(dom.get('bans'), 'chain-head').length, 0);
  assert.doesNotMatch(flat(dom.get('bans')), /substituto/);
  // An absent key behaves like an empty list.
  api.state.blocklist = { manual_bans: [{ model: 'glm-5.3' }], breaker_cooldowns: [] };
  api.renderHealth();
  assert.equal(findAll(dom.get('bans'), 'chain-head').length, 0);
});

test('the unban button says the gesture in flight and rests after the write (§3.3)', async () => {
  // The mocks that resolve on the spot cannot pin the in-flight label — the
  // write is over by the first tick. A gate the test releases keeps the write
  // airborne long enough to read the button mid-flight.
  let releaseWrite;
  const gate = new Promise((resolve) => { releaseWrite = resolve; });
  const server = {
    blocklist: {
      manual_bans: [{ model: 'glm-5.3' }],
      fallback_chain: [],
      breaker_enabled: true,
      breaker_cooldowns: [],
    },
  };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (url.endsWith('/blocklist')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(server.blocklist)) });
      }
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      if (url.endsWith('/plan')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: JSON.parse(opts.body).policy, diff: '- glm-5.3', base_hash: 'h' })) });
      }
      if (url.endsWith('/apply')) {
        return gate.then(() => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) }));
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.policy = {};
  api.state.blocklist = server.blocklist;
  api.setMode('editing');
  const btn = findAll(dom.get('bans'), 'btn')[0];
  btn._listeners.click();
  await tick();
  assert.equal(btn.textContent, 'Removendo o bloqueio…',
    '§3.3: in flight the unban button says the gesture, not the generic Salvando…');
  releaseWrite();
  await tick();
  assert.equal(btn.textContent, 'Remover o bloqueio',
    'and rests as the §3.3 gesture once the write is over');
});

test('with nobody banned the substitute queue is not mounted (§2.6)', () => {
  const { api, dom } = loadConsole();
  api.state.policy = {};
  api.state.blocklist = {
    manual_bans: [],
    breaker_cooldowns: [],
    fallback_chain: ['deepseek-v4-flash', 'glm-5.2'],
  };
  api.renderHealth();
  assert.equal(dom.get('bansGroup').hidden, true,
    'zero bans and zero cooldowns: the block itself is hidden');
  assert.equal(findAll(dom.get('bans'), 'chain-head').length, 0,
    'the queue is not mounted while its block is off screen (DESIGN.md rule 1)');
  assert.equal(findAll(dom.get('bans'), 'hop').length, 0, 'and no substitute line either');
  assert.doesNotMatch(flat(dom.get('bans')), /deepseek-v4-flash/,
    'the substitute models stay off the screen');
});
// ── the decision phrase names the mechanism only where the log proves it ──
// Card t_9388289e: "T2 → glm-5.3" read the same whether the group had a
// fail-safe behind it or not, and a decision the fail-safe SERVED never said
// so. The log records the decision's cause and the rule that made it, and the
// policy says whether a fail-safe exists — no more. Two clauses, only two,
// inside the row's phrase: "atendido pelo último recurso (fail-safe)" when
// the cause is the fail-safe the screen already resolves, and "este grupo não
// tem último recurso (fail-safe)" when the decision went through a group the
// policy backs with nothing. Everything else is silence: attempt outcomes are
// not recorded (backlog t_1c6a002d), so "reserva não acionada" and
// "substituição na 2ª tentativa" are never promised on this screen.

// A policy whose rule routes to a real group, with or without a fail-safe
// behind it — the two worlds the card says must read differently.
function groupPolicy(withFailSafe) {
  const policy = {
    rules: [{ id: 'hard', then: { model: 'T2' } }],
    default: {},
    tiers: { T2: { model: 'glm-5.3', provider: 'zai' } },
  };
  if (withFailSafe) policy.fail_safe = { model: 'glm-4.7', provider: 'zai' };
  return policy;
}

test('a decision the fail-safe served names the mechanism', () => {
  const { api } = loadConsole();
  const clause = api.decisionMechanismClause(
    { id: 'r1', cause: 'fail_safe_strong', rule_id: 'hard', model: 'glm-4.7' },
    groupPolicy(false));
  assert.equal(clause.word, 'atendido pelo último recurso (fail-safe)');
  assert.equal(clause.cls, 'bad', 'a serve by the last resort is a bad-news fact');
});

test('a veto is a refusal, never a fail-safe catch', () => {
  const { api } = loadConsole();
  assert.equal(
    api.decisionMechanismClause({ id: 'r1', cause: 'blocklist_veto', model: '' }, groupPolicy(false)),
    null, 'nothing served a refusal, so no clause may claim a serve');
  assert.equal(
    api.decisionMechanismClause({ id: 'r1', cause: 'selection_vetoed', model: '' }, groupPolicy(false)),
    null);
});

test('a group with no fail-safe behind it says so', () => {
  const { api } = loadConsole();
  const clause = api.decisionMechanismClause(
    { id: 'r1', cause: 'hard_rule', rule_id: 'hard', model: 'glm-5.3', provider: 'zai' },
    groupPolicy(false));
  assert.equal(clause.word, 'este grupo não tem último recurso (fail-safe)');
  assert.equal(clause.cls, 'info');
});

test('a group backed by a fail-safe stays silent on a normal decision', () => {
  const { api } = loadConsole();
  assert.equal(
    api.decisionMechanismClause(
      { id: 'r1', cause: 'hard_rule', rule_id: 'hard', model: 'glm-5.3', provider: 'zai' },
      groupPolicy(true)),
    null, 'a group with a last resort behind it and a normal decision carries no clause');
});

test('a decision whose group cannot be determined carries no clause', () => {
  const { api } = loadConsole();
  // Rule targets a fixed model, not a group.
  const fixed = { rules: [{ id: 'hard', then: { model: 'glm-5.3', provider: 'zai' } }], default: {}, tiers: { T2: {} } };
  assert.equal(
    api.decisionMechanismClause({ id: 'r1', cause: 'hard_rule', rule_id: 'hard' }, fixed),
    null, 'a fixed-model destination is not a group, so no group can lack a fail-safe');
  // Rule id not in the policy on screen.
  assert.equal(
    api.decisionMechanismClause({ id: 'r1', cause: 'hard_rule', rule_id: 'ghost' }, groupPolicy(false)),
    null, 'a rule the policy on screen does not have names no group');
  // Classifier-bound decision: the group is chosen at runtime.
  const classify = { rules: [{ id: 'rev', then: { action: 'classify' } }], default: {}, tiers: { T2: {} } };
  assert.equal(
    api.decisionMechanismClause({ id: 'r1', cause: 'classifier', rule_id: 'rev' }, classify),
    null);
});

test('an unknown cause invents no clause', () => {
  const { api } = loadConsole();
  // The row renders an empty cause as "—": the screen knows nothing to attest.
  assert.equal(
    api.decisionMechanismClause({ id: 'r1', cause: '', rule_id: 'hard', model: 'glm-5.3' }, groupPolicy(false)),
    null);
  assert.equal(
    api.decisionMechanismClause({ id: 'r1', cause: null, rule_id: 'hard', model: 'glm-5.3' }, groupPolicy(false)),
    null);
});

test('a cause the screen has not learned invents no clause', () => {
  const { api } = loadConsole();
  // 'size_rule' is a real decision-log cause, but the screen's vocabulary
  // never resolves it — the phrase must not speak for decisions it cannot
  // read, even where the group is determinable and the policy has no
  // fail-safe (the "nenhuma cláusula inventada" fixture of the card).
  assert.equal(
    api.decisionMechanismClause({ id: 'r1', cause: 'size_rule', rule_id: 'hard', model: 'glm-5.3' }, groupPolicy(false)),
    null);
});

test('the decision row renders the mechanism clause in words', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = groupPolicy(true);
  api.state.routes = [{ id: 'r1', cause: 'fail_safe_strong', model: 'glm-4.7', provider: 'zai', task: 't', ts: 1 }];
  api.renderRoutes();
  const row = dom.get('routesTable').children[0];
  assert.match(flat(row), /atendido pelo último recurso \(fail-safe\)/,
    'the word is on the row, so colour is never the only channel');
});

test('a row says the group has no last resort only when it has none', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.routes = [{ id: 'r1', cause: 'hard_rule', rule_id: 'hard', model: 'glm-5.3', provider: 'zai', task: 't', ts: 1 }];

  api.state.policy = groupPolicy(false);
  api.renderRoutes();
  assert.match(flat(dom.get('routesTable')), /este grupo não tem último recurso \(fail-safe\)/);

  api.state.policy = groupPolicy(true);
  api.renderRoutes();
  assert.doesNotMatch(flat(dom.get('routesTable')), /último recurso/,
    'a group with a fail-safe behind it says nothing on a normal decision');

  api.state.policy = groupPolicy(false);
  api.state.routes = [{ id: 'r1', cause: '', model: 'glm-5.3', provider: 'zai', task: 't', ts: 1 }];
  api.renderRoutes();
  assert.doesNotMatch(flat(dom.get('routesTable')), /último recurso/,
    'an unknown cause invents no clause even where the group lacks a fail-safe');
});

test('the decision phrase never promises attempt data the log does not record', () => {
  // Backlog card t_1c6a002d measured zero per-attempt fields in routes.jsonl;
  // the screen must not promise what the log cannot attest.
  const html = fs.readFileSync(sourcePath, 'utf8');
  for (const banned of ['reserva não acionada', 'substituição na', 'classe da tarefa']) {
    assert.ok(!html.includes(banned), `'${banned}' is backlog, and this file must not promise it`);
  }
});

// ── the elo dating: when the router LAST tried this model (t_0a3cff85) ─────
// The log records the head the executor dispatched, never an outcome, so the
// fact is "foi tentado", not "atendeu" — and a model with no decision gets NO
// line at all: an absent record is not absence of use, so "nunca" is banned.

test('lastTriedAt is the newest decision that tried the elo, by model@provider', () => {
  const { api } = loadConsole();
  const routes = [
    { ts: 1000, model: 'glm-4.7', provider: 'zai', cause: 'hard_rule' },
    { ts: 2000, model: 'glm-4.7', provider: 'zai', cause: 'hard_rule' },
    { ts: 3000, model: 'glm-4.7', provider: 'deepseek', cause: 'hard_rule' },
    { ts: 4000, model: 'gpt-5.6-luna', provider: 'openai-codex', cause: 'fail_safe_strong' },
  ];
  assert.equal(api.lastTriedAt(routes, 'glm-4.7', 'zai'), 2000, 'the LARGEST ts for this pair, not the first');
  assert.equal(api.lastTriedAt(routes, 'glm-4.7', 'deepseek'), 3000, 'same id on another rail is a separate fact');
  assert.equal(api.lastTriedAt(routes, 'gpt-5.6-luna', 'openai-codex'), 4000);
  assert.equal(api.lastTriedAt(routes, 'glm-4.7', 'xiaomi'), null, 'never tried on this rail');
  assert.equal(api.lastTriedAt(routes, 'mimo-v2.5', 'xiaomi'), null, 'model absent from the log');
  assert.equal(api.lastTriedAt(routes, '', 'zai'), null, 'no id, no identity');
  assert.equal(api.lastTriedAt(null, 'glm-4.7', 'zai'), null, 'no log, no line');
});

test('lastTriedAt ignores decisions with junk identity or instant', () => {
  const { api } = loadConsole();
  const routes = [
    { ts: 'abc', model: 'glm-4.7', provider: 'zai' },   // unparseable instant
    { ts: -5, model: 'glm-4.7', provider: 'zai' },      // not an instant
    { ts: 0, model: 'glm-4.7', provider: 'zai' },       // not an instant
    { ts: 10, cause: 'blocklist_veto' },                // a refusal tried nothing
    { ts: 20, model: 'glm-4.7', provider: 'zai' },      // the one that counts
  ];
  assert.equal(api.lastTriedAt(routes, 'glm-4.7', 'zai'), 20);
});

test('a chain row dates the last decision that tried it, off the pinned clock', () => {
  // 260 s before the pinned clock renders as "há 4m" — the compact unit the
  // decision rows already speak (the card's "há 4 min" is the age in prose).
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.routes = [{
    ts: PEAK.getTime() / 1000 - 260, model: 'glm-4.7', provider: 'zai', cause: 'hard_rule', task: 't',
  }];
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /última decisão que tentou este modelo: há 4m/);
});

test('an elo the log never tried gets no dating line at all', () => {
  // DESIGN.md rule 1: an absent record is not absence of use, so there is no
  // line — and never a "nunca foi tentado" fallback.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.routes = [{ ts: 1, model: 'some-other-model', provider: 'other-rail', cause: 'hard_rule' }];
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.doesNotMatch(text, /última decisão/);
  assert.doesNotMatch(text, /nunca foi tentado/);
});

test('the same model id on two providers gets two independent dating lines', () => {
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;
  api.state.loading = false;
  api.state.policy = {
    rules: [], default: {},
    tiers: {
      T1: {
        model: 'glm-4.7', provider: 'zai',
        fallback: [{ model: 'glm-4.7', provider: 'deepseek' }],
        fallback_strategy: 'sequential',
      },
    },
  };
  api.state.routes = [
    { ts: PEAK.getTime() / 1000 - 260, model: 'glm-4.7', provider: 'zai', cause: 'hard_rule', task: 't' },
    { ts: PEAK.getTime() / 1000 - 120, model: 'glm-4.7', provider: 'deepseek', cause: 'hard_rule', task: 't' },
  ];
  api.renderLadder();
  const tried = findAll(dom.get('ladder'), 'hop-tried').map((n) => n.textContent);
  assert.deepEqual(tried, [
    'última decisão que tentou este modelo: há 4m',
    'última decisão que tentou este modelo: há 2m',
  ], 'each rail dates ITS OWN last attempt');
});

test('the last-resort chain on Modelos dates its elos too', () => {
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;
  api.state.loading = false;
  api.state.policy = {
    rules: [], default: {}, tiers: {},
    fail_safe: { model: 'glm-4.7', provider: 'zai', fallback: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }] },
  };
  api.state.routes = [
    { ts: PEAK.getTime() / 1000 - 260, model: 'glm-4.7', provider: 'zai', cause: 'fail_safe_strong', task: 't' },
  ];
  api.renderFailSafe();
  const box = dom.get('failSafeBox');
  assert.match(flat(box), /última decisão que tentou este modelo: há 4m/,
    'the same chainList the groups use, so the same dating vocabulary');
});

test('the elo dating phrase never says "atendeu" or "nunca"', () => {
  // The static promise of the card: the phrase dates a TRY, because the log
  // records the head dispatched and never an outcome. A swap of the verb, or
  // a "nunca" fallback, fails here before it reaches a reader.
  const html = fs.readFileSync(sourcePath, 'utf8');
  const idx = html.indexOf('última decisão que tentou este modelo');
  assert.ok(idx !== -1, 'the dating phrase must exist');
  const snippet = html.slice(idx, idx + 80);
  assert.ok(!snippet.includes('atendeu'), `dating claims an outcome: ${snippet}`);
  assert.ok(!snippet.includes('nunca'), `dating invents an absence: ${snippet}`);
});
// ── the test hour (card t_fbdc3e38) ──────────────────────────────────────

// A cheapest_now plan whose DISPLAYED order flips between 03:00 and 14:00 UTC.
// deepseek-v4-pro doubles inside its real 01:00-04:00 window; the luna hop
// DECLARES a metered rate of $2.50 out — declared keys win over the registry,
// the same precedence capabilities_for applies — so at 03:00 luna leads a
// doubled deepseek, and at 14:00 deepseek's base rate leads luna.
function hourFlipPlan(extra) {
  return chainPlan(Object.assign({
    strategy: 'cheapest_now', strategy_declared: 'cheapest_now', pin_primary: false,
    chain: [
      { model: 'deepseek-v4-pro', provider: 'deepseek', billing_mode: 'metered' },
      { model: 'gpt-5.6-luna', provider: 'openai-codex', billing_mode: 'metered',
        price_in: 1.00, price_out: 2.50 },
    ],
    // The server planned at a flat hour: these are the numbers a chosen hour
    // must NOT reuse — the display at 03:00 has to read deepseek's own window.
    multipliers: { 'deepseek-v4-pro': 1, 'gpt-5.6-luna': 1 },
  }, extra || {}));
}

test('the chosen test hour reprices and reorders a cheapest_now queue, and leaves sequential identical', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.clock = PEAK;   // Monday 07:14 UTC — the weekday frame for a chosen hour
  api.state.policy = tierPolicy();
  api.state.capabilities = api.capabilityRegistry(catalogue('deepseek-v4-pro', 'gpt-5.6-luna'));

  // ── cheapest_now: the SAME plan, two chosen hours, two queues ──
  api.state.testHour = 3;
  api.renderChainPlan(hourFlipPlan());
  assert.deepEqual(findAll(dom.get('chainPlan'), 'hop-model').map((n) => n.textContent),
    ['gpt-5.6-luna', 'deepseek-v4-pro'],
    'at 03:00 UTC deepseek doubles and luna\'s declared $2.50 out leads');
  let text = flat(dom.get('chainPlan'));
  assert.match(text, /hora escolhida: 03:00 UTC/, 'the queue says which chosen hour it is priced at');
  assert.match(text, /2× em hora de pico · \$1\.32 entrada \/ \$3\.96 saída por 1M/,
    'the chosen hour reached the price function: deepseek\'s own window doubles its rate');

  api.state.testHour = 14;
  api.renderChainPlan(hourFlipPlan());
  assert.deepEqual(findAll(dom.get('chainPlan'), 'hop-model').map((n) => n.textContent),
    ['deepseek-v4-pro', 'gpt-5.6-luna'],
    'at 14:00 UTC deepseek is back at base and leads luna\'s declared rate');
  text = flat(dom.get('chainPlan'));
  assert.match(text, /hora escolhida: 14:00 UTC/);
  assert.doesNotMatch(text, /2× em hora de pico/,
    'at 14:00 the same elo is flat — the plan\'s own multipliers were NOT reused at a chosen hour');

  // ── sequential: the field changes NOTHING, and nothing pretends it did ──
  // The variant carries NO plan multipliers, so the prices below come from the
  // elos' own windows at the hour in use — the clock's hour (07:00, inside
  // deepseek's peak), never the chosen 14:00. If the chosen hour leaked into a
  // sequential queue, deepseek would render flat at testHour 14.
  api.state.testHour = 3;
  api.renderChainPlan(hourFlipPlan({ strategy: 'sequential', strategy_declared: 'sequential', multipliers: {} }));
  const at3 = flat(dom.get('chainPlan'));
  api.state.testHour = 14;
  api.renderChainPlan(hourFlipPlan({ strategy: 'sequential', strategy_declared: 'sequential', multipliers: {} }));
  const at14 = flat(dom.get('chainPlan'));
  assert.equal(at3, at14, 'a sequential queue is identical at every chosen hour (card decision 5)');
  assert.doesNotMatch(at14, /hora escolhida/, 'no mark: the queue is NOT priced at a chosen hour');
  assert.doesNotMatch(at14, /ordenada pelo preço/, 'no reorder claim either');
  assert.match(at14, /2× em hora de pico/,
    'the display kept the clock\'s hour (07:00 UTC, inside deepseek\'s peak), not the chosen 14:00');
});

test('the chosen hour reaches the ceiling marks too, and the plan hour is named', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.clock = PEAK;
  api.state.policy = tierPolicy();
  api.state.capabilities = api.capabilityRegistry(catalogue('deepseek-v4-pro', 'gpt-5.6-luna'));
  // The engine planned at 14:00 UTC (a flat hour for deepseek); the operator
  // asks how the queue looks at 03:00, where deepseek doubles past the cap.
  api.state.testHour = 3;
  api.renderChainPlan(hourFlipPlan({
    utc_hour: 14, utc_weekday: 0,
    time_cap: { max_multiplier: 1.5 },
  }));
  const text = flat(dom.get('chainPlan'));
  assert.match(text, /acima do teto agora/,
    'the TETO mark reads the chosen hour: deepseek 2× is above the 1.5× ceiling at 03:00');
  assert.match(text, /planejado às 14:00/, 'the plan\'s own hour is named, since the display is at another');
  assert.match(text, /hora escolhida: 03:00 UTC/);
});

test('the Hora do teste field marks the queue while overridden, and Agora clears it', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.clock = PEAK;   // 07:14 UTC
  api.state.policy = tierPolicy();
  api.state.capabilities = api.capabilityRegistry(catalogue('deepseek-v4-pro', 'gpt-5.6-luna'));
  api.state.chainPlan = hourFlipPlan();

  api.applyTestHour(3);
  assert.equal(api.state.testHour, 3, 'picking an hour sets the override');
  assert.equal(dom.get('probeHour').value, '3', 'the select shows the chosen hour');
  assert.match(flat(dom.get('chainPlan')), /hora escolhida: 03:00 UTC/, 'the mark appears');

  // Agora: back to the clock's hour — the mark leaves and the select repõe.
  api.applyTestHour(null);
  assert.equal(api.state.testHour, null, 'Agora clears the override');
  assert.equal(dom.get('probeHour').value, '7', 'the select shows the clock\'s hour again (07:14 UTC)');
  assert.doesNotMatch(flat(dom.get('chainPlan')), /hora escolhida/, 'the mark leaves with it');
});

test('displayOrder mirrors order_chain: the hour swaps the queue, non-time strategies never reorder', () => {
  const { api } = loadConsole();
  const registry = api.capabilityRegistry(catalogue('deepseek-v4-pro', 'gpt-5.6-luna', 'gpt-5.5'));
  const hops = [
    { model: 'deepseek-v4-pro', provider: 'deepseek', billing_mode: 'metered' },
    { model: 'gpt-5.6-luna', provider: 'openai-codex', billing_mode: 'metered', price_in: 1.00, price_out: 2.50 },
    { model: 'gpt-5.5', provider: 'openai-codex', billing_mode: 'subscription' },
  ];
  const models = (list) => plain(list.map((h) => h.model));
  const monday3 = { hour: 3, weekday: 0 };
  const monday14 = { hour: 14, weekday: 0 };

  // The same three hops order differently at the two chosen hours: at 03:00
  // doubled deepseek ($3.96) trails luna's declared $2.50; at 14:00 its base
  // $1.98 leads. gpt-5.5 ($30) trails both, always.
  assert.deepEqual(models(api.displayOrder(hops, 'cheapest_now', monday3, { registry })),
    ['gpt-5.6-luna', 'deepseek-v4-pro', 'gpt-5.5'], '03:00: luna leads a doubled deepseek');
  assert.deepEqual(models(api.displayOrder(hops, 'cheapest_now', monday14, { registry })),
    ['deepseek-v4-pro', 'gpt-5.6-luna', 'gpt-5.5'], '14:00: base deepseek leads');

  // pin_primary true keeps hop 1 fixed and sorts only the reserves — the same
  // rule capabilities.order_chain applies.
  assert.deepEqual(models(api.displayOrder(hops, 'cheapest_now', monday3, { registry, pinPrimary: true })),
    ['deepseek-v4-pro', 'gpt-5.6-luna', 'gpt-5.5'],
    'a pinned primary stays first; the tail sorts');

  // An unpriced plan-credit hop leads the dollars bucket by billing — never as
  // zero, and never behind a dollar rail it cannot be compared to.
  const withPlan = [hops[0], hops[1], { model: 'glm-5.3', provider: 'zai', billing_mode: 'plan' }];
  assert.deepEqual(models(api.displayOrder(withPlan, 'cheapest_now', monday3, { registry })),
    ['glm-5.3', 'gpt-5.6-luna', 'deepseek-v4-pro'],
    'plan credits bucket first, then the dollars at 03:00');

  // No hour, sequential and random: the queue never reorders — the mutation
  // "random passa a reordenar por hora" fails here.
  assert.deepEqual(models(api.displayOrder(hops, 'cheapest_now', null, { registry })), models(hops),
    'no hour means no reorder');
  assert.deepEqual(models(api.displayOrder(hops, 'sequential', monday3, { registry })), models(hops),
    'sequential is the written order at any hour');
  assert.deepEqual(models(api.displayOrder(hops, 'random', monday3, { registry })), models(hops),
    'random must not reorder by the hour either');
});
test('triedShare counts the decisions that TRIED this model@provider pair', () => {
  const { api } = loadConsole();
  const routes = [
    { ts: 1000, model: 'glm-4.7', provider: 'zai', cause: 'hard_rule' },
    { ts: 2000, model: 'glm-4.7', provider: 'zai', cause: 'hard_rule' },
    { ts: 3000, model: 'glm-4.7', provider: 'deepseek', cause: 'hard_rule' },
    { ts: 4000, model: 'gpt-5.6-luna', provider: 'openai-codex', cause: 'fail_safe_strong' },
  ];
  assert.deepEqual(plain(api.triedShare(routes, 'glm-4.7', 'zai')), { n: 2, total: 4 });
  assert.deepEqual(plain(api.triedShare(routes, 'glm-4.7', 'deepseek')), { n: 1, total: 4 },
    'same id on another rail is a separate share');
  assert.deepEqual(plain(api.triedShare(routes, 'mimo-v2.5', 'xiaomi')), { n: 0, total: 4 },
    'never tried: 0 of the total, not an absent number');
  assert.deepEqual(plain(api.triedShare([], 'glm-4.7', 'zai')), { n: 0, total: 0 }, 'no log, no column');
  assert.deepEqual(plain(api.triedShare(null, 'glm-4.7', 'zai')), { n: 0, total: 0 });
  assert.deepEqual(plain(api.triedShare(routes, '', 'zai')), { n: 0, total: 4 }, 'no id, no identity');
});

test('triedShare denominator counts only decisions with a legible attempted model', () => {
  const { api } = loadConsole();
  const routes = [
    { ts: 10, cause: 'deny' },                     // a refusal tried nothing
    { ts: 20, model: '', provider: 'zai' },        // junk identity
    { ts: 30, model: 'glm-4.7', provider: 'zai' }, // the one that counts
  ];
  assert.deepEqual(plain(api.triedShare(routes, 'glm-4.7', 'zai')), { n: 1, total: 1 });
});

test('each chain entry shows its observed share: 154 decisions, sum closes, 0 shows, window cited', () => {
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;
  api.state.loading = false;
  api.state.policy = {
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
    },
  };
  const routes = [];
  const now = PEAK.getTime() / 1000;
  const add = (model, provider, ts) => routes.push({ ts, model, provider, cause: 'hard_rule', task: 't' });
  // 153 decisions inside the last 3h, plus ONE 3 days old — the window is the
  // data's own span (so "nos últimos 3 dias"), and a clock-relative filter
  // that dropped the old decision would make the total 153 and break the sum.
  for (let i = 0; i < 99; i += 1) add('glm-4.7', 'zai', now - 60 - i * 60);
  for (let i = 0; i < 54; i += 1) add('gpt-5.6-luna', 'openai-codex', now - 3600 - i * 60);
  add('glm-4.7', 'zai', now - 3 * 86400);
  assert.equal(routes.length, 154);
  api.state.routes = routes;
  api.renderLadder();
  const shares = findAll(dom.get('ladder'), 'hop-share').map((n) => n.textContent);
  assert.deepEqual(shares, [
    'tentada em 100 das 154 decisões (nos últimos 3 dias)',
    'tentada em 54 das 154 decisões (nos últimos 3 dias)',
    'tentada em 0 das 154 decisões (nos últimos 3 dias)',
  ], 'the three shares sum to the total, the zero entry SHOWS its 0, and the window is cited');
  const sum = shares.reduce((acc, s) => acc + Number(/em (\d+) das/.exec(s)[1]), 0);
  assert.equal(sum, 154, 'the shares close the sum');
});

test('the same model id on two providers gets two independent share lines', () => {
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;
  api.state.loading = false;
  api.state.policy = {
    rules: [], default: {},
    tiers: {
      T1: {
        model: 'glm-4.7', provider: 'zai',
        fallback: [{ model: 'glm-4.7', provider: 'deepseek' }],
        fallback_strategy: 'sequential',
      },
    },
  };
  const now = PEAK.getTime() / 1000;
  const routes = [];
  for (let i = 0; i < 10; i += 1) routes.push({ ts: now - 60 - i, model: 'glm-4.7', provider: 'zai', cause: 'hard_rule', task: 't' });
  for (let i = 0; i < 7; i += 1) routes.push({ ts: now - 3600 - i, model: 'glm-4.7', provider: 'deepseek', cause: 'hard_rule', task: 't' });
  api.state.routes = routes;
  api.renderLadder();
  const shares = findAll(dom.get('ladder'), 'hop-share').map((n) => n.textContent);
  assert.deepEqual(shares, [
    'tentada em 10 das 17 decisões (na última hora)',
    'tentada em 7 das 17 decisões (na última hora)',
  ], 'each rail counts ITS OWN attempts — counting by model alone would give 17 on both');
});

test('no recorded decision means no share column on any line', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.routes = [];
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.doesNotMatch(text, /tentada em/);
  assert.doesNotMatch(text, /0 de 0/);
});

test('the last-resort chain on Modelos shows its observed share too', () => {
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;
  api.state.loading = false;
  api.state.policy = {
    rules: [], default: {}, tiers: {},
    fail_safe: { model: 'glm-4.7', provider: 'zai', fallback: [{ model: 'gpt-5.6-luna', provider: 'openai-codex' }] },
  };
  api.state.routes = [
    { ts: PEAK.getTime() / 1000 - 260, model: 'glm-4.7', provider: 'zai', cause: 'fail_safe_strong', task: 't' },
  ];
  api.renderFailSafe();
  const box = dom.get('failSafeBox');
  assert.match(flat(box), /tentada em 1 das 1 decisões \(no último minuto\)/,
    'the same chainList the groups use, so the same share vocabulary');
});

// ── Card t_7c5d6f91: the Modelos tab's three read-only blocks ─────────────
// The contract is comp-modelos.html plus the LEIA-ME axis correction (spec
// t_c90c5336): the 24-cell strip is PER MODEL, grouped visually by provider —
// never aggregated by provider, because two models of one provider may declare
// different windows and the aggregation would hide the divergence.

// A registry table with TWO zai models sharing the 06-10 Mon-Fri peak, one
// deepseek model with its daily 01-04 peak, and one flat openai-codex model.
// The windows are real registry shapes (capabilities.py carries exactly these
// hours/multipliers for the zai family), so the strip test prices the same
// declarations the running path prices.
function stripRegistry() {
  return {
    'glm-4.7': {
      provider: 'zai', context_window: 200000, billing_mode: 'plan',
      price_in: 0.60, price_out: 2.20,
      price_windows: [{ hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2.0 }],
    },
    'glm-5.3': {
      provider: 'zai', context_window: 200000, billing_mode: 'plan',
      price_in: 1.20, price_out: 4.00,
      price_windows: [{ hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2.0 }],
    },
    'deepseek-v4-pro': {
      provider: 'deepseek', context_window: 128000, billing_mode: 'metered',
      price_windows: [{ hours_utc: [1, 4], multiplier: 2.0 }],
    },
    'gpt-5.6-luna': {
      provider: 'openai-codex', context_window: 272000, billing_mode: 'subscription',
    },
  };
}

test('the price strip draws one 24-cell band PER MODEL, two rows inside one provider group', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  api.state.clock = PEAK; // Monday 07:14 UTC - inside zai's and deepseek's peaks
  api.renderPriceStrip();
  const box = dom.get('priceStrip');
  // Provider groups: zai, deepseek, openai-codex. The group is a frame, the
  // strip is a row inside it - aggregating by provider is the defect the spec
  // corrected, so the counts assert BOTH levels.
  const groups = findAll(box, 'price-group');
  assert.equal(groups.length, 3, 'one visual group per provider');
  const bands = findAll(box, 'price-band');
  assert.equal(bands.length, 4, 'one band per MODEL - glm-4.7 and glm-5.3 are two bands, not one zai band');
  // Floor AND ceiling on the band: exactly 24 cells, never 23 or 25.
  bands.forEach((band) => {
    const cells = findAll(band, 'h-cell');
    assert.equal(cells.length, 24, 'every band has exactly 24 cells');
  });
  // The zai group must contain the two divergent-capable models, named.
  const zaiText = flat(groups[0]);
  assert.match(zaiText, /glm-4\.7/);
  assert.match(zaiText, /glm-5\.3/);
});

test('peak, cheap and base cells get the three states, and ONLY the current hour is marked now', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  api.state.clock = PEAK; // Monday 07:14 UTC: zai peak (6..10), deepseek NOT (1..4)
  api.renderPriceStrip();
  const bands = findAll(dom.get('priceStrip'), 'price-band');
  // Band order follows the registry's own key order: glm-4.7, glm-5.3, deepseek, codex.
  const zaiCells = findAll(bands[0], 'h-cell');
  assert.equal(zaiCells.filter((c) => c.classList.contains('peak')).length, 4,
    "zai peak is hours 6,7,8,9 - the half-open window [6,10)");
  assert.equal(zaiCells.filter((c) => c.classList.contains('now')).length, 1,
    'exactly one now cell per band');
  assert.ok(zaiCells[7].classList.contains('now'), 'hour 7 is now - clock is 07:14 UTC');
  assert.ok(zaiCells[7].classList.contains('peak'), 'the now cell can also be a peak cell');
  const dsCells = findAll(bands[2], 'h-cell');
  assert.equal(dsCells.filter((c) => c.classList.contains('peak')).length, 3,
    'deepseek peak is hours 1,2,3 - daily, no weekday gate needed here');
  const codexCells = findAll(bands[3], 'h-cell');
  assert.equal(codexCells.filter((c) => c.classList.contains('peak')).length, 0,
    'a model with no declared windows has no peak cells at all');
  assert.equal(codexCells.filter((c) => c.classList.contains('cheap')).length, 0,
    'and no cheap cells - flat is flat');
});

test('the now mark follows the INJECTED clock, not a second reader', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  // NIGHT is Monday 18:00 UTC - outside every window in the fixture.
  api.state.clock = NIGHT;
  api.renderPriceStrip();
  let bands = findAll(dom.get('priceStrip'), 'price-band');
  bands.forEach((band) => {
    const nowCells = findAll(band, 'h-cell').filter((c) => c.classList.contains('now'));
    assert.equal(nowCells.length, 1);
    assert.ok(findAll(band, 'h-cell')[18].classList.contains('now'),
      'hour 18 is now - the mark moved with the injected clock');
  });
  // Move the clock and the mark moves: same state, same render, new hour.
  api.state.clock = new Date(Date.UTC(2026, 7, 17, 2, 0));
  api.renderPriceStrip();
  bands = findAll(dom.get('priceStrip'), 'price-band');
  const ds = findAll(bands[2], 'h-cell');
  assert.ok(ds[2].classList.contains('now'));
  assert.ok(ds[2].classList.contains('peak'), '02:00 UTC is inside deepseek peak');
  assert.equal(ds.filter((c) => c.classList.contains('now')).length, 1);
});

test('a weekday-gated window only paints peak on the days it declared', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  // WEEKEND is Saturday 07:00 UTC: zai's peak is Mon-Fri, so Saturday paints base.
  api.state.clock = WEEKEND;
  api.renderPriceStrip();
  const bands = findAll(dom.get('priceStrip'), 'price-band');
  assert.equal(findAll(bands[0], 'h-cell').filter((c) => c.classList.contains('peak')).length, 0,
    "zai's Mon-Fri gate holds - Saturday 07:00 is base price");
});

test('a cheap window paints cheap cells below the base', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  const registry = stripRegistry();
  registry['mimo-v2.5'] = {
    provider: 'xiaomi', context_window: 1050000, billing_mode: 'metered',
    price_windows: [{ hours_utc: [16, 24], multiplier: 0.8 }],
  };
  api.state.capabilities = registry;
  api.state.clock = NIGHT; // Monday 18:00 UTC - inside the 16-24 discount
  api.renderPriceStrip();
  const bands = findAll(dom.get('priceStrip'), 'price-band');
  const mimo = bands[bands.length - 1];
  const cells = findAll(mimo, 'h-cell');
  assert.equal(cells.filter((c) => c.classList.contains('cheap')).length, 8,
    'hours 16..23 are cheap - the [16,24) window');
  assert.ok(cells[18].classList.contains('cheap'));
  assert.equal(cells.filter((c) => c.classList.contains('cheap')).length, 8,
    'no arithmetic shortcut: exactly 8 cheap cells');
});

test('an empty or unreadable registry renders the honest absence, not an empty frame', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = null;
  api.renderPriceStrip();
  // The section's title is static markup beside the box; the BOX is what the
  // render owns, so the absence is asserted where the render draws it.
  const text = flat(dom.get('priceStrip'));
  assert.match(text, /sem catálogo/i,
    'the strip says WHY there are no bands - an absent catalogue is a stated absence, not a blank box');
  assert.doesNotMatch(text, /preço base|pico, custa mais/,
    'and it draws no legend for bands that do not exist');
  assert.equal(findAll(dom.get('priceStrip'), 'price-band').length, 0);
});

// ── the groups block: observed share + the DISABLED peak-policy selector ──

test('each group card shows its observed share of decisions, derived from decisionGroup', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [
      { id: 'r1', status: 'stable', when: {}, then: { model: 'T2' } },
    ],
    default: {},
    tiers: {
      T2: { model: 'glm-5.3', provider: 'zai', fallback: [], fallback_strategy: 'sequential' },
    },
  };
  const now = PEAK.getTime() / 998;
  const routes = [];
  for (let i = 0; i < 6; i += 1) routes.push({ ts: now - 60 - i, model: 'glm-5.3', provider: 'zai', rule_id: 'r1', cause: 'hard_rule', task: 't' });
  for (let i = 0; i < 2; i += 1) routes.push({ ts: now - 90 - i, model: 'glm-4.7', provider: 'zai', rule_id: null, cause: 'profile_ignored', task: 't' });
  api.state.routes = routes;
  api.renderLadder();
  const text = flat(dom.get('ladder'));
  assert.match(text, /6 de 8/, 'six of the eight decisions went through T2');
});

test('the peak-policy selector renders DISABLED and names the follow-up card as the reason', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = {
    rules: [], default: {},
    tiers: { T1: { model: 'glm-4.7', provider: 'zai', fallback: [], fallback_strategy: 'sequential' } },
  };
  api.state.routes = [];
  api.renderLadder();
  const selects = findAll(dom.get('ladder'), 'peak-policy');
  assert.equal(selects.length, 1, 'one selector per group card');
  const sel = selects[0];
  assert.equal(sel.disabled, true, 'the selector does not write yet');
  assert.match(sel.title, /card filho|ainda não grava|não grava/,
    'the reason rides on the control itself, where the pointer lands');
  const opts = (sel.children || []).filter((c) => c.tagName === 'option');
  assert.deepEqual(opts.map((o) => o.textContent), ['manter a ordem', 'evitar o pico', 'usar o mais barato'],
    'the three contract states, in contract order');
});

// ── the compaction block: state read off the real /compaction shape ───────

function compactionPayload() {
  return {
    aggressiveness: 50,
    summarizer_window: 272000,
    threshold_fraction: 0.766,
    threshold_tokens: 208352,
    warning: false,
    model_thresholds: { 'glm-4.7': 0.8, 'deepseek-v4-pro': 0.85, 'gpt-5.6-terra': 0.62 },
    compaction: {
      provider: 'zai',
      model: 'glm-4.5-flash',
      fallback_chain: [
        { model: 'glm-4.7', provider: 'zai' },
        { model: 'gpt-5.6-luna', provider: 'openai-codex' },
        { model: 'mimo-v2.5', provider: 'xiaomi' },
      ],
    },
    compaction_errors: [],
  };
}

test('the compaction block states threshold, window, aggressiveness and every model threshold', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.compaction = compactionPayload();
  api.renderCompaction();
  const text = flat(dom.get('compactionBox'));
  assert.match(text, /208\.352 tokens/, 'the threshold in tokens, formatted pt-BR');
  assert.match(text, /272\.000/, 'the summarizer window it is a fraction of');
  assert.match(text, /76,6%|0,766/, 'the fraction named');
  assert.match(text, /glm-4\.5-flash/, 'the model that compacts today');
  assert.match(text, /zai/, 'its provider');
  assert.match(text, /glm-4\.7 → gpt-5\.6-luna → mimo-v2\.5/, 'the fallback queue, said once, in order');
  ['glm-4.7', 'deepseek-v4-pro', 'gpt-5.6-terra'].forEach((m) => {
    assert.match(text, new RegExp(m.replace(/\./g, '\\.')),
      `per-model threshold for ${m} is on screen`);
  });
});

test('a compaction block that is not declared is an honest absence, not a fake zero', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.compaction = Object.assign({}, compactionPayload(), { compaction: null, compaction_errors: [] });
  api.renderCompaction();
  const text = flat(dom.get('compactionBox'));
  assert.match(text, /208\.352 tokens/);
  assert.match(text, /nenhum modelo escolhido|sem escolha|não informado/,
    'no invented model - the absence is said in words');
  assert.doesNotMatch(text, /modelo não informado: null|undefined/,
    'and never a stringified null standing in for an answer');
});

test('compaction refusals ride on screen, not as a 400 page', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.compaction = {
    ...compactionPayload(),
    compaction: null,
    compaction_errors: ["compaction.model 'glm-9' is not in the capability registry - compacting on an unknown id would fail only when the conversation is already too large to carry"],
  };
  api.renderCompaction();
  const text = flat(dom.get('compactionBox'));
  assert.match(text, /glm-9/);
  assert.match(text, /não está no catálogo|is not in the capability registry/,
    'the refusal is displayed verbatim - it is the motor speaking, not the screen guessing');
});

test('the compaction controls are disabled and say why', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.compaction = compactionPayload();
  api.renderCompaction();
  const disableds = findAll(dom.get('compactionBox'), 'ctl');
  assert.ok(disableds.length >= 2, 'model choice and fallback queue are both controls');
  disableds.forEach((c) => {
    assert.equal(c.disabled, true);
    assert.match(c.title, /card filho|ainda não grava|não grava/);
  });
});

test('a missing /compaction read degrades to the stated absence', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.compaction = null;
  api.renderCompaction();
  const text = flat(dom.get('compactionBox'));
  assert.match(text, /sem leitura|não respondeu|Sem leitura/,
    'the block says the read failed - it does not render zeros as if they were facts');
  assert.doesNotMatch(text, /0 tokens/);
});
