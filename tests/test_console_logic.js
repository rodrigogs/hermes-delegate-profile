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

function escapedHtml(value, attribute = false) {
  const text = String(value);
  return text.replace(attribute ? /[&"<]/g : /[&<>]/g, (char) => ({
    '&': '&amp;', '"': '&quot;', '<': '&lt;', '>': '&gt;',
  }[char]));
}

function outerHtml(node) {
  const tag = node.tagName || 'div';
  const classes = [...new Set([
    ...String(node.className || '').split(/\s+/).filter(Boolean),
    ...node.classList._set,
  ])];
  const attrs = [];
  if (node._realId) attrs.push(`id="${escapedHtml(node.id, true)}"`);
  if (classes.length) attrs.push(`class="${escapedHtml(classes.join(' '), true)}"`);
  if (node.hidden) attrs.push('hidden');
  if (node.title) attrs.push(`title="${escapedHtml(node.title, true)}"`);
  ['type', 'tabIndex', 'htmlFor'].forEach((key) => {
    if (node[key] !== undefined && node[key] !== '') {
      attrs.push(`${key === 'tabIndex' ? 'tabindex' : key === 'htmlFor' ? 'for' : key}="${escapedHtml(node[key], true)}"`);
    }
  });
  Object.entries(node.dataset).forEach(([key, value]) => {
    attrs.push(`data-${key.replace(/[A-Z]/g, (char) => `-${char.toLowerCase()}`)}="${escapedHtml(value, true)}"`);
  });
  Object.entries(node.attrs).forEach(([key, value]) => attrs.push(`${key}="${escapedHtml(value, true)}"`));
  const body = `${escapedHtml(node.textContent || '')}${node.children.map(outerHtml).join('')}`;
  return `<${tag}${attrs.length ? ` ${attrs.join(' ')}` : ''}>${body}</${tag}>`;
}

// A DOM stub good enough for the console's init path.
function fakeDom() {
  const nodes = new Map();
  const make = (id) => {
    const node = {
      id, _realId: false, className: '', textContent: '', value: '', title: '',
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
      getBoundingClientRect() { return { width: 900, height: 300, top: 0, left: 0, right: 900 }; },
      clientWidth: 900,
      scrollIntoView(opts) { node._scrolledTo = opts || null; },
      focus() {},
      // Card t_3ba979a1: the JSON tools drive the editor programmatically —
      // Copiar selects the whole text, Formatar lands the cursor back on the
      // key. The stub records the calls so the flows are assertable.
      select() {},
      setSelectionRange(a, b) { node.selectionStart = a; node.selectionEnd = b; },
    };
    Object.defineProperty(node, 'id', {
      get: () => id,
      set(next) {
        nodes.delete(id);
        id = String(next);
        nodes.set(id, node);
      },
    });
    Object.defineProperty(node, 'firstChild', { get: () => node.children[0] || null });
    Object.defineProperty(node, 'outerHTML', { get: () => outerHtml(node) });
    return node;
  };
  const get = (id) => {
    if (!nodes.has(id)) {
      const node = make(id);
      node._realId = true;
      nodes.set(id, node);
    }
    return nodes.get(id);
  };
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

function loadConsole({ width = 1440, embedded = false, csrfToken, fetch: fetchStub, dom: domIn, keepWire = false, navigator: navIn, timers } = {}) {
  const html = fs.readFileSync(sourcePath, 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
    // Skip the init calls that need a live browser; keep everything else intact.
    // keepWire leaves wire() in place — the arrow-key test needs the handlers it
    // registers — and strips only the layout/clock/load tail.
    .replace(keepWire
      ? /\n      applyLayout\(\);[\s\S]*?load\(\);\n/
      : /\n      wire\(\);[\s\S]*?load\(\);\n/, '\n');
  const dom = domIn || fakeDom();
  const top = {};
  const win = { innerWidth: width, addEventListener() {}, top };
  win.self = embedded ? win : top;
  if (csrfToken !== undefined) win.__HERMES_CONFIG__ = { csrfToken };
  const context = {
    console, window: win, document: dom.document, globalThis: {},
    fetch: fetchStub || (() => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') })),
    setTimeout() {},
    setInterval: timers ? timers.setInterval : undefined,
    clearInterval: timers ? timers.clearInterval : undefined,
    Math, JSON, Number, Object, Array, String, Set, Map, Date, encodeURIComponent,
    // Card t_3ba979a1: Copiar reads navigator.clipboard. The default stub has
    // no clipboard, so the fallback path is what runs; a test injects a
    // clipboard stub to pin the happy path.
    navigator: navIn !== undefined ? navIn : { clipboard: null },
  };
  vm.runInNewContext(script, context, { filename: sourcePath });
  return { api: context.globalThis.__router, dom, win };
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

  // The Tarefas count is GONE: the sheet's numbered rule list is its own counter.
  assert.equal(dom.get('stateTarefas').children.length, 1,
    'tarefas keeps only its meaningful policy state');
  assert.equal(dom.get('stateTarefas').children[0].children.length, 1,
    'tarefas draws a dot but no duplicate count');
  assert.equal(dom.get('stateDecisoes').children[0].children[0].textContent, '2', 'decisões counts recorded decisions');
  // The Modelos badge counts EXCEPTIONS, not elos: two models with no bans or
  // breaker cooldowns show only the degraded state, not "2".
  assert.equal(dom.get('stateModelos').children[0].children.length, 1,
    'no exceptions → no modelos count, however many elos');
  // One degraded target must surface, not be averaged into "fine".
  assert.match(dom.get('stateModelos').children[0].className, /is-degraded/);
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
  const warningCount = dom.get('stateModelos').children[0].children[0];
  assert.equal(warningCount.textContent, '2', 'bans + breakers, not elos');
  assert.match(warningCount.className, /is-warn/,
    'an exception count wears amber, the attention colour');

  // Exceptions cleared → the count node is removed entirely (zero is not drawn, §2.1).
  api.state.blocklist = { manual_bans: [], breaker_cooldowns: [] };
  api.renderRail();
  assert.equal(dom.get('stateModelos').children[0].children.length, 1);
  assert.equal(dom.get('stateModelos').children[0].children[0].className, 'dot');
});

test('the rail survives being rendered before any data arrives', () => {
  const { api, dom } = loadConsole();
  // The rail is rendered at boot, before the first poll. An unguarded read
  // here kills the whole IIFE and the operator gets a blank page.
  assert.doesNotThrow(() => api.renderRail());
  assert.equal(dom.get('stateTarefas').children.length, 0, 'no policy yet → no state node is drawn');
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

test('the freshness report is one line, so the embedded head matches the host it sits beside', () => {
  // TWO RECORDED DECISIONS WERE IN CONTRADICTION, and the operator saw the result.
  //
  //   * `.is-embedded .view-head` is tuned to the host sidebar's .panel-head
  //     "exactly: 41px min-height, 8px/16px padding", with the arithmetic written
  //     down — "25px of content plus 16px of padding lands on 41 exactly" — which is
  //     true of a ONE-LINE reach. The comment says WHY: a head that misses that
  //     height "read as a second navigation row one row off from the real one".
  //   * `.reach`'s own comment then made the three ages "their own lines at every
  //     width", because they ARE the report and not collapsible chrome.
  //
  // Measured in the real Hermes One panel on 2026-09-02: .view-head renders 59px,
  // 18px over its target, and the .reach block inside it is 165×42 starting at
  // x=561 of an 851px band — so two thirds of the head is empty and the densest
  // text on the screen is jammed into the right edge of it.
  //
  // The resolution keeps BOTH intents: every age keeps its full words and stays
  // visible at every width (nothing collapses, nothing hides, nothing moves to
  // another surface), and they sit on ONE line, which is what returns the head to
  // 41px.
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.unreachable = false;
  api.state.status = {
    process_started_at: new Date(T - 2 * 3600 * 1000).toISOString(),
    code_mtime: new Date(T - 3 * 3600 * 1000).toISOString(),
    config_mtime: new Date(T - 5 * 60 * 1000).toISOString(),
  };
  api.renderRail();
  const text = dom.get('reachText').textContent;
  // Nothing was dropped: all three ages, in their own words.
  assert.match(text, /serviço no ar há 2h/);
  assert.match(text, /código carregado há 3h/);
  assert.match(text, /arquivo mudou há 5m/);
  // And it is one line, which is the whole point.
  assert.equal(text.includes('\n'), false,
    'a three-line reach puts the embedded head 18px over the 41px its own comment '
    + 'measured against the host sidebar');
  assert.match(text, / · /, 'the ages are separated by the middot this console uses everywhere');
  // The CSS that only made sense while it was multi-line goes with it.
  const { style } = consoleStyle();
  assert.doesNotMatch(style, /#reachText\s*{[^}]*pre-line/,
    'pre-line existed to render the newlines; there are none left to render');
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
// The mode itself is gone (card t_f81c24ee): no Editar button, no
// reading/editing state. What the read-only default was FOR — a stray tap must
// not change routing — is now the row click: it opens the QUEUE, never an
// editor. Only a click on a VALUE — a condition, the destination cell or a
// queue capsule — opens that value's edit. These tests pin both halves.
test('the console ships no edit mode: the contract greps are zero', () => {
  const src = fs.readFileSync(sourcePath, 'utf8');
  assert.doesNotMatch(src, /state\.mode === 'editing'/,
    'the mode comparison the card greps for is gone');
  assert.doesNotMatch(src, /editLabel/,
    'the mode button label the card greps for is gone');
  assert.doesNotMatch(src, /setMode|editMode/,
    'no mode machinery survives anywhere');
  const { api } = loadConsole();
  assert.equal(api.setMode, undefined, 'the console no longer exports a mode setter');
  assert.equal('mode' in api.state, false, 'state has no mode key');
});

test('a stray tap on a row opens the queue, never the editor', () => {
  // The row click is the read gesture (comp-tarefas: "Clique na linha para
  // abrir a fila inteira"). A stray tap must not open an editor and must not
  // arm anything — the open queue is the whole of what a row click does.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.renderSheet();
  const row = dom.get('sheet').children[0];
  const open = row.children.find((c) => c.className === 'step-open');
  assert.ok(open, 'the row carries the open block');
  assert.equal(open.hidden, true, 'closed by default');
  assert.equal(api.state.selected, null, 'nothing is selected before the click');
  row._listeners.click();
  assert.equal(open.hidden, false, 'a row click opens the queue');
  assert.equal(api.state.selected, null, 'and it does not open the rule editor');
  assert.match(flat(open), /fila/, 'the queue is what opened');
});

test('clicking a condition opens that rule\'s editor — no mode to arm', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const row = dom.get('sheet').children.find((c) => c.dataset.ruleId === 'audit');
  const when = findAll(row, 'step-when')[0];
  assert.ok(when, 'the rule carries its condition line');
  assert.ok(when.classList.contains('is-edit'), 'the clickable line wears the editable affordance');
  assert.equal(api.state.selected, null, 'nothing selected before the click');
  when._listeners.click();
  assert.equal(api.state.selected, 'rule:audit', 'the click opens the rule\'s editor');
  const box = dom.get('inspector');
  assert.match(flat(box), /Condições/, 'the editor leads with the conditions block');
  assert.ok(byLabel(box, 'keywords (contains)'),
    'the very clause the operator clicked is a field here');
});

test('clicking the destination cell opens the rule\'s editor, group first', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.renderSheet();
  const row = dom.get('sheet').children[0]; // 'deep' -> T3
  const dest = findAll(row, 'step-dest')[0];
  assert.ok(dest, 'the destination cell exists');
  assert.ok(dest.classList.contains('is-edit'), 'the cell wears the editable affordance');
  dest._listeners.click();
  assert.equal(api.state.selected, 'rule:deep', 'the cell click opens the rule\'s editor');
  assert.ok(byLabel(dom.get('inspector'), 'Destino'),
    'the destination control is the edit the click leads to');
});

test('clicking a capsule in the open queue opens the group\'s chain editor', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.state.capabilities = capModels();
  api.renderSheet();
  const row = dom.get('sheet').children[0]; // 'deep' -> T3
  row._listeners.click(); // open the queue (the row click is the read gesture)
  const chain = findAll(row, 'step-open-chain')[0];
  assert.ok(chain, 'the open queue carries the chain line');
  assert.ok(chain.classList.contains('is-edit'), 'the capsules wear the editable affordance');
  chain._listeners.click();
  assert.equal(api.state.selected, 'tier:T3', 'the capsule click opens the group\'s queue editor');
  assert.ok(byLabel(dom.get('inspector'), 'Modelo'),
    'the chain editor is the surface where that hop is swapped');
});

// WHERE THE EDITOR APPEARS IS THE WHOLE OF WHETHER IT IS USABLE. The three tests
// above prove the click opens the editor; none of them proved the operator can
// SEE it. It could not: renderInspector filled `<div id="inspector">`, declared as
// the LAST child of panel-tarefas — below every rule row, below the "se nada acima
// casou" tail and below the stale note — and pickBind neither scrolled nor
// focused. On the reference install (8 rules, 1440×900) the editor opened roughly
// 900px below the fold, so a click on row 1 produced no visible change at all, and
// the gesture that DID produce one — the row click, which opens the read-only
// fila/papel/regra block — taught that clicking shows details rather than edits.
//
// So the editor is now DOCKED at the click: one node, moved to whatever owns the
// selection. One node and not two, because a second editor would be a second
// renderInspector, a second surfacePatch and a second write path to keep honest.
function contains(root, node) {
  const kids = root.children || [];
  for (let i = 0; i < kids.length; i += 1) {
    if (kids[i] === node || contains(kids[i], node)) return true;
  }
  return false;
}

test('the editor opens inside the row the operator clicked, not at the foot of the tab', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const ruleRow = () => dom.get('sheet').children.find((c) => c.dataset.ruleId === 'audit');
  findAll(ruleRow(), 'step-when')[0]._listeners.click();
  const box = dom.get('inspector');
  assert.match(flat(box), /Condições/, 'the editor did render');
  // Re-read the row: pickBind re-renders the sheet, so the node the click came
  // from is gone and the docked row is its replacement.
  const row = ruleRow();
  assert.ok(contains(row, box),
    'the editor is a descendant of the row whose condition was clicked');
  // The dock is the row's OPEN block, so the row reads as one thing: the read
  // view the row click gives, and the edit view underneath it.
  const open = findAll(row, 'step-open')[0];
  assert.ok(open && contains(open, box), 'it docks inside the row\'s open block');
  assert.equal(open.hidden, false, 'and clicking a value forces that block open');
  assert.match(flat(open), /fila/, 'the read view is still there, above the editor');
});

test('a rebuild of the sheet leaves the open editor where the operator left it', () => {
  // The sheet is rebuilt on a timer-driven reload, not only by a click. An
  // editor that closed itself every few seconds would be worse than one that
  // opened out of sight, so the dock is re-established from state.selectedOrigin
  // on every render rather than being a one-shot of the click.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const ruleRow = () => dom.get('sheet').children.find((c) => c.dataset.ruleId === 'audit');
  findAll(ruleRow(), 'step-when')[0]._listeners.click();
  const box = dom.get('inspector');
  assert.ok(contains(ruleRow(), box), 'docked once');
  api.renderSheet();
  assert.equal(dom.get('inspector'), box, 'the node was moved, never replaced');
  assert.ok(contains(ruleRow(), box), 'and the rebuilt row has it again');
  assert.equal(findAll(ruleRow(), 'step-open')[0].hidden, false, 'still open');
});

test('moving the selection to another surface does not lose the editor', () => {
  // The gesture: the editor is docked in a Tarefas row, and the operator picks a
  // group from the Modelos ladder. The selection crosses surfaces, so the row that
  // holds the editor is about to be discarded by a renderer that is NOT the one
  // taking the editor.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.state.capabilities = capModels();
  api.renderSheet();
  api.renderLadder();
  const ruleRow = () => dom.get('sheet').children[0];
  findAll(ruleRow(), 'step-dest')[0]._listeners.click();
  const box = dom.get('inspector');
  assert.ok(contains(ruleRow(), box), 'docked in the row first');
  // Now the ladder's group name — a different surface, a different dock owner.
  findAll(dom.get('ladder'), 'tier-name')[0]._listeners.click();
  assert.equal(api.state.selectedOrigin, 'ladder:T3');
  assert.ok(contains(findAll(dom.get('ladder'), 'tier')[0], box),
    'the editor moved to the ladder entry');
  assert.equal(contains(dom.get('sheet'), box), false,
    'and left the sheet rather than riding a discarded row out of the document');
  assert.ok(byLabel(box, 'Modelo'), 'and it is filled, so the node that moved is the live one');
});

test('the editor is moved by reference and parked by containment, not by id and origin', () => {
  // THE DOM STUB CANNOT SEE THIS ONE, which is why it is a contract test. Its
  // getElementById is a Map lookup, so no node is ever unreachable and a detached
  // editor still answers — the exact opposite of a browser, where a node inside a
  // discarded row is gone and getElementById returns null.
  //
  // Reproduced in a real browser against the live local hermes-stack on 2026-09-02:
  // with the editor docked in a Tarefas row, clicking a group name on Modelos left
  // `document.getElementById('inspector')` === null and no click reopened the editor
  // until a page reload. Two causes, both pinned here.
  const src = fs.readFileSync(sourcePath, 'utf8');
  const park = src.match(/function parkInspector\([^)]*\)\s*{([\s\S]*?)\n      }/);
  assert.ok(park, 'parkInspector is gone or reformatted — this test cannot see it');

  // (1) It must decide on WHERE THE NODE IS. An origin-prefix test asks where the
  // selection is going, and those differ exactly when the selection crosses
  // surfaces — which is the gesture that lost the editor.
  assert.match(park[1], /inspectorInside/,
    'the park decision is containment of the current host');
  assert.doesNotMatch(park[1], /selectedOrigin/,
    'and never state.selectedOrigin, which describes the destination, not the node');

  // (2) It must not resolve the node by id: a detached node has no id to find, so
  // resolving that way fails precisely when recovery matters.
  assert.doesNotMatch(park[1], /\$\('inspector'\)/,
    "parkInspector must use the tracked reference, not $('inspector')");
  assert.match(src, /function inspectorEl\(\)\s*{\s*\n\s*if \(!inspectorNode\) inspectorNode = \$\('inspector'\);/,
    'the id is used once, to find the node; the reference is what moves afterwards');
});

test('the editor goes home when the row it was docked in stops existing', () => {
  // clear(sheet) removes the sheet's children, and in a real document that
  // DETACHES everything inside them — so an editor left in a discarded row is no
  // longer reachable by getElementById, and every later renderInspector would
  // fill a node that is not on the page. Parking before the clear is what keeps
  // the node in the document when no host is going to claim it back.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const ruleRow = () => dom.get('sheet').children.find((c) => c.dataset.ruleId === 'audit');
  findAll(ruleRow(), 'step-when')[0]._listeners.click();
  const box = dom.get('inspector');
  assert.ok(contains(ruleRow(), box), 'docked in the row that was clicked');
  // The rule leaves the file, so the row that held the editor cannot be rebuilt.
  api.state.policy = Object.assign(sheetPolicy(),
    { rules: sheetPolicy().rules.filter((r) => r.id !== 'audit') });
  api.renderSheet();
  assert.equal(ruleRow(), undefined, 'the row really is gone');
  assert.equal(contains(dom.get('sheet'), box), false,
    'and the editor was not left behind inside the discarded row');
  assert.ok(contains(dom.get('panel-tarefas'), box),
    'it is home in the panel it is declared in, still reachable by id');
});

test('the catch-all rows dock their editor too, not just the numbered ones', () => {
  // default and fail_safe live in #sheetTailList, not in the sheet, and they are
  // editable binds. They were the two rows whose click sent the editor furthest
  // away — the tail is the LAST thing above the inspector's declared home.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const tailRow = () => dom.get('sheetTailList').children.find((c) => c.dataset.ruleId === '__default');
  assert.ok(tailRow(), 'the tail rendered the default row');
  const cell = findAll(tailRow(), 'step-dest')[0] || findAll(tailRow(), 'step-when')[0];
  assert.ok(cell, 'the catch-all row carries a clickable value');
  cell._listeners.click();
  assert.equal(api.state.selected, 'default', 'the click opened the default\'s editor');
  assert.ok(contains(tailRow(), dom.get('inspector')),
    'the editor docks in the catch-all row that was clicked');
});

test('a group picked from the model ladder edits inside the ladder, not in a hidden panel', () => {
  // The ladder lives on Modelos and its "Trocar por um modelo do catálogo" button
  // called pickBind WITHOUT selectTab('tarefas') — so it filled a node inside the
  // HIDDEN panel-tarefas and the operator saw nothing whatsoever. Docking is what
  // fixes it: the editor goes where the click was, whichever tab that is.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.state.capabilities = capModels();
  api.renderLadder();
  const group = () => findAll(dom.get('ladder'), 'tier')[0];
  const block = group();
  assert.ok(block, 'the ladder rendered its groups');
  // The group's NAME is the affordance. Before this it had none: the only way
  // into a queue editor from Modelos was the unknown-model swap button, so a
  // group whose ids the catalogue all knew could not be edited from the screen
  // that shows it at all.
  const name = findAll(block, 'tier-name')[0];
  assert.ok(name, 'the group carries its name');
  assert.ok(name.classList.contains('is-edit'), 'and the name wears the editable affordance');
  name._listeners.click();
  const box = dom.get('inspector');
  assert.equal(api.state.selected, 'tier:T3', 'the click opens that group\'s queue editor');
  assert.ok(byLabel(box, 'Modelo'), 'the group editor rendered');
  // Re-read: pickBind re-renders the ladder, so the docked block is the
  // replacement for the one whose name was clicked.
  assert.ok(contains(group(), box),
    'and it is inside the ladder entry the operator was looking at');
});

// EVERY CONFIGURABLE VALUE ON THE ROW OPENS, or the ones that do cannot be
// trusted. Quando and Vai para were clickable and Primeira tentativa was not,
// which does not read as "that column is derived" — it reads as clicking being
// unreliable, and an operator who tries the inert one first concludes the screen
// does not edit. So the cell opens WHAT IT NAMES: a group's queue when the
// attempt comes from a group, the rule when the rule names the model itself, and
// the classifier when the answer is decided at runtime. A refusal names nothing,
// and stays inert.
test('the first-attempt cell opens the group it names, and a refusal\'s stays inert', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy(); // rules: 'deep' -> T3, 'no' -> deny
  api.state.capabilities = capModels();
  api.renderSheet();
  const first = findAll(dom.get('sheet').children[0], 'step-first')[0];
  assert.ok(first, 'the column exists');
  assert.ok(first.classList.contains('is-edit'),
    'an attempt that comes from a group wears the editable affordance');
  first._listeners.click();
  assert.equal(api.state.selected, 'tier:T3', 'and it opens that group\'s queue');
  assert.ok(contains(dom.get('sheet').children[0], dom.get('inspector')),
    'docked in the row the operator was reading, not in the ladder on another tab');
  const deny = findAll(dom.get('sheet').children[1], 'step-first')[0];
  assert.equal(deny.classList.contains('is-edit'), false,
    'a refusal has no first attempt, so its cell is not dressed as a control');
});

test('a first attempt the rule names itself opens the rule; one decided at runtime opens the classifier', () => {
  // The classifier is the second half of this: it is in EDITABLE and
  // renderInspector has always had a branch for it, and NOTHING opened it —
  // no pickBind call site named the bind. The editor existed and was
  // unreachable, so the classifier's model was a JSON-only field in practice.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const rowFor = (id) => dom.get('sheet').children.find((c) => c.dataset.ruleId === id);
  const fixed = findAll(rowFor('fixed'), 'step-first')[0];
  assert.ok(fixed.classList.contains('is-edit'), 'a rule-named model is clickable');
  fixed._listeners.click();
  assert.equal(api.state.selected, 'rule:fixed',
    'the rule names the model, so the rule is what opens');
  const ask = findAll(rowFor('ask'), 'step-first')[0];
  assert.ok(ask.classList.contains('is-edit'), '"decide na hora" is clickable');
  ask._listeners.click();
  assert.equal(api.state.selected, 'classifier',
    'a runtime decision opens the model that makes it');
  assert.ok(byLabel(dom.get('inspector'), 'Provedor'),
    'and that editor really is the classifier\'s');
  assert.ok(contains(rowFor('ask'), dom.get('inspector')), 'docked in the row that asked');
});

test('the edit affordance is as legible as the value it marks', () => {
  // MEASURED on the standalone console at 1440×900, 2026-09-02: the dotted
  // underline was var(--line-strong) = #33333d on the page's #0a0a0c — a contrast
  // ratio of 1.58:1 — while the words it underlines (var(--muted) = #9a9aa8) sit
  // at 7.12:1. The mark that says "you can change this" was 4.5× harder to see
  // than the value it marked, so on screen the cells read as inert text.
  //
  // That is the other half of "I don't know how to edit things here": docking the
  // editor fixes the click that appeared to do nothing, and this fixes not knowing
  // there was anything to click. A hairline token is for BORDERS, where 1.58:1 is
  // exactly the point; it is the wrong family for a mark that has to be READ.
  const { style } = consoleStyle();
  const rule = style.match(/\.step-when\.is-edit,[\s\S]*?{([^}]*)}/);
  assert.ok(rule, 'the affordance rule is gone or reformatted — this test cannot see it');
  const deco = rule[1].match(/text-decoration-color:\s*var\((--[\w-]+)\)/);
  assert.ok(deco, 'the affordance takes its underline colour from a token, never a literal');
  assert.ok(!/^--line/.test(deco[1]),
    `the underline must not use a hairline token (found ${deco[1]}): measured 1.58:1 `
    + 'against the page background, against 7.12:1 for the text above it');
  assert.equal(deco[1], '--muted',
    'it is the text\'s own weight, so the mark is exactly as readable as the value');
});

test('every cell that opens an editor is styled as one, in all three states', () => {
  // The affordance is THREE selector lists (base, :hover, :focus-visible) and a
  // new editable cell has to enter all three. Nothing enforced that, so the way
  // to add a clickable cell with no dotted underline, no hover and no focus ring
  // was to simply add it — which is invisible in review and invisible on screen.
  // This walks the rendered surfaces, collects what editAffordance actually
  // marked, and requires each class in each list.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.state.capabilities = capModels();
  api.state.status = { enabled: true };
  api.state.blocklist = { manual_bans: [], breaker_cooldowns: [], fallback_chain: [] };
  api.renderSheet();
  api.renderLadder();
  api.renderHealth();
  // editAffordance marks with classList.add, and the stub keeps classList apart
  // from className — so this reads BOTH, the way outerHtml does. findAll sees only
  // className and would report every surface as unmarked.
  const marked = new Set();
  const walk = (node) => {
    const classes = [...String(node.className || '').split(/\s+/).filter(Boolean),
      ...((node.classList && node.classList._set) || [])];
    if (classes.includes('is-edit')) {
      classes.filter((c) => c !== 'is-edit').forEach((c) => marked.add(c));
    }
    (node.children || []).forEach(walk);
  };
  [dom.get('sheet'), dom.get('sheetTailList'), dom.get('ladder'), dom.get('healthFacts')]
    .forEach(walk);
  // Non-vacuity first: an empty walk would pass every assertion below.
  assert.ok(marked.size >= 3,
    `expected several kinds of editable cell, found ${[...marked].join(', ') || 'none'}`);
  const { style } = consoleStyle();
  const lists = {
    base: style.match(/((?:\s*\.[\w-]+\.is-edit,)+\s*\.[\w-]+\.is-edit)\s*{\s*cursor: pointer/),
    hover: style.match(/((?:\s*\.[\w-]+\.is-edit:hover,)+\s*\.[\w-]+\.is-edit:hover)\s*{/),
    focus: style.match(/((?:\s*\.[\w-]+\.is-edit:focus-visible,)+\s*\.[\w-]+\.is-edit:focus-visible)\s*{/),
  };
  Object.entries(lists).forEach(([which, hit]) => {
    assert.ok(hit, `the ${which} affordance rule must exist to be checked`);
    marked.forEach((cls) => {
      assert.ok(hit[1].includes(`.${cls}.is-edit`),
        `.${cls} opens an editor, so it belongs in the ${which} affordance rule`);
    });
  });
});

test('the editor paints as a panel only when it is holding an edit', () => {
  // #inspector matched ZERO css rules: even scrolled to, it was loose fields under
  // a bare <h2>, which is not a thing a reader identifies as "the editor". It gets
  // a card — and only when non-empty, because an empty bordered box parked in the
  // row would read as a control that does nothing.
  const { style } = consoleStyle();
  assert.match(style, /#inspector:not\(:empty\)\s*{/,
    'the card paints on the non-empty selector, never on the parked empty node');
  const card = style.match(/#inspector:not\(:empty\)\s*{([^}]*)}/)[1];
  assert.match(card, /border/, 'it has an edge, so it reads as attached to the row');
  assert.match(card, /var\(--/, 'and it takes its colours from the tokens, never a literal');
});

test('a write needs no mode: writable() does not check any arming', () => {
  // An apply that is otherwise valid has to be possible without the operator
  // first arming a UI toggle, because the toggle never protected anything —
  // the server's token, CSRF, base_hash and lint are the only gates.
  const { api } = loadConsole({ csrfToken: 'tok' });
  const msg = { textContent: '', className: '' };
  assert.equal(api.writable(msg, 'Apply'), true,
    'no mode to arm and no refusal for not arming it');
  assert.equal(msg.textContent, '', 'and it produces no refusal message');
});

test('an open editor does not report the router as degraded', () => {
  // The Tarefas dot used to go amber whenever the editor was open, so the console
  // claimed a machine problem because someone had clicked a button — and amber is
  // the colour that means "this needs your attention".
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = { rules: [{ id: 'r1' }] };
  api.state.status = { validation_errors: [] };

  // The generated state node is absent when there is nothing to say; its class
  // is the observable condition when policy data exists.
  const tarefasState = () => dom.get('stateTarefas').children[0]?.className || '';

  api.renderInspector({ id: 'rule:r1', name: 'r1', bind: 'rule', ruleIndex: 0 });
  api.renderRail();
  assert.doesNotMatch(tarefasState(), /is-degraded/,
    `an open editor is not a degradation, got "${tarefasState()}"`);
  assert.match(tarefasState(), /is-alive/, 'a valid policy is alive while being edited');

  // A policy the router cannot parse IS one, and must still be reported.
  api.state.status = { validation_errors: ['rule r1: unknown field'] };
  api.renderRail();
  assert.match(tarefasState(), /is-degraded/, 'an invalid policy must still show amber');
});

test('without a session write token, saving is refused with the reason', () => {
  // Measured against the live proxy: unsafe methods without X-Hermes-CSRF-Token
  // come back 403 "Session expired". The token exists only on pages the WebUI
  // renders, so a standalone console must say where editing does work.
  const { api } = loadConsole({ csrfToken: '' });
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

test('a non-JSON 200 policy body is a failed read, not a synthetic policy document', async () => {
  const html = '<!DOCTYPE html><html><body>sidecar failed</body></html>';
  const { api, dom } = loadConsole({
    fetch: (url) => Promise.resolve({
      ok: true, status: 200,
      headers: { get: () => url.endsWith('/policy') ? 'text/html' : 'application/json' },
      text: () => Promise.resolve(url.endsWith('/policy') ? html : '{}'),
    }),
  });

  await api.fetchAll();

  assert.equal(api.state.policy, null, 'the policy slot remains untouched when no policy was read');
  assert.equal(api.state.readFailures['/policy'].status, 200);
  assert.equal(api.state.readFailures['/policy'].malformed, true);
  assert.equal(dom.get('policyEditor').value, '', 'an error page never reaches the editor');
  assert.match(dom.get('jsonFoot').textContent, /\/policy.*HTTP 200.*não é JSON/);
  assert.doesNotMatch(dom.get('jsonFoot').textContent, /JSON válido/);
});

test('a proxy HTML 502 preserves the prior policy and keeps only a bounded diagnostic', async () => {
  const html = `<!DOCTYPE html><html><body>${'x'.repeat(500)}DONT_LEAK</body></html>`;
  const before = { rules: [{ id: 'still-here' }] };
  const { api } = loadConsole({
    fetch: (url) => Promise.resolve({
      ok: !url.endsWith('/policy'), status: url.endsWith('/policy') ? 502 : 200,
      headers: { get: () => url.endsWith('/policy') ? 'text/html' : 'application/json' },
      text: () => Promise.resolve(url.endsWith('/policy') ? html : '{}'),
    }),
  });
  api.state.policy = before;

  await api.fetchAll();

  const failure = api.state.readFailures['/policy'];
  const words = api.absence('empty', '/policy');
  assert.equal(api.state.policy, before, 'a failed refresh must not erase the policy already on screen');
  assert.equal(failure.status, 502);
  assert.match(words, /\/policy.*HTTP 502.*não é JSON/);
  assert.ok(words.length < html.length, 'the diagnostic must not carry the whole proxy document');
  assert.doesNotMatch(words, /DONT_LEAK/);
});

test('a JSON policy still fills the editor and keeps its document counts', async () => {
  const policy = { rules: [{ id: 'r1' }], tiers: { T1: { model: 'm', provider: 'p' } } };
  const { api, dom } = loadConsole({
    fetch: (url) => Promise.resolve({
      ok: true, status: 200,
      headers: { get: () => 'application/json' },
      text: () => Promise.resolve(JSON.stringify(url.endsWith('/policy') ? policy : {})),
    }),
  });

  await api.fetchAll();

  assert.deepEqual(api.state.policy, policy);
  assert.match(dom.get('policyEditor').value, /"r1"/);
  assert.match(dom.get('jsonFoot').textContent, /JSON válido.*1 regra, 1 grupo/);
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
  // Card t_3ba979a1: the diff is now the two DOCUMENTS — the file this
  // screen read vs the plan's merged result — rendered as − / + lines with
  // the count head and the whole-list replacement note. The old pin on the
  // server's raw text ('+ new') is superseded: the server's text is YAML
  // and cannot say "9 rules replace 8" — the two documents can.
  const antes = { rules: Array.from({ length: 8 }, (_, i) => ({ id: `r${i}` })), fail_safe: { strong: true } };
  const depois = { rules: Array.from({ length: 9 }, (_, i) => ({ id: `r${i}` })), fail_safe: { strong: false } };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      posted.push(url);
      if (url.endsWith('/policy')) {
        // The disk matches the snapshot the screen rendered.
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(antes)) });
      }
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ valid: true, policy: depois, diff: '- old\n+ new' })),
      });
    },
  });
  api.state.policy = antes;
  const msg = { textContent: '', className: '' };
  // The diff node is a DOM node: renderJsonDiff builds −/+ rows inside it
  // (append/children), which the old text-only fixture did not carry.
  const diff = dom.get('jsonDiff');
  await api.doPreview({ rules: [] }, msg, diff);

  assert.equal(posted.length, 3, 'preview must not write: lint, freshness, plan');
  assert.match(posted[0], /\/lint$/, '§5.5: the preview revalidates first');
  assert.match(posted[1], /\/policy$/, 'then the staleness guard reads');
  assert.match(posted[2], /\/plan$/);
  assert.equal(diff.hidden, false);
  const said = flat(diff);
  assert.match(said, /2 chaves mudam · 1 lista substituída inteira/,
    'the head counts the change the preview exists to show');
  assert.match(said, /"rules": \[ 8 itens \]/);
  assert.match(said, /"rules": \[ 9 itens \]/);
  assert.match(said, /Quem manda 9 itens troca os 8 que estão lá/,
    'the whole-list replacement is said out loud');
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

test('Decisões keeps a selected decision hour in its own replay, not in the global clock', () => {
  // A replay is historical. The global price band is deliberately absent here so
  // it cannot claim a current cost alongside the decision's recorded instant.
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;
  api.state.tab = 'decisoes';
  api.state.replay = {
    id: 'r1', at: 0, steps: [], plan: null,
    recordedAt: new Date(TRACE_AT * 1000),
  };
  api.renderClock();
  assert.equal(dom.get('clockbar').hidden, true);
  assert.equal(dom.get('clockNow').textContent, '');

  // Moving back to a time-aware screen restores the live present clock.
  api.state.tab = 'modelos';
  api.renderClock();
  assert.equal(dom.get('clockbar').hidden, false);
  assert.equal(dom.get('clockNow').textContent, '07:14 UTC');
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
  // page load is deleted by the next Apply. The diff contains that deletion.
  // The old two-click path made it unavoidable — Validate rendered it and Apply was
  // unreachable until it had. One-click Apply was computing it and throwing it away.
  // Card t_3ba979a1: the deletion renders as the whole-list swap (the merged
  // policy went from two rules to one), because that is what the merge does —
  // the item-by-item view would be the lie this card exists to prevent.
  const antes = { rules: [{ id: 'URGENT-block-prod' }, { id: 'keep' }] };
  const depois = { rules: [{ id: 'keep' }] };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => Promise.resolve({
      ok: true, status: 200,
      text: () => Promise.resolve(JSON.stringify(
        url.endsWith('/policy')
          ? antes // the disk matches the snapshot the screen rendered
          : (url.endsWith('/plan')
            ? { valid: true, policy: depois, base_hash: 'h', diff: '-  - id: URGENT-block-prod\n' }
            : { ok: true }),
      )),
    }),
  });
  api.state.policy = antes;
  const diff = dom.get('jsonDiff');
  await api.doApply('/apply', { textContent: '', className: '' }, { rules: [] }, diff);

  assert.equal(diff.hidden, false, 'the operator must see the diff they authorised');
  const said = flat(diff);
  assert.match(said, /"rules": \[ 2 itens \]/,
    'including a concurrent edit this write would remove — the list swap names it');
  assert.match(said, /"rules": \[ 1 item \]/,
    'and the side that remains, one rule');
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

test('Salvar is disarmed while the text is not JSON, and only then (card t_8e79466b)', () => {
  // The whole-file editor validates LIVE — the scanner IS the validator — and
  // a button that exists solely to refuse the click must not be armed. The
  // mode-era rule this test used to pin ("no code path leaves Apply inert")
  // is superseded for the SYNTAX gate: the lint gate still refuses by ABSENCE
  // (§3.4(a), DESIGN.md:435-463 — a policy that parses but fails lint), while
  // the syntax gate refuses by disarming — the two gates refuse differently
  // because the two failures are different: lint is a verdict on a document,
  // and a broken text is not a document at all.
  const src = fs.readFileSync(sourcePath, 'utf8');
  const markup = src.slice(0, src.indexOf('<script>'));
  assert.doesNotMatch(markup, /jsonApply[^>]*disabled/,
    'no disabled attribute on Apply in the markup — the arming is a live property');
  assert.doesNotMatch(markup, /jsonRevert[^>]*disabled/,
    'nor on Revert — the two-click rule is the only gate there');
  const { api, dom } = loadConsole({ keepWire: true, csrfToken: 'tok' });
  const ta = dom.get('policyEditor');
  const apply = dom.get('jsonApply');
  assert.equal(apply.disabled, true, 'boot arms nothing: an empty document is not JSON');
  ta.value = '{ "a": 1, }';
  ta._listeners.input();
  assert.equal(apply.disabled, true, 'a trailing comma leaves Salvar disarmed');
  ta.value = '{ "a": 1 }';
  ta._listeners.input();
  assert.equal(apply.disabled, false, 'fixing the text arms it again');
  ta.value = '{ "a": ';
  ta._listeners.input();
  assert.equal(apply.disabled, true, 'an unfinished value disarms it again');
  assert.ok(!dom.get('jsonRevert').disabled, 'the destructive one is two clicks away, never greyed out');
});

// ── the JSON scanner: tokens AND the error in one pass ───────────────────
// JSON.parse reports a different message per engine (and often no position),
// so the console's live validation is OUR scanner, in OUR words. Every token
// type and every error code is pinned here with exact spans and exact
// line/column — a scanner that "mostly" validates is exactly the bug this
// card exists to kill, so the table is exhaustive.

test('tokenizeJson emits every token type, and the tokens rebuild the text', () => {
  const { api } = loadConsole();
  const text = '{ "chave": "texto", "n": 12.5, "b": true, "nulo": null }';
  const { tokens, erro } = api.tokenizeJson(text);
  assert.equal(erro, null);
  const kinds = new Set(tokens.map((t) => t.tipo));
  for (const kind of ['chave', 'texto', 'numero', 'palavra', 'pontuacao', 'espaco']) {
    assert.ok(kinds.has(kind), `the sample must exercise ${kind}`);
  }
  // The tokens partition the document exactly: concatenating their slices
  // rebuilds the input, or the mirror would drop or duplicate text.
  assert.equal(tokens.map((t) => text.slice(t.inicio, t.fim)).join(''), text,
    'every character belongs to exactly one token');
  const chave = tokens.find((t) => t.tipo === 'chave');
  assert.equal(text.slice(chave.inicio, chave.fim), '"chave"');
  assert.deepEqual(plain({ inicio: chave.inicio, fim: chave.fim }), { inicio: 2, fim: 9 });
  const numero = tokens.find((t) => t.tipo === 'numero');
  assert.equal(text.slice(numero.inicio, numero.fim), '12.5');
  const palavra = tokens.find((t) => t.tipo === 'palavra');
  assert.equal(text.slice(palavra.inicio, palavra.fim), 'true');
});

test('tokenizeJson keeps accents and escaped quotes as ordinary string content', () => {
  const { api } = loadConsole();
  const text = '{ "política": "café", "chave \\"com\\" aspas": "a\\nb" }';
  const { tokens, erro } = api.tokenizeJson(text);
  assert.equal(erro, null, 'accents and escaped quotes are legal string content');
  const chaves = tokens.filter((t) => t.tipo === 'chave').map((t) => text.slice(t.inicio, t.fim));
  assert.ok(chaves.includes('"política"'), 'the accented key survives whole');
  assert.ok(chaves.includes('"chave \\"com\\" aspas"'), 'the escaped quotes stay inside the key');
  // And the escapes are the real thing: JSON.parse agrees with the scanner.
  assert.equal(JSON.parse(text).política, 'café');
});

test('tokenizeJson names each error code with the exact line and column', () => {
  const { api } = loadConsole();
  // The column points at the character where the scanner gives up — or at
  // the end of the document, past the last character.
  const cases = [
    // virgula-sobrando points at the COMMA, not at the closer that follows
    // it: engines report the closer (where the parser gives up), and the
    // operator walks to that line and finds no comma — it is on the line
    // above (card t_5fb727b5, operator's measurement). Other codes keep
    // pointing where they are.
    ['{ "a": 1, }', 'virgula-sobrando', 1, 9],    // the comma's own column
    ['[1, 2,]', 'virgula-sobrando', 1, 6],
    ['{ "a" 1 }', 'chave-sem-valor', 1, 7],       // a key that never saw its ':'
    ['{ "a"', 'chave-sem-valor', 1, 6],           // ... at the end of the document
    ['{ "a": "ab', 'texto-nao-fechado', 1, 11],   // the string never closed
    ['{ "a": 01 }', 'numero-invalido', 1, 8],     // leading zero
    ['{ "a": 1.2.3 }', 'numero-invalido', 1, 8],  // a second dot
    ['{ a: 1 }', 'caractere-inesperado', 1, 3],   // a bare word where a key belongs
    ['{ "a": x }', 'caractere-inesperado', 1, 8], // a word that is not true/false/null
    ['{ "a": 1', 'fim-inesperado', 1, 9],         // the object never closed
    ['', 'fim-inesperado', 1, 1],                 // an empty document
  ];
  for (const [text, codigo, linha, coluna] of cases) {
    const { erro } = api.tokenizeJson(text);
    assert.ok(erro, `"${text}" must fail`);
    assert.equal(erro.codigo, codigo, `"${text}" must say ${codigo}, got ${erro && erro.codigo}`);
    assert.deepEqual(plain({ linha: erro.linha, coluna: erro.coluna }), { linha, coluna },
      `"${text}" must report the exact position`);
  }
});

test('every error code has its pt-BR phrase — the footer can never say "undefined"', () => {
  const { api } = loadConsole();
  // The comp's own example is the first line: the same words, the same order.
  const expected = {
    'virgula-sobrando': 'Não é JSON válido — linha 9, coluna 48: vírgula sobrando',
    'chave-sem-valor': 'Não é JSON válido — linha 9, coluna 48: chave sem valor',
    'texto-nao-fechado': 'Não é JSON válido — linha 9, coluna 48: texto não fechado',
    'numero-invalido': 'Não é JSON válido — linha 9, coluna 48: número inválido',
    'caractere-inesperado': 'Não é JSON válido — linha 9, coluna 48: caractere inesperado',
    'fim-inesperado': 'Não é JSON válido — linha 9, coluna 48: fim inesperado',
  };
  for (const [code, phrase] of Object.entries(expected)) {
    assert.equal(api.jsonFootPhrase({ linha: 9, coluna: 48, codigo: code }), phrase, code);
  }
});

test('a 200+ line document reports an error far from the first line', () => {
  const { api } = loadConsole();
  // 200 entries then a trailing comma: the error lands on the COMMA's own
  // line, 201 — not on the first line, where a scanner that validated by
  // regex over the head of the document would give up, and not on line
  // 202, where the closer that follows it sits (the position engines
  // report and the operator walks to without finding a comma).
  const entries = Array.from({ length: 200 }, (_, i) => `  "k${i}": ${i}`);
  const text = '{\n' + entries.join(',\n') + ',\n}';
  assert.equal(text.split('\n').length, 202, 'the fixture really is 200+ lines');
  const { tokens, erro } = api.tokenizeJson(text);
  assert.equal(erro.codigo, 'virgula-sobrando');
  // Line 201 is `  "k199": 199,` — the comma is the 14th character.
  assert.deepEqual(plain({ linha: erro.linha, coluna: erro.coluna }), { linha: 201, coluna: 14 });
  // The highlight still covers everything before the error.
  const lastChave = tokens.filter((t) => t.tipo === 'chave').pop();
  assert.equal(text.slice(lastChave.inicio, lastChave.fim), '"k199"');
});

// ── the overlay editor's CSS contract ─────────────────────────────────────
// The mirror paints each character at the same pixel as the textarea's text:
// that is the whole technique, so the metrics are pinned as a static scan —
// a drift would shift the first line and nothing but the eye would catch it.

test('the mirror and the textarea share ONE metric rule — alignment cannot drift', () => {
  const { style } = consoleStyle();
  const rules = [...style.matchAll(/([^{}]+)\{([^}]*)\}/g)];
  const shared = rules.find((m) => m[1].includes('.editor,') && m[1].includes('.editor-mirror'));
  assert.ok(shared, 'one rule must name both the textarea and the mirror');
  // line-height rides inside the `font` shorthand (var(--t-small)/1.6), so
  // `font` is the property to pin; the METRICS scan below still refuses a
  // literal line-height on the mirror anywhere else.
  for (const prop of ['font', 'letter-spacing', 'padding', 'border', 'white-space', 'tab-size']) {
    assert.ok(shared[2].includes(prop), `the shared rule declares ${prop}`);
  }
  // And no OTHER top-level rule may style a mirror metric without naming the
  // textarea in the same selector.
  const METRICS = ['font-size', 'line-height', 'letter-spacing', 'white-space', 'tab-size', 'padding'];
  // `.editor` as a STANDALONE selector — a bare `.includes('.editor')` would
  // be fooled by `.editor-mirror` itself (the prefix).
  const namesEditor = (sel) => /(^|,)\s*\.editor(?=[\s,{])/.test(sel);
  for (const m of rules) {
    if (!m[1].includes('.editor-mirror')) continue;
    for (const metric of METRICS) {
      if (m[2].includes(metric)) {
        assert.ok(namesEditor(m[1]),
          `a rule naming .editor-mirror must name .editor too when it sets ${metric} — got "${m[1].trim()}"`);
      }
    }
  }
  // The touch guard lifts the editor to 16px under a coarse pointer (iOS
  // zooms in below 16px and never zooms back); it must lift the mirror and
  // the gutter with it, or the overlay misaligns exactly on the devices
  // that cannot zoom back out.
  const touch = style.slice(style.indexOf('@media (hover: none) and (pointer: coarse)'));
  const guard = touch.match(/([^{}]*)\{\s*font-size: max\(16px, 1em\);\s*\}/);
  assert.ok(guard, 'the touch guard that lifts the editor to 16px still exists');
  for (const sel of ['.editor', '.editor-mirror', '.editor-gutter']) {
    assert.ok(guard[1].includes(sel), `the 16px touch guard names ${sel} — the mirror must grow with the textarea`);
  }
});

test('the live editor is an overlay: visible caret, transparent text and chrome', () => {
  const { style } = consoleStyle();
  assert.match(style, /caret-color: var\(--accent-text\)/,
    'the caret is painted — transparent text still needs a visible caret');
  const editor = style.match(/\.editor \{[^}]*\}/)[0];
  assert.match(editor, /color: transparent/, 'the textarea text is transparent so the mirror shows through');
  assert.match(editor, /background: transparent/, 'the textarea background is transparent too');
  assert.match(style, /\.editor-mirror \{[^}]*pointer-events: none/, 'the mirror never eats a click');
  assert.match(style, /\.editor-gutter \{[^}]*pointer-events: none/, 'the gutter never eats a click or the wheel');
});

// ── the live view: mirror, gutter, footer, arming, one scan ───────────────

test('typing paints the mirror, the gutter and the footer from one scan', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  const valid = '{"rules": [{"id": "r1"}], "tiers": {"T1": {"model": "glm-4.7", "provider": "zai"}}, "price_windows": {"glm-4.7@zai": [{"hours_utc": [20, 22], "multiplier": 3.0}]}}';
  ta.value = valid;
  ta._listeners.input();
  // Footer: one line, one rule, one tier, one provider with a window.
  assert.equal(dom.get('jsonFoot').textContent,
    'JSON válido · 1 linha · 1 regra, 1 grupo, 1 provedor com janela');
  assert.equal(dom.get('jsonFoot').className, 'editor-foot ok');
  // Mirror: one row per source line; the first row carries the tokens.
  const mirror = dom.get('policyMirror');
  assert.equal(mirror.children.length, 1, 'one source line, one row');
  assert.equal(mirror.children[0].className, 'code-line', 'no error, no mark');
  const chave = mirror.children[0].children.find((c) => c.className === 'tk-chave');
  assert.ok(chave && chave.textContent === '"rules"', 'the key is painted as a chave token');
  const numero = mirror.children[0].children.find((c) => c.className === 'tk-numero');
  assert.ok(numero && numero.textContent === '20', 'a window hour is a numero token');
  // Gutter: one number per line, none marked.
  const gutter = dom.get('policyLines');
  assert.equal(gutter.children.length, 1);
  assert.equal(gutter.children[0].textContent, '1');
  assert.equal(gutter.children[0].className, '');
  assert.equal(dom.get('jsonApply').disabled, false, 'Salvar is armed');

  // Break it: the same scan names the error, marks the row and disarms Salvar.
  ta.value = '{ "a": 1, }';
  ta._listeners.input();
  assert.equal(dom.get('jsonFoot').textContent,
    'Não é JSON válido — linha 1, coluna 9: vírgula sobrando');
  assert.equal(dom.get('jsonFoot').className, 'editor-foot bad');
  assert.match(dom.get('policyMirror').children[0].className, /bad/,
    'the line that carries the error is marked');
  assert.equal(dom.get('policyLines').children[0].className, 'err',
    'the gutter names the same line');
  assert.equal(dom.get('jsonApply').disabled, true, 'Salvar is disarmed');

  // The mark leaves the moment the text changes again.
  ta.value = '{ "a": 1 }';
  ta._listeners.input();
  assert.doesNotMatch(dom.get('policyMirror').children[0].className, /bad/,
    'a change clears the error mark');
  assert.equal(dom.get('jsonFoot').textContent, 'JSON válido · 1 linha · 0 regras, 0 grupos, 0 provedores com janela');
});

test('a trailing comma reports ITS OWN line, and the gutter marks that line', () => {
  // Card t_5fb727b5, operator's measurement: a comma at the end of line N
  // used to report line N+1 (the closer, where the parser gives up) — the
  // operator walked to N+1 and found no comma. Both the footer phrase and
  // the gutter read erro.linha, so one fix moves both.
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  ta.value = '{\n  "a": 1,\n  "b": 2,\n}';
  ta._listeners.input();
  assert.equal(dom.get('jsonFoot').textContent,
    'Não é JSON válido — linha 3, coluna 9: vírgula sobrando');
  const gutter = dom.get('policyLines');
  assert.equal(gutter.children.length, 4, 'four source lines, four gutter rows');
  assert.equal(gutter.children[2].className, 'err', 'the gutter marks the COMMA line');
  assert.equal(gutter.children[3].className, '', 'the closer line is not the mark');
  assert.match(dom.get('policyMirror').children[2].className, /bad/,
    'the mirror marks the same line');
  assert.equal(dom.get('policyMirror').children[3].className, 'code-line',
    'the closer row stays clean');
});

test('the valid footer counts come from the document in the box, not from state', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  // The state says "no rules, no tiers" — the footer must ignore it and read
  // the text: the whole point of the editor is that the TEXT is the policy.
  api.state.policy = { rules: [], tiers: {} };
  const text = '{\n  "rules": [{"id": "a"}, {"id": "b"}],\n'
    + '  "tiers": {"T1": {"model": "glm-4.7", "provider": "zai"}, "T2": {"model": "deepseek-v4-pro", "provider": "deepseek"}},\n'
    + '  "price_windows": {"glm-4.7@zai": [], "deepseek-v4-pro": []}\n}';
  const ta = dom.get('policyEditor');
  ta.value = text;
  ta._listeners.input();
  assert.equal(dom.get('jsonFoot').textContent,
    'JSON válido · 5 linhas · 2 regras, 2 grupos, 2 provedores com janela');
  // And jsonCounts is the pure reader behind it — the provider of a windowed
  // model resolves from the tiers' own hops, or from the model@vendor
  // spelling when the model is not in the tiers.
  assert.deepEqual(plain(api.jsonCounts(text, JSON.parse(text))),
    { linhas: 5, regras: 2, grupos: 2, provedoresComJanela: 2 });
});

test('the mirror and the gutter scroll with the textarea', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  const mirror = dom.get('policyMirror');
  const gutter = dom.get('policyGutter');
  ta.scrollTop = 123;
  ta.scrollLeft = 45;
  ta._listeners.scroll();
  assert.equal(mirror.scrollTop, 123, 'the mirror follows the vertical scroll');
  assert.equal(mirror.scrollLeft, 45, 'and the horizontal one');
  assert.equal(gutter.scrollTop, 123, 'the gutter follows too — the numbers stay on their lines');
});

// ── the JSON tools (card t_3ba979a1) ──────────────────────────────────────
// Formatar, Copiar, Dobrar, a busca literal e o caminho da chave — cada um é
// função pura onde dá para ser, exportada para a regra ser presa aqui em vez
// de olhada uma vez no navegador.

test('foldSummary dobra valor composto e recusa os vazios', () => {
  const { api } = loadConsole();
  assert.equal(api.foldSummary(Array.from({ length: 14 }, (_, i) => i)), '[ 14 itens ]');
  assert.equal(api.foldSummary({ T1: 1, T2: 2, T3: 3, T4: 4 }), '{ 4 chaves — T1, T2, T3, T4 }');
  assert.equal(api.foldSummary([]), null, 'lista vazia não dobra — não há o que esconder');
  assert.equal(api.foldSummary({}), null, 'objeto vazio também não');
  assert.equal(api.foldSummary('texto'), null, 'nem escalar');
  // Mais de quatro chaves: nomeia as quatro primeiras e para.
  assert.equal(api.foldSummary({ a: 1, b: 2, c: 3, d: 4, e: 5 }), '{ 5 chaves — a, b, c, d, … }');
});

test('dobrar e expandir NUNCA alteram o texto do editor', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  const text = [
    '{',
    '  "rules": [',
    '    { "id": "a" },',
    '    { "id": "b" }',
    '  ],',
    '  "tiers": {',
    '    "T1": { "model": "glm-5.3" }',
    '  }',
    '}',
  ].join('\n');
  ta.value = text;
  ta._listeners.input();
  const mirror = dom.get('policyMirror');
  assert.equal(mirror.children.length, 9, 'tudo expandido: uma linha por linha de origem');

  dom.get('jsonFold')._listeners.click();
  assert.equal(ta.value, text, 'dobrar é só visual: o value NÃO muda');
  assert.equal(mirror.children.length, 4, 'rules e tiers viraram uma linha cada');
  assert.equal(flat(mirror.children[0]), '{');
  assert.equal(flat(mirror.children[1]), '"rules": [ 2 itens ]');
  assert.equal(flat(mirror.children[2]), '"tiers": { 1 chave — T1 }');
  assert.equal(flat(mirror.children[3]), '}');
  // A medianiz marca as linhas dobradas — quem procura a chave que sumiu
  // olha o número da linha, e o ▸ (CSS ::before) diz que há mais ali.
  const gutter = dom.get('policyLines');
  assert.equal(gutter.children[0].className, '', 'linha 1 não dobra');
  assert.equal(gutter.children[1].className, 'dobrada', 'a linha da rules');
  assert.equal(gutter.children[5].className, 'dobrada', 'a linha da tiers');

  dom.get('jsonFold')._listeners.click();
  assert.equal(ta.value, text, 'expandir tampouco');
  assert.equal(mirror.children.length, 9, 'as linhas voltam, uma por linha de origem');
});

test('texto inválido não dobra — a linha do erro tem de ficar visível', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  ta.value = '{\n  "rules": [1, 2],\n  "a": 1, }';
  ta._listeners.input();
  const mirror = dom.get('policyMirror');
  assert.ok(mirror.children.some((r) => /bad/.test(r.className)),
    'a linha do erro continua pintada');
  assert.equal(findAll(mirror, 'fold-resumo').length, 0, 'nenhuma linha dobrada enquanto o texto não é JSON');
  assert.equal(dom.get('jsonFold').disabled, true, 'e o botão não tem o que fazer');
});

test('o botão de dobra diz o que o clique FAZ', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  ta.value = '{\n  "rules": [1, 2],\n  "tiers": { "T1": { "model": "x" } }\n}';
  ta._listeners.input();
  const btn = dom.get('jsonFold');
  assert.equal(btn.textContent, 'Dobrar tudo', 'nada dobrado: o clique dobra');
  assert.equal(btn.disabled, false);
  btn._listeners.click();
  assert.equal(btn.textContent, 'Expandir tudo', 'tudo dobrado: o clique expande');
  btn._listeners.click();
  assert.equal(btn.textContent, 'Dobrar tudo', 'de volta ao expandido');
  // Sem chave composta não há o que dobrar — o botão desarma.
  ta.value = '{ "a": 1 }';
  ta._listeners.input();
  assert.equal(btn.disabled, true);
});

test("a busca é literal: 'glm-5.3' acha 7 e não acha 'glmX5.3' nem 'glm-5X3'", () => {
  const { api } = loadConsole();
  // Duas armadilhas, uma por metacaractere: o hífen (glmX5.3) e o ponto
  // (glm-5X3) — uma busca com regex casaria a segunda, onde o `.` do padrão
  // vale qualquer caractere. A literal não casa nenhuma das duas.
  const comArmadilha = `${Array.from({ length: 7 }, () => '"glm-5.3"').join(', ')}, "glmX5.3", "glm-5X3"`;
  assert.ok(comArmadilha.includes('glmX5.3'), 'a armadilha do hífen está mesmo no texto');
  assert.ok(comArmadilha.includes('glm-5X3'), 'a armadilha do ponto está mesmo no texto');
  const hits = api.findAll(comArmadilha, 'glm-5.3');
  assert.equal(hits.length, 7, `as 7 ocorrências reais, achou ${hits.length}`);
  for (const h of hits) {
    assert.equal(comArmadilha.slice(h.inicio, h.fim), 'glm-5.3', 'cada ocorrência é o literal exato');
  }
  // Numa linha de política de verdade: os irmãos com X não casam — o ponto e
  // o hífen são caracteres, não metacaracteres.
  const linha = '{ "model": "glm-5.3", "modeloX": "glmX5.3", "modeloY": "glm-5X3" }';
  const dois = api.findAll(linha, 'glm-5.3');
  assert.equal(dois.length, 1, 'nem glmX5.3 nem glm-5X3 são achados por glm-5.3');
  assert.equal(linha.slice(dois[0].inicio, dois[0].fim), 'glm-5.3');
});

test('a busca pinta forte a corrente e fraca as outras, e navega', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  const text = '{ "a": "glm-5.3", "b": "glm-5.3" }';
  ta.value = text;
  ta._listeners.input();
  const search = dom.get('jsonSearch');
  search.value = 'glm-5.3';
  search._listeners.input();
  const mirror = dom.get('policyMirror');
  assert.equal(findAll(mirror, 'hl-cur').length, 1, 'uma ocorrência corrente');
  assert.equal(findAll(mirror, 'hl').length, 1, 'as outras, fracas');
  assert.equal(dom.get('jsonSearchCount').textContent, '1 de 2');
  assert.equal(dom.get('jsonSearchCount').hidden, false);
  // Enter = próximo, Shift+Enter = anterior.
  search._listeners.keydown({ key: 'Enter', shiftKey: false, preventDefault() {} });
  assert.equal(dom.get('jsonSearchCount').textContent, '2 de 2');
  assert.equal(findAll(mirror, 'hl-cur').length, 1, 'a corrente migrou, não duplicou');
  search._listeners.keydown({ key: 'Enter', shiftKey: true, preventDefault() {} });
  assert.equal(dom.get('jsonSearchCount').textContent, '1 de 2');
  // Esc limpa.
  search._listeners.keydown({ key: 'Escape', shiftKey: false, preventDefault() {} });
  assert.equal(search.value, '', 'o campo limpa');
  assert.equal(dom.get('jsonSearchCount').hidden, true, 'a contagem some');
  assert.equal(findAll(mirror, 'hl-cur').length, 0, 'e o realce sai');
});

test('keyPathAt diz onde o cursor está, em cinco posições', () => {
  const { api } = loadConsole();
  const text = [
    '{',
    '  "rules": [',
    '    { "when": { "verb_class": { "eq": "x" } }, "model": "g1" },',
    '    { "when": { "verb_class": { "eq": "y" } }, "model": "g2" },',
    '    { "when": { "verb_class": { "eq": "z" } }, "model": "g3" },',
    '    { "when": { "utc_hour": { "gte": 20 } }, "model": "glm-5.3" }',
    '  ]',
    '}',
  ].join('\n');
  // 1. dentro de uma chave (o nome "model" da 4ª regra — a última do texto).
  assert.equal(api.keyPathAt(text, text.lastIndexOf('"model"') + 1), 'rules › 3 › model');
  // 2. dentro de um valor de texto.
  assert.equal(api.keyPathAt(text, text.indexOf('"glm-5.3"') + 2), 'rules › 3 › model');
  // 3. dentro de um número (o gte da 4ª regra — o exemplo do card, com o
  //    índice 3 do array na frente).
  assert.equal(api.keyPathAt(text, text.indexOf('20') + 1), 'rules › 3 › when › utc_hour › gte');
  // 4. dentro de uma chave de outra regra do mesmo array (a segunda).
  const segundaRegra = text.indexOf('"verb_class"', text.indexOf('"verb_class"') + 1);
  assert.equal(api.keyPathAt(text, segundaRegra + 1), 'rules › 1 › when › verb_class');
  // 5. na raiz: a chave de topo.
  assert.equal(api.keyPathAt(text, text.indexOf('"rules"') + 1), 'rules');
  // E o inverso: a posição da chave no texto formatado volta para a mesma chave.
  const pos = api.posOfPath(JSON.stringify(JSON.parse(text), null, 2), 'rules › 3 › when › utc_hour › gte');
  const novo = JSON.stringify(JSON.parse(text), null, 2);
  assert.equal(api.keyPathAt(novo, pos + 1), 'rules › 3 › when › utc_hour › gte');
});

test('o caminho da chave aparece no rodapé e some na raiz', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  const text = '{ "rules": [ { "model": "glm-5.3" } ] }';
  ta.value = text;
  ta._listeners.input();
  ta.selectionStart = text.indexOf('glm-5.3') + 2;
  ta._listeners.keyup();
  const span = dom.get('jsonPath');
  assert.equal(span.textContent, 'rules › 0 › model', 'o rodapé nomeia o lugar do cursor');
  assert.equal(span.hidden, false);
  // A frase da validade continua no rodapé — o caminho é o segundo texto, à direita.
  assert.equal(dom.get('jsonFoot').textContent,
    'JSON válido · 1 linha · 1 regra, 0 grupos, 0 provedores com janela');
  // Na raiz não há chave a nomear: o caminho some.
  ta.selectionStart = 1; // dentro do { de abertura
  ta._listeners.keyup();
  assert.equal(span.textContent, '', 'raiz não tem caminho');
  assert.equal(span.hidden, true);
});

test('formatar com texto inválido não muda o texto e escreve a frase', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  const broken = '{ "a": 1, }';
  ta.value = broken;
  ta._listeners.input();
  dom.get('jsonFormat')._listeners.click();
  assert.equal(ta.value, broken, 'o texto NÃO muda');
  const msg = dom.get('jsonMsg');
  assert.match(msg.textContent, /Não formatei: Não é JSON válido — linha 1, coluna 9: vírgula sobrando/,
    'a frase diz por que não formatou, nas palavras do scanner');
  assert.match(msg.className, /bad/);
});

test('formatar reindenta e devolve o cursor à MESMA chave', () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  const text = '{ "rules": [ { "model": "glm-5.3" } ], "tiers": { "T1": { "model": "glm-4.7" } } }';
  ta.value = text;
  ta.selectionStart = text.indexOf('glm-5.3') + 2; // dentro do valor da chave model
  ta._listeners.input();
  // O clique no botão desfoca o textarea e o Chrome zera o selectionStart —
  // o handler lê o caret RASTREADO (state.caret), gravado nos eventos de
  // cursor. O keyup é o evento que o grava aqui.
  ta._listeners.keyup();
  const before = ta.value;
  dom.get('jsonFormat')._listeners.click();
  assert.notEqual(ta.value, before, 'formatou');
  assert.equal(ta.value.split('\n').length, 12, 'reindentado em 2 espaços, cada nível na sua linha');
  assert.equal(JSON.parse(ta.value).rules[0].model, 'glm-5.3', 'o conteúdo é o mesmo');
  assert.equal(JSON.parse(ta.value).tiers.T1.model, 'glm-4.7');
  // O cursor voltou pela CHAVE, não pelo índice de caractere (que mudou).
  assert.equal(api.keyPathAt(ta.value, ta.selectionStart), 'rules › 0 › model',
    'o cursor está na mesma chave de antes');
});

test('copiar usa a área de transferência e confirma no rodapé', async () => {
  const written = [];
  const { api, dom } = loadConsole({
    keepWire: true,
    navigator: { clipboard: { writeText: async (t) => { written.push(t); } } },
  });
  const ta = dom.get('policyEditor');
  ta.value = '{ "a": 1 }';
  ta._listeners.input();
  assert.equal(await api.copiarJson(), true);
  assert.deepEqual(written, ['{ "a": 1 }'], 'o texto inteiro foi para a área de transferência');
  assert.equal(dom.get('jsonFoot').textContent, 'Copiado.', 'a frase do rodapé confirma');
  assert.equal(dom.get('jsonFoot').className, 'editor-foot ok');
});

test('sem clipboard o fallback falha com a frase, não em silêncio', async () => {
  const { api, dom } = loadConsole({ keepWire: true });
  const ta = dom.get('policyEditor');
  ta.value = '{ "a": 1 }';
  ta._listeners.input();
  assert.equal(await api.copiarJson(), false);
  assert.match(dom.get('jsonFoot').textContent, /Não consegui copiar/,
    'a falha é dita, e a frase vive no rodapé como a confirmação');
});

test('o diff conta os dois lados de uma lista: 8 → 9 é substituição inteira', () => {
  const { api } = loadConsole();
  const antes = { rules: Array.from({ length: 8 }, (_, i) => ({ id: `r${i}` })), fail_safe: { strong: true } };
  const depois = { rules: Array.from({ length: 9 }, (_, i) => ({ id: `r${i}` })), fail_safe: { strong: false } };
  const rec = api.jsonDiffLines(antes, depois);
  assert.equal(rec.cabecalho, '2 chaves mudam · 1 lista substituída inteira',
    'a linha de contagem do comp: as duas chaves (rules e strong) e a lista trocada');
  assert.match(rec.nota, /Uma lista no corpo substitui a lista inteira no servidor/);
  assert.match(rec.nota, /Quem manda 9 itens troca os 8 que estão lá/);
  // Cada lista vira UMA linha − e UMA linha + com o resumo dos dois lados.
  const del = rec.linhas.find((l) => l.tipo === 'del' && l.texto.includes('"rules"'));
  const add = rec.linhas.find((l) => l.tipo === 'add' && l.texto.includes('"rules"'));
  assert.equal(del.texto, '"rules": [ 8 itens ]');
  assert.equal(add.texto, '"rules": [ 9 itens ]');
  // A chave que muda dentro do objeto aparece como −/+ no corpo do objeto.
  assert.ok(rec.linhas.some((l) => l.tipo === 'del' && /"strong": true/.test(l.texto)));
  assert.ok(rec.linhas.some((l) => l.tipo === 'add' && /"strong": false/.test(l.texto)));
  // Dois documentos iguais: nenhum cabeçalho, uma linha de contexto.
  const parado = api.jsonDiffLines({ rules: [1] }, { rules: [1] });
  assert.equal(parado.cabecalho, '');
  assert.equal(parado.linhas.length, 1);
  assert.equal(parado.linhas[0].tipo, 'ctx');
});

test('as ferramentas novas respeitam o toque e empilham a 360px', () => {
  const { style } = consoleStyle();
  const touch = style.slice(style.indexOf('@media (hover: none) and (pointer: coarse)'));
  assert.match(touch, /\.json-nav \{ [^}]*min-height: 44px/, 'os ‹ › são botões: 44px sob toque');
  const guard = touch.match(/([^{}]*)\{\s*font-size: max\(16px, 1em\);?\s*\}/);
  assert.ok(guard && guard[1].includes('.json-search-input'),
    'a busca é um input: 16px sob toque, nomeada — o seletor bare perde para a classe');
  // A 360px a barra empilha como no comp-360: coluna, busca de largura total.
  const narrow = style.slice(style.indexOf('@media (max-width: 360px)'));
  assert.match(narrow, /\.json-tools \{ [^}]*flex-direction: column/, 'a barra vira coluna');
  assert.match(narrow, /\.json-search \{ [^}]*margin-left: 0/, 'a busca deixa a direita e toma a linha');
  // A medianiz marca a linha dobrada com ▸, e o realce usa os tokens do host.
  assert.match(style, /content:\s*["']\s*▸\s*["']/, 'o ▸ da linha dobrada existe no CSS');
  assert.match(style, /\.hl-cur \{ [^}]*background: var\(--accent\)/, 'forte = accent cheio');
  assert.match(style, /\.hl \{ [^}]*background: var\(--accent-bg\)/, 'fraca = wash do accent');
});

test('revealHit centraliza a linha da ocorrência corrente', () => {
  const { api } = loadConsole();
  const editor = { clientHeight: 200, scrollTop: 0 };
  const mirror = { scrollTop: 0 };
  const gutter = { scrollTop: 0 };
  assert.equal(api.revealHit(editor, mirror, gutter, { offsetTop: 400 }), true);
  assert.equal(editor.scrollTop, 300, '400 - 200/2: a linha centraliza');
  assert.equal(mirror.scrollTop, 300, 'o espelho acompanha');
  assert.equal(gutter.scrollTop, 300, 'e a medianiz');
  // Sem métricas (o stub do DOM) não há o que rolar — e não quebra.
  assert.equal(api.revealHit({ clientHeight: 200 }, {}, {}, {}), false);
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
  // The default row moved to the catch-all tail (§5), so both lists are read.
  const words = JSON.stringify(dom.get('sheet')) + JSON.stringify(dom.get('sheetTailList'));
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

test('a rule row writes one condition line with every clause, so two conditions never merge into one', () => {
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

  // The sheet no longer draws conditions as <li> chips (comp-tarefas: the
  // conditions are TEXT on the line, joined by " · ") — the chip list still
  // exists for the other blocks that use it, but not here.
  assert.equal(findAll(dom.get('sheet'), 'chip').length, 0,
    'the sheet renders no chip list items');
  const cond = findAll(dom.get('sheet'), 'step-when');
  const condText = cond.map((n) => n.textContent).join(' | ');
  assert.ok(condText.includes('tem código · o contexto estimado passa de 400.000 tokens · o pedido envolve imagem'),
    `the clauses read as one sentence, got "${condText}"`);
  // Each clause is still its own sentence in the string — a dropped clause
  // misstates why the rule fires, and the join keeps the three apart.
  assert.ok(condText.includes('tem código'), 'the shape clause survives');
  assert.ok(condText.includes('o contexto estimado passa de 400.000 tokens'), 'and so does the context clause');
  assert.ok(condText.includes('o pedido envolve imagem'), 'and the capability clause');
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

// ── the comp-tarefas sheet: the five pieces the adversarial review named ──
// The review card t_bf751f1b blocked t_5afc3438 because the six-column grid,
// the id-in-title, the text conditions, the click-open block and the separate
// catch-all were claimed but never committed. Each of the five has its own
// test below, so the next "done" is checkable by the gate, not by trust.

test('the sheet grid is the comp\'s six columns, and the column heads name them', () => {
  const { style } = consoleStyle();
  // comp-tarefas: punho 20 · número 26 · Quando minmax(0,1fr) · Vai para 128 ·
  // Primeira tentativa 196 · Uso 8d 62 — on the row and on the head row.
  assert.match(style, /\.step \{[\s\S]*?grid-template-columns: 20px 26px minmax\(0, 1fr\) 128px 196px 62px/,
    'the six-track grid is the comp\'s own widths');
  assert.equal(style.split('grid-template-columns: 20px 26px minmax(0, 1fr) 128px 196px 62px').length - 1, 2,
    'the row and the head row share the same six tracks');
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.renderSheet();
  assert.equal(dom.get('sheetCols').hidden, false, 'the head row shows with a policy');
  // The stub DOM does not parse markup: the heads are asserted on the file,
  // the show/hide on the rendered state.
  const html = fs.readFileSync(sourcePath, 'utf8');
  for (const head of ['Quando', 'Vai para', 'Primeira tentativa', 'Uso 8d']) {
    assert.match(html, new RegExp(`>${head}<`), `the head "${head}" is in the markup`);
  }
  assert.match(html, /id="sheetCols"/, 'the head row carries the id renderSheet toggles');
  // The Primeira tentativa column names the head of the queue, with how it is
  // billed and on whose rail.
  const first = flat(findAll(dom.get('sheet'), 'step-first')[0]).replace(/\s+/g, ' ').trim();
  assert.match(first, /gpt-5\.6-terra/, 'the first attempt is the chain head');
  assert.match(first, /subscription/, 'billed as the chain declares');
  assert.match(first, /openai-codex/, 'on its own rail');
  api.state.policy = null;
  api.renderSheet();
  assert.equal(dom.get('sheetCols').hidden, true, 'no policy, no heads');
});

test('each cell is pinned to its track, and the row DOM order is the comp\'s reading order', () => {
  const { style } = consoleStyle();
  // The pins: the sheet is ONE row shape now — the grip is on every rule
  // row, because reorder is an always-on gesture, not an editing-mode one —
  // so a cell whose pin was dropped auto-places into the 20px grip track and
  // every column reads one cell off its head (measured on the live sheet
  // with the head row as the ruler: step-to landed under "Primeira
  // tentativa", step-hits under "Vai para").
  for (const [cls, col] of [['step-ord', 2], ['step-what', 3], ['step-to', 4],
    ['step-first', 5], ['step-hits', 6]]) {
    assert.match(style, new RegExp(`\\.${cls} \\{ grid-column: ${col}; \\}`),
      `the ${cls} cell is pinned to column ${col}`);
  }
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.renderSheet();
  // The DOM order is the comp's row (punho · número · Quando · Vai para ·
  // Primeira tentativa · Uso 8d), so a screen reader reads the same sentence
  // the eye reads across the line.
  const row = dom.get('sheet').children[0];
  assert.deepEqual(row.children.map((c) => c.className.split(' ')[0]).slice(0, 6),
    ['step-grip', 'step-ord', 'step-what', 'step-to', 'step-first', 'step-hits'],
    'every rule row follows the comp order, grip first — no mode decides the shape');
});

test('every rule row carries its id in the title attribute, never in the line', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const rows = dom.get('sheet').children;
  assert.ok(rows.length >= 5, 'the fixture rules are all on the sheet');
  rows.forEach((row) => {
    const id = row.dataset.ruleId;
    assert.ok(id && !String(id).startsWith('__'), `a real rule row, got ${id}`);
    assert.equal(row.title, id, 'the id lives in the title attribute');
    // The old rendering is gone: no element of the step-id class, and the
    // name cell carries no " — <id>" suffix. The KEYWORD of a condition may
    // legitimately contain the id word ("audit" in 'o texto contém "audit"'),
    // so the check is the element, not the whole row's text.
    assert.equal(findAll(row, 'step-id').length, 0, `no step-id element on row ${id}`);
    const nameCell = findAll(row, 'step-name')[0];
    assert.ok(nameCell, `the row ${id} has a name cell`);
    const escaped = id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    assert.doesNotMatch(nameCell.textContent, new RegExp(` — ${escaped}$`),
      `the name cell has no id suffix ("${nameCell.textContent}")`);
  });
  // The class that used to hold the id in the text is gone from the file. (The
  // open block still writes the id as text — that IS the comp's item 4, "regra:
  // <id>"; this check is about the ROW's own line.)
  const html = fs.readFileSync(sourcePath, 'utf8');
  assert.doesNotMatch(html, /step-id/, 'no step-id class survives anywhere');
});

test('clicking a rule row opens the whole queue, the role and the id (comp-tarefas linha 4)', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.renderSheet();
  const row = dom.get('sheet').children[0]; // 'deep' -> T3
  const open = row.children.find((c) => c.className === 'step-open');
  assert.ok(open, 'the open block exists on the row');
  assert.equal(open.hidden, true, 'closed by default');
  row._listeners.click();
  assert.equal(open.hidden, false, 'a click opens it');
  const text = flat(open);
  assert.match(text, /fila/, 'the block names the queue');
  assert.match(text, /gpt-5\.6-terra/, 'the chain head is there');
  assert.match(text, /deepseek-v4-pro/, 'and the reserves, in order');
  assert.match(text, /glm-5\.3/, 'down to the last hop');
  assert.match(text, /papel/, 'the block names the role');
  assert.match(text, /informativo neste caminho: quem cria o card escolhe o papel/,
    'the role is marked informative in this path');
  assert.match(text, /regra/, 'and the block names the rule');
  assert.match(text, /deep/, 'with the rule id in it');
  row._listeners.click();
  assert.equal(open.hidden, true, 'a second click closes it');
});

test('a rule with a declared profile shows it as the papel in the open block', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy(); // audit: then.profile = 'reviewer'
  api.renderSheet();
  const row = dom.get('sheet').children.find((c) => c.dataset.ruleId === 'audit');
  row._listeners.click();
  const open = row.children.find((c) => c.className === 'step-open');
  assert.match(flat(open), /reviewer/, 'the rule\'s own profile is the papel');
  assert.match(flat(open), /audit/, 'and the id names the rule');
});

test('default and fail-safe leave the numbered list for the tail under "se nada acima casou"', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const tail = dom.get('sheetTailList');
  assert.equal(tail.children.length, 2, 'default + fail-safe in the tail');
  assert.deepEqual(tail.children.map((r) => r.dataset.ruleId), ['__default', '__fail_safe']);
  const ords = tail.children.map((r) => r.children.find((c) => c.className === 'step-ord').textContent);
  assert.deepEqual(ords, ['—', '—'], 'the tail rows carry the comp\'s dash, not an ordinal');
  assert.equal(dom.get('sheetTail').hidden, false, 'the tail block is visible');
  // The label is static markup (the stub DOM does not parse it), so it is
  // asserted on the file.
  const html = fs.readFileSync(sourcePath, 'utf8');
  assert.match(html, /se nada acima casou/, 'the comp\'s own label is in the file');
  // The numbered list holds only the real rules (+ the ban, when one exists).
  assert.equal(dom.get('sheet').children.length, 5, 'no synthetic row in the numbered list');
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
  assert.equal(chip.textContent, 'T3 · Moderado');
  assert.equal(chip.getAttribute('aria-expanded'), 'false');
  // The comp's tier cell is the bare "T1 · Trivial" — no arrow: the "Vai
  // para" head already says where it goes, and the 128px column cannot hold
  // the arrow, the gap and the full label (measured 130px needed). The deny
  // row below keeps its arrow, so the sheet has exactly one.
  const tierDest = findAll(dom.get('sheet'), 'step-dest')[0];
  assert.equal(findAll(tierDest, 'step-arrow').length, 0, 'a tier destination carries no arrow');
  assert.match(flat(tierDest), /^T3 · Moderado/, 'the chip reads bare, like the comp');
  assert.equal(findAll(dom.get('sheet'), 'step-arrow').length, 1,
    'only the deny row keeps the arrow');
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
  assert.equal(chip.textContent, 'T4 · Difícil');
  chip._listeners.click();
  const text = flat(findAll(dom.get('sheet'), 'step-chain')[0]);
  assert.match(text, /gpt-5\.5/, 'T4\'s own primary');
  assert.match(text, /deepseek-v4-pro/, 'the hop T4 shares with T3');
  assert.match(text, /glm-5\.3/, 'and the hop it shares with T3 — the two hops T4 is not independent on');
});

test('the tier chip does not trigger the row\'s queue toggle', () => {
  // The row click opens the QUEUE (comp-tarefas: "Clique na linha para abrir
  // a fila inteira"); the chip is inside the row, so its click must not
  // toggle the queue — an operator expanding a chain is not asking for the
  // queue or for an editor.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = chipPolicy();
  api.renderSheet();
  const chip = findAll(dom.get('sheet'), 'step-tier')[0];
  assert.ok(chip, 'the chip exists on the row');
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
  assert.deepEqual(plain(api.logFreshness()),
    { stale: false, reason: null, days: 2 / 24, newest: nowS - 2 * hourS });

  // The router.yaml on disk is NEWER than the newest decision: every rule in the
  // sheet is newer than the window, so a zero hit proves nothing about it.
  api.state.routes = [{ ts: nowS - 2 * hourS }];
  api.state.status = { config_mtime: new Date(T - 1 * hourS * 1000).toISOString() };
  assert.equal(api.logFreshness().stale, true);
  assert.equal(api.logFreshness().reason, 'config');
  assert.ok(Math.abs(api.logFreshness().days - 2 / 24) < 1e-9,
    'the config case still measures the age for the banner');
  assert.equal(api.logFreshness().newest, nowS - 2 * hourS,
    'newest rides along so the banner names the age with fmtAge, not a second rounding');

  // An older sidecar reports no config_mtime: the wall-clock backstop.
  api.state.status = {};
  api.state.routes = [{ ts: nowS - 17 * dayS }, { ts: nowS - 18 * dayS }];
  assert.equal(api.logFreshness().stale, true);
  assert.equal(api.logFreshness().reason, 'age');
  assert.ok(api.logFreshness().days > 16, 'the age is measured in days');
  assert.equal(api.logFreshness().newest, nowS - 17 * dayS);

  // Nothing recorded at all.
  api.state.routes = [];
  assert.equal(api.logFreshness().stale, true);
  assert.equal(api.logFreshness().reason, 'empty');
  assert.equal(api.logFreshness().newest, null, 'no decision, no reference instant');
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
  assert.match(banner, /A política mudou depois da última decisão \(há 17d\)/,
    'a policy that moved after the log is a POLICY story, not an age story');
  assert.match(banner, /Contar as decisões antigas contra as regras novas seria mentira/,
    'and it says why the counts are refused: they would measure old decisions against new rules');
  assert.doesNotMatch(banner, /Nenhuma decisão há/,
    'the age phrasing belongs to the age reason, never to a config reason');
  assert.equal(dom.get('windowStale').hidden, false);

  // The counter REFUSES: no row carries a count or an amber "never fired" —
  // even though this corpus would have painted both rules amber before. The
  // 62px Uso column shows "sem dados"; the finding it is a verdict about
  // lives in the cell's title. Default and fail-safe sit in the catch-all
  // tail (§5), so both lists are read.
  const hits = findAll(dom.get('sheet'), 'step-hits')
    .concat(findAll(dom.get('sheetTailList'), 'step-hits'));
  // 2 rules + default + fail-safe: no blocklist row, because this policy
  // declares no manual ban (spec 1.3).
  assert.ok(hits.length >= 4, '2 rules + default + fail-safe carry hits');
  assert.ok(hits.every((n) => n.textContent === 'sem dados'),
    `every count demoted to a placeholder, got ${JSON.stringify(hits.map((n) => n.textContent))}`);
  assert.ok(hits.every((n) => n.title === 'sem histórico: o registro de decisões não cobre este período'),
    `and the reason is in the title, got ${JSON.stringify(hits.map((n) => n.title))}`);
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
  // The Uso column shows the NUMBER; the finding words live in the cell's
  // title (62px cannot hold "nunca disparou na última hora").
  const hits = findAll(dom.get('sheet'), 'step-hits')
    .concat(findAll(dom.get('sheetTailList'), 'step-hits'));
  const titles = hits.map((n) => n.title);
  assert.ok(hits.some((n) => n.textContent === '2'), 'a rule that fired keeps its count');
  assert.ok(titles.includes('disparou 2× na última hora'),
    `the count says what it counted over, got ${JSON.stringify(titles)}`);
  assert.ok(titles.includes('nunca disparou na última hora'),
    'a rule that existed and never fired keeps the finding, with its period');
  assert.ok(titles.every((t) => !/sem histórico/.test(t)), 'no demotion on a covering window');
  assert.doesNotMatch(flat(dom.get('sheet')), /% of the decisions/);
  // The three zero-hit rows (never-caught, default, fail-safe) are amber: the
  // window covers the policy, so every zero is a genuine finding. There is no
  // blocklist row — this policy declares no manual ban (spec 1.3). Default and
  // fail-safe live in the catch-all tail (§5).
  const zeros = hits.filter((n) => /zero/.test(n.className));
  assert.equal(zeros.length, 3, 'every zero-hit row stays an amber finding');
  assert.equal(hits.filter((n) => /empty/.test(n.className)).length, 0);
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
  const hits = findAll(dom.get('sheet'), 'step-hits')
    .concat(findAll(dom.get('sheetTailList'), 'step-hits'));
  assert.ok(hits.length >= 3, 'every rule-bearing row renders a hits cell');
  assert.ok(hits.every((n) => n.textContent === 'sem dados'),
    `placeholder only, got ${JSON.stringify(hits.map((n) => n.textContent))}`);
  assert.ok(hits.every((n) => n.title === 'sem histórico: o registro de decisões não cobre este período'));
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
  assert.match(banner, /Nenhuma decisão há 17d/,
    'the age reason names the age with fmtAge, the same formatter the provenance line uses');
  assert.match(banner, /Estas contagens descrevem de 01\/08 22:12 a 02\/08 01:51 UTC, não o presente/);
  assert.doesNotMatch(flat(dom.get('sheet')), /never fired/);
});

test('a six-hour-old window under a newer policy reads as horas, never as "1 dia"', () => {
  // The reported incident: router.yaml edited at ~14:5x, last decision at
  // 16:19 UTC. The old banner ignored fresh.reason, told the AGE story and
  // rounded: Math.round(0.25) = 0, Math.max(1, …) = 1 → "Nenhuma decisão há
  // 1 dia" — a 4× inflation that made a same-day change read as a day of
  // silence. Both halves must be pinned: the reason is CONFIG (policy words,
  // not age words) and the age is 6h (fmtAge), never a rounded day.
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 27, 16, 19, 0);   // the last decision's hour, 16:19 UTC
  api.state.clock = new Date(T);
  api.state.loading = false;
  const hourS = 3600;
  api.state.routes = [{ id: 'a', ts: T / 1000 - 6 * hourS }];
  api.state.status = { config_mtime: new Date(T - 90 * 60 * 1000).toISOString() };  // edited after the decision
  api.state.policy = {
    rules: [{ id: 'r', when: {}, then: { model: 'T4' } }],
    default: {}, classifier: { model: 'm' }, fail_safe: { model: 'm' }, tiers: { T4: {} },
  };
  api.renderSheet();

  assert.equal(dom.get('windowStale').hidden, false);
  const banner = flat(dom.get('windowStale'));
  assert.match(banner, /A política mudou depois da última decisão \(há 6h\)/,
    'the age inside the policy story is hours, via fmtAge');
  assert.doesNotMatch(banner, /1 dia|1d\b/, 'hours must not round up to a day');
  assert.doesNotMatch(banner, /Nenhuma decisão há/);
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
const TAB_NAMES = ['tarefas', 'simular', 'modelos', 'precos', 'decisoes', 'politica'];
function tabWire(dom) {
  const tabs = TAB_NAMES.map((name) => {
    const t = dom.get(`tab-${name}`);
    t.dataset.tab = name;
    return t;
  });
  const screens = TAB_NAMES.map((name) => dom.get(`panel-${name}`));
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
  api.selectTab('simular');
  assert.equal(tabs[1].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[0].getAttribute('aria-selected'), 'false');
  assert.equal(screens[1].classList.contains('active'), true);
  assert.equal(screens[0].classList.contains('active'), false);
});

test('the six tabs and panels are a bijection, born with exactly one selected', () => {
  // DESIGN.md §7 invariant: for every data-tab="X" there is an id="panel-X"
  // and the converse. selectTab builds the panel id from the tab name
  // (`panel-${name}`), so a tab without a panel, or a panel without a tab, is
  // a click that lands nowhere. The order is the approved split (2026-08-27).
  const src = fs.readFileSync(sourcePath, 'utf8');
  const tabs = [...src.matchAll(/data-tab="([a-z]+)"/g)].map((m) => m[1]);
  const panels = [...src.matchAll(/id="panel-([a-z]+)"/g)].map((m) => m[1]);
  assert.deepEqual(tabs,
    ['tarefas', 'simular', 'modelos', 'precos', 'decisoes', 'politica'],
    'the six destinations, in the approved order');
  assert.deepEqual(panels,
    ['tarefas', 'simular', 'modelos', 'precos', 'decisoes', 'politica'],
    'every tab has its panel, in the same order — the bijection');
  // Born state lives in the markup, not in a script pass: exactly one tab is
  // selected and it is Tarefas, the tab an operator lands on.
  const selected = [...src.matchAll(/class="tab" id="(tab-[a-z]+)"[^>]*aria-selected="true"/g)].map((m) => m[1]);
  assert.deepEqual(selected, ['tab-tarefas'], 'exactly one tab is born selected, and it is Tarefas');
});

test('each block lives inside the panel the approved split names', () => {
  // The 2026-08-27 split moved blocks between panels. A panel's markup is the
  // slice between its id and the next panel's id, so a block that drifted to
  // the wrong tab fails here by index — the same cut the card's grep uses.
  const src = fs.readFileSync(sourcePath, 'utf8');
  const order = ['panel-tarefas', 'panel-simular', 'panel-modelos',
    'panel-precos', 'panel-decisoes', 'panel-politica'];
  const bounds = order.map((panel, i) => {
    const start = src.indexOf(`id="${panel}"`);
    assert.ok(start >= 0, `${panel} must exist`);
    return {
      panel, start,
      end: i + 1 < order.length ? src.indexOf(`id="${order[i + 1]}"`) : src.length,
    };
  });
  const inPanel = {
    'panel-tarefas': ['sheet', 'inspector', 'windowStale'],
    'panel-simular': ['probeForm', 'probeTask', 'probeHourBox', 'probeContextBox', 'probeResult', 'chainPlan'],
    'panel-modelos': ['presetBox', 'ladder', 'failSafeBox', 'compactionBox', 'agentQueues', 'bans'],
    'panel-precos': ['priceStrip', 'priceNote'],
    'panel-decisoes': ['routesTable', 'replayPath', 'replayPlan', 'routesFilter', 'routeScopes'],
    'panel-politica': ['jsonNote', 'policyEditor', 'jsonActions', 'jsonMsg', 'jsonDiff'],
  };
  for (const { panel, start, end } of bounds) {
    for (const block of inPanel[panel]) {
      const at = src.indexOf(`id="${block}"`);
      assert.ok(at >= start && at < end,
        `${block} must live inside ${panel} (found at ${at}, panel spans [${start}, ${end}))`);
    }
  }
  // The whole-file editor left its <details>: the only <details> left (the
  // probe's context box) sits on Simular, BEFORE Política — so every <details>
  // opening precedes the editor, and the editor sits inside its own panel.
  const editorAt = src.indexOf('id="policyEditor"');
  const politica = src.indexOf('id="panel-politica"');
  assert.ok(editorAt > politica && editorAt < src.indexOf('</section>', politica),
    'policyEditor is inside panel-politica, not under a <details>');
  assert.ok(src.lastIndexOf('<details') < editorAt,
    'no <details> wraps the editor — the tab itself is the disclosure');
});

test('arrows walk the six tabs and wrap; exactly one is selected', () => {
  // The keydown handler lives in wire(), which the harness normally strips;
  // keepWire leaves it in and this test drives the REAL registered handlers —
  // a tab whose handler was never attached would fail at the first press.
  const dom = fakeDom();
  const { tabs, screens } = tabWire(dom);
  loadConsole({ dom, keepWire: true });
  const selectedCount = () => tabs.filter((t) => t.getAttribute('aria-selected') === 'true').length;
  const activeCount = () => screens.filter((s) => s.classList.contains('active')).length;
  const press = (tab, key) => {
    const fn = tab._listeners && tab._listeners.keydown;
    assert.ok(fn, 'wire() attached the keydown handler to every tab');
    fn({ key, preventDefault() {} });
  };

  // → walks forward; each step leaves exactly one tab selected and one panel up.
  press(tabs[0], 'ArrowRight');
  assert.equal(tabs[1].getAttribute('aria-selected'), 'true');
  assert.equal(screens[1].classList.contains('active'), true);
  assert.equal(selectedCount(), 1);

  // Five more → returns to the first: the strip is a loop, no dead end at the
  // sixth. Each press lands on the tab the previous press selected (wire()
  // focuses it), which is how a real keyboard user walks the strip.
  for (let i = 2; i <= 5; i += 1) press(tabs[i - 1], 'ArrowRight');
  press(tabs[5], 'ArrowRight');
  assert.equal(tabs[0].getAttribute('aria-selected'), 'true', '→ wraps from the sixth back to the first');
  assert.equal(selectedCount(), 1);

  // ← from the first wraps to the sixth.
  press(tabs[0], 'ArrowLeft');
  assert.equal(tabs[5].getAttribute('aria-selected'), 'true', '← wraps from the first to the sixth');
  assert.equal(selectedCount(), 1);
  assert.equal(activeCount(), 1, 'exactly one panel is visible at every step');
});

// ── the phone forms (card t_16e0c261): 360px, and what collapses ──────
// Below the host's own 640px breakpoint the console stops drawing the
// 24-cell band and the six-column rule row — the band is illegible before it
// stops fitting (24 cells at 360px = 15px each), and a six-column row at 360
// would give each column ~60px — and draws the written-window list and the
// rule cards instead. The switch is a RENDER swap decided by two pure
// functions, never display:none on both forms.

test('priceMode and ruleMode hit the 640 breakpoint on BOTH sides', () => {
  const { api } = loadConsole();
  // 640 is the host's own collapse width (style.css:2137): below it the rail
  // hides and the drawer is the only navigation, and this console's own
  // @media (max-width: 640px) block matches it. The boundary is asserted on
  // both sides because a one-sided limit is a half limit.
  assert.equal(api.priceMode(360), 'lista');
  assert.equal(api.priceMode(639), 'lista');
  assert.equal(api.priceMode(640), 'lista');
  assert.equal(api.priceMode(641), 'faixa');
  assert.equal(api.priceMode(851), 'faixa');
  assert.equal(api.ruleMode(360), 'cartao');
  assert.equal(api.ruleMode(640), 'cartao');
  assert.equal(api.ruleMode(641), 'grade');
  assert.equal(api.ruleMode(861), 'grade');
});

test('below 640 the price strip draws the written list, not the band', () => {
  const { api, dom } = loadConsole({ width: 360 });
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  api.state.clock = PEAK;
  api.renderPriceStrip();
  const box = dom.get('priceStrip');
  assert.equal(findAll(box, 'price-band').length, 0, 'the band is not drawn at 360');
  assert.equal(findAll(box, 'h-cell').length, 0, 'and no hour cell with it');
  const cards = findAll(box, 'pwin');
  assert.equal(cards.length, 4, 'one written card per MODEL — the strip per-model axis survives the swap');
  // The provider grouping survives too: zai still holds its two models.
  const zai = flat(findAll(box, 'price-group')[0]);
  assert.match(zai, /glm-4\.7/);
  assert.match(zai, /glm-5\.3/);
  // The window facts are said in words, not in 15px cells.
  const text = flat(box);
  assert.match(text, /06 – 10/);
  assert.match(text, /de segunda a sexta/);
  assert.match(text, /01 – 04/);
  assert.match(text, /todo dia/);
  assert.match(text, /o preço não varia com a hora/, 'the flat model says so in words');
});

test('the list head carries the now multiplier; each line carries its window', () => {
  const { api, dom } = loadConsole({ width: 360 });
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  api.state.clock = PEAK; // Monday 07:14 UTC: zai in its 06-10 peak, deepseek not
  api.renderPriceStrip();
  const cards = findAll(dom.get('priceStrip'), 'pwin');
  const nowOf = (i) => findAll(cards[i], 'pwin-now')[0].textContent;
  assert.equal(nowOf(0), '2× agora', 'glm-4.7 is inside zai peak');
  assert.ok(findAll(cards[0], 'pwin-now')[0].classList.contains('up'), 'peak wears the danger colour');
  assert.equal(nowOf(1), '2× agora');
  assert.equal(nowOf(2), 'preço base agora', 'deepseek peak is 01-04; at 07 it is base');
  assert.equal(nowOf(3), 'preço base agora', 'a model with no windows is flat now');
  // The line's own multiplier is the WINDOW's, not the now one.
  assert.equal(findAll(cards[2], 'pwin-mul')[0].textContent, '2×');
  // The origin word rides the card as it rides the band row: give one model
  // a registry origin and the card must say where its windows came from.
  api.state.capabilities['glm-4.7'].price_windows_origin = 'registry';
  api.renderPriceStrip();
  assert.match(flat(dom.get('priceStrip')), /janela do catálogo/);
});

test('the list keeps the write surface: the band hour-click, at the hour on screen', () => {
  const { api, dom } = loadConsole({ width: 360 });
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  api.state.clock = PEAK;
  api.renderPriceStrip();
  const adds = findAll(dom.get('priceStrip'), 'pwin-add');
  assert.equal(adds.length, 4, 'one button per editable model (declared models would lose it)');
  assert.equal(adds[0].textContent, 'Remover a janela das 07h', 'glm-4.7 at 07 is in the peak — the click removes');
  assert.equal(adds[2].textContent, 'Acrescentar janela', 'deepseek at 07 is base — the click adds');
  // The click arms the SAME proposal spine the band's hour-click uses.
  adds[0]._listeners.click();
  const box = dom.get('priceStrip');
  const area = box.children[box.children.length - 1];
  assert.equal(area.hidden, false, 'the proposal row opens');
  assert.match(flat(area), /volta ao preço base/, 'removing names the price change');
  adds[2]._listeners.click();
  const area2 = box.children[box.children.length - 1];
  assert.match(flat(area2), /passa a custar 0,8×/, 'adding names the new price');
});

test('renderSheet keeps the #sheet outerHTML fixture byte-for-byte', () => {
  const { api, dom } = loadConsole({ width: 851 });
  api.state.loading = false;
  api.state.policy = reorderPolicy();
  const sheet = dom.get('sheet');
  sheet.tagName = 'ol';
  sheet.className = 'sheet';
  api.renderSheet();
  const expected = fs.readFileSync('tests/fixtures/render_sheet.outer.html', 'utf8');
  assert.equal(sheet.outerHTML, expected);
});

test('ruleMode decides the sheet draw: cards at 360, grid at 851', () => {
  const { api, dom } = loadConsole({ width: 360 });
  api.state.loading = false;
  api.state.policy = { rules: [
    { id: 'r1', when: { verb_class: { eq: 'trivial' } }, then: { model: 'T1' } },
    { id: 'r2', when: {}, then: { deny: true } },
  ], default: {}, tiers: {} };
  api.renderSheet();
  assert.equal(dom.get('sheet').classList.contains('mode-cards'), true, 'at 360 the sheet draws cards');
  assert.equal(dom.get('sheetTailList').classList.contains('mode-cards'), true, 'the catch-all rows share the card form');
  const wide = loadConsole({ width: 851 });
  wide.api.state.loading = false;
  wide.api.state.policy = { rules: [], default: {}, tiers: {} };
  wide.api.renderSheet();
  assert.equal(wide.dom.get('sheet').classList.contains('mode-cards'), false, 'at 851 the grid stays');
});

test('syncModes re-renders only when the mode actually crosses the breakpoint', () => {
  const { api, dom, win } = loadConsole({ width: 851 });
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  api.state.clock = PEAK;
  api.renderPriceStrip();
  assert.equal(findAll(dom.get('priceStrip'), 'price-band').length, 4, '851 draws the band');
  // A resize inside the same mode must not redraw anything.
  win.innerWidth = 700;
  api.syncModes();
  assert.equal(findAll(dom.get('priceStrip'), 'price-band').length, 4, '700 is still faixa — nothing redrawn');
  // Crossing to 640 swaps the draw to the list.
  win.innerWidth = 640;
  api.syncModes();
  assert.equal(findAll(dom.get('priceStrip'), 'price-band').length, 0, '640 crossed into lista — the draw swapped');
  assert.equal(findAll(dom.get('priceStrip'), 'pwin').length, 4, 'and the list took over');
  // Another resize inside lista stays put.
  win.innerWidth = 360;
  api.syncModes();
  assert.equal(findAll(dom.get('priceStrip'), 'pwin').length, 4, '360 is still lista — no churn');
});

test('a keyboard tab change scrolls the selected tab into the strip', () => {
  const dom = fakeDom();
  const { tabs } = tabWire(dom);
  loadConsole({ dom, keepWire: true });
  const press = (tab, key) => {
    const fn = tab._listeners && tab._listeners.keydown;
    assert.ok(fn, 'wire() attached the keydown handler');
    fn({ key, preventDefault() {} });
  };
  press(tabs[0], 'ArrowRight');
  // Field compare, not deepEqual: the stub's scrollIntoView stores the VM
  // realm's object literal, whose prototype differs from this file's.
  assert.ok(tabs[1]._scrolledTo && tabs[1]._scrolledTo.block === 'nearest',
    'the tab the keyboard moved to is brought into the scrollable strip');
});

test('the tab fade is lit by measurement of the rendered strip, not by width', () => {
  const dom = fakeDom();
  tabWire(dom);
  const { api } = loadConsole({ dom, keepWire: true });
  const nav = dom.get('sel:nav.tabs');
  assert.ok(nav, 'the strip has a nav to measure');
  nav.clientWidth = 360;
  nav.scrollWidth = 360;
  api.syncTabFade();
  assert.equal(nav.classList.contains('tabstrip-fade'), false, 'fits — no fade');
  nav.scrollWidth = 1200;
  api.syncTabFade();
  assert.equal(nav.classList.contains('tabstrip-fade'), true, 'overflows — the fade lights');
});

test('the tab strip scrolls with a click-through fade (comp-360)', () => {
  const { style } = consoleStyle();
  // The fade is the ONLY signal that the strip scrolls, so it has to be seen
  // (34px, opaque at its root, the peeked tab showing under it) and it must
  // never eat a tap on that tab.
  assert.match(style, /\.tabs::after \{[^}]*width: 34px/);
  assert.match(style, /\.tabs::after \{[^}]*pointer-events: none/);
  assert.match(style, /\.tabs\.tabstrip-fade::after \{[^}]*opacity: 1/);
  // The strip itself scrolls instead of wrapping (the ≤640 block).
  const phone = style.slice(style.indexOf('@media (max-width: 640px)'));
  assert.match(phone, /\.tabs \{[^}]*overflow-x: auto/);
});

test('the phone block treats the price list, the decisions table and the compaction pick', () => {
  const { style } = consoleStyle();
  const phone = style.slice(style.indexOf('@media (max-width: 640px)'));
  assert.match(phone, /\.pwin/, 'the written-window list is folded in the same block');
  assert.match(phone, /\.row\.route/, 'the decisions table keeps its two-row phone form');
  // The compaction pick measured +181px at 360 (the 88px label column plus
  // a select that refuses to shrink below its content); below 640 it stacks.
  assert.match(phone, /\.compaction-pick \{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(phone, /\.compaction-k \{[^}]*text-align:\s*left/);
});

test('more @media blocks than the original five, with the phone block intact', () => {
  const { style } = consoleStyle();
  const blocks = (style.match(/@media/g) || []).length;
  assert.ok(blocks > 5, `more media blocks than the original five, got ${blocks}`);
});

test('the phone swap is a RENDER swap — no display:none hides the band or the rows', () => {
  const { style } = consoleStyle();
  // Below 640 the strip and the sheet change DRAW (priceMode/ruleMode), never
  // hide-and-reveal: a display:none would take the fact out of the DOM and
  // put the two forms at the mercy of a cascade order.
  for (const sel of ['.price-row', '.price-band', '.pwin', '.step']) {
    const esc = sel.replace('.', '\\.');
    // (?![-\w:]) keeps the two pre-existing decorative rules out of the
    // audit: .step::before (the spine tick, display:none on tail rows) and
    // .step-grip .grip-glyph (the drag glyph, hidden on touch where the
    // arrows replace it). A pseudo-element or a child control is not a fact
    // being hidden — the ROW itself is the fact carrier.
    assert.doesNotMatch(style, new RegExp(`${esc}(?![-\w:])[^{]*\\{[^}]*display:\\s*none`),
      `${sel} must not be display:none'd`);
  }
});

test('the new phone surfaces are 44px targets, with the classes named', () => {
  const { style } = consoleStyle();
  // §7: a finger needs 44px. The guard names the CLASS — a bare `button`
  // (0,0,1) loses to any class-based sizing in this stylesheet, which is how
  // the 14px probe input survived a decorative guard once (measured, iPhone).
  assert.match(style, /\.pwin-add \{[^}]*min-height: 44px/);
  assert.match(style, /\.tab \{[^}]*min-height: 44px/);
});

test('the controls measured under the 44px floor get it, classes named', () => {
  // Card t_5fb727b5, measured at 360px (dpr 3, coarse pointer): the reorder
  // arrows 22px, the destination chip 21px, the peak-policy and compaction
  // selects 20px, the probe hour and the decision filter 39px. The JSON
  // search (29px) is the seventh — it arrived with the tools card
  // (t_3ba979a1), after the operator's measurement, and was caught by the
  // re-measurement here. Each rule must live INSIDE the coarse-pointer
  // guard — on a desktop these controls keep the sizes a mouse earned —
  // and must name the class or id, for the same (0,0,1)-loses reason as
  // the test above.
  const { style } = consoleStyle();
  const touch = style.slice(style.indexOf('@media (hover: none) and (pointer: coarse)'));
  for (const sel of ['.step-target', '.peak-policy', '.ctl',
    '.step-grip .grip-arrow', '#probeHour', '#routesFilter',
    '.json-search-input']) {
    const esc = sel.replace(/\./g, '\\.');
    assert.match(touch, new RegExp(`${esc} \\{[^}]*min-height: 44px`),
      `${sel} needs the 44px floor inside the coarse-pointer guard`);
  }
});

test('.pwin-name wraps anywhere — the model id end stays legible at 360px', () => {
  // Card t_5fb727b5, measured at 360px: nvidia/nemotron-3-super-120b-a…
  // ran 13px past its 121px box. What distinguishes these names sits at the
  // END ("…-120b" contra "…-550b"), so ellipsis would cut exactly the part
  // that answers "qual dos dois?" — the rule must wrap, not clip.
  const { style } = consoleStyle();
  assert.match(style, /\.pwin-name \{[^}]*overflow-wrap: anywhere/,
    'the model id breaks anywhere so the full id stays readable');
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

  assert.equal(screens[0].classList.contains('active'), true, 'the Tarefas tab is now visible');
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
  api.renderInspector({ id: 'rule:r3', name: 'r3', bind: 'rule', ruleIndex: 2 });
  const labels = findAll(dom.get('inspector'), 'btn').map((b) => b.textContent || '');
  assert.ok(!labels.some((t) => /Mover/.test(t)),
    'no move button without a finding — the button must not invent a shadower');
  assert.ok(labels.some((t) => /Desativar/.test(t)), 'disable is always available for a rule');
});

test('a synthetic row is never clickable — no pointer, no handler on a row with no editor', () => {
  const { api, dom } = loadConsole();
  api.state.policy = rulePolicy();
  // The synthetic row is conditional on a manual ban existing (spec 1.3), so the
  // subject of this test has to be declared for the test to have a subject at all.
  api.state.policy.blocklist = { manual_ban: ['glm-4.7'] };
  api.state.status = { validation_errors: [], error_targets: [] };
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

test('a declared window is read from the ONE spelling, and junk is read as flat', () => {
  const { api } = loadConsole();
  // `price_windows` — which the time-layer addendum calls "the ONE encoding".
  assert.deepEqual(plain(api.entryWindows({
    price_windows: [{ hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2 }],
  })), [{ hours: [6, 10], multiplier: 2, weekdays: [0, 1, 2, 3, 4] }]);
  // `peak_windows_utc` + `peak_multiplier` is NOT a second spelling. This test used
  // to assert it worked, with a comment saying dropping it "would silently un-price
  // the busiest metered rail" — and `git log -S` over router/ shows those names never
  // existed there, in any version. The console was the only thing in the repo that
  // understood them, so a hop written that way priced HERE and priced flat
  // everywhere else. It is now a hard lint error, so such a hop cannot be saved.
  assert.deepEqual(plain(api.entryWindows({
    peak_windows_utc: [[1, 4], [6, 10]], peak_multiplier: 2,
  })), [], 'the retired dialect must read as no window at all');
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
  const windowed = api.eloWindows(catalogueEntry('glm-5.3-flash'));
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
  // peaks and glm-5.3-flash the weekday-gated one. No registry entry declares a
  // CHEAP window any more, so that exemplar is declared inline below.
  // The zai exemplar was glm-4.7 until 2026-08-27, when the plan dropped that id
  // and it lost the credit window with the coverage: the weekday gate is a fact
  // about a PLAN-covered elo, so the exemplar has to be one.
  const deepseek = api.eloWindows(catalogueEntry('deepseek-v4-flash'));
  const zai = api.eloWindows(catalogueEntry('glm-5.3-flash'));
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
  const zai = api.eloWindows(catalogueEntry('glm-5.3-flash'));
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
  assert.deepEqual(rows.map((r) => r.rail), ['deepseek', 'zai'],
    'the two rails the registry actually prices by the hour');
  assert.equal(rows[0].expensive, true);
  assert.equal(rows[0].changesAt, 10);

  // Saturday 07:00: NEITHER rail is expensive. Both windows are gated Mon-Fri in
  // the registry, and this used to assert the opposite — deepseek alone expensive
  // — from the pre-2026-08-22 vendor wording, before deepseek's weekday gate was
  // added following a silent vendor edit. The console was pricing deepseek at 2x
  // for 14 h every weekend that the vendor bills at 1x.
  const weekend = plain(api.railWindowRows(null, { hour: 7, weekday: 5 }));
  assert.equal(weekend.filter((r) => r.expensive).length, 0,
    'both published windows are weekday-only');

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
  assert.equal(rails.length, 2, 'one line per rail that prices by the hour');
  const first = flat(rails[0]);
  assert.match(first, /deepseek 2× em hora de pico até 10:00 UTC/, 'real spaces, so it reads aloud');
  assert.match(rails[0].className, /peak/, 'and amber, because paying double needs attention');

  // Saturday: both published windows are gated Mon-Fri, so no rail is a condition.
  // This used to assert a xiaomi 0.8x "hora barata" row — a discount the registry
  // publishes for NO mimo elo, on purpose: it is a prepaid Token Plan credit
  // coefficient and this install bills pay-as-you-go, so carrying it claimed metered
  // cost fell 20% for 8 h/day when real cost was 1.25x the estimate there. The
  // mechanism is still covered, by a DECLARED per-elo window
  // (`api.eloWindows({price_windows: [...]})` above), which is where a real discount
  // would come from.
  api.state.clock = new Date(Date.UTC(2026, 7, 22, 7, 14));  // Saturday
  api.renderClock();
  const weekend = dom.get('clockRails').children;
  for (const row of weekend) {
    assert.doesNotMatch(row.className, /peak/,
      'a weekday-gated window must not paint the weekend amber');
  }
});

test('the pricing clock exists only on tabs whose visible fact changes with time', () => {
  const { api, dom } = loadConsole();
  api.state.clock = PEAK;
  const shown = ['tarefas', 'simular', 'modelos', 'precos'];
  const hidden = ['politica', 'decisoes'];

  for (const tab of shown) {
    api.state.tab = tab;
    api.renderClock();
    assert.equal(dom.get('clockbar').hidden, false, `${tab} needs the current price fact`);
  }
  for (const tab of hidden) {
    api.state.tab = tab;
    api.renderClock();
    assert.equal(dom.get('clockbar').hidden, true, `${tab} must not repeat a fact it does not use`);
  }
});

test('leaving a time-aware tab stops the pricing-clock timer and a stale tick cannot repaint it', () => {
  const scheduled = [];
  const cleared = [];
  const timers = {
    setInterval(fn, ms) { scheduled.push({ fn, ms }); return scheduled.length; },
    clearInterval(id) { cleared.push(id); },
  };
  const dom = fakeDom();
  tabWire(dom);
  const { api } = loadConsole({ dom, keepWire: true, timers });
  api.state.clock = PEAK;
  api.selectTab('tarefas');
  assert.equal(scheduled.filter(({ ms }) => ms === 60000).length, 2,
    'one minute timer updates the clock and one separately watches status');

  api.selectTab('politica');
  assert.deepEqual(cleared, [1], 'only the clock timer stops; the status watcher remains alive');
  dom.get('clockNow').textContent = 'não repintar';
  scheduled[0].fn();
  assert.equal(dom.get('clockNow').textContent, 'não repintar',
    'a queued tick after the tab switch cannot redraw an absent clock');
});

test('tab state is rendered only when the tab has a state, in a fixed reservation beside its label', () => {
  const { api, dom } = loadConsole();
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.renderRail();

  assert.equal(dom.get('stateSimular').children.length, 0);
  assert.equal(dom.get('statePrecos').children.length, 0);
  assert.equal(dom.get('statePolitica').children.length, 0);
  assert.equal(dom.get('stateTarefas').children[0].className, 'tab-state is-alive');

  const src = fs.readFileSync(sourcePath, 'utf8');
  assert.match(src, /\.tab-state-slot \{[^}]*width: 32px/, 'the fixed slot reserves the label width');
  assert.doesNotMatch(src, /<span class="tab-state"/, 'an empty tab never ships a muted dot in its DOM');
});

test('the first panel group uses the same 34px step on every tab', () => {
  const src = fs.readFileSync(sourcePath, 'utf8');
  assert.match(src, /\.clockbar \{[^}]*margin-bottom: 16px/, 'the existing clock-to-panel step remains 30–38px');
  assert.match(src, /#panel-politica > \.group:first-child \{ margin-top: 0; \}/,
    'the policy editor does not add its own group margin after the clock slot disappears');
  assert.match(src, /\.price-section \{ margin-top: 0; \}/,
    'prices does not add 18px to the shared top step');
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
  assert.match(text, /2× agora, teto 1\.5×/, 'the price and the ceiling, not an enum');
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
  assert.match(flat(dom.get('chainPlan')), /2× agora, teto 1\.5×/);
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
  assert.match(text, /2× agora, teto 1\.5×/, 'and the numbers behind the objection survive');
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

  // THE CATALOGUE ANSWERED, and it says this elo bills in credits with no dollar
  // rate at all. That is a reported fact and it earns words — an operator has to
  // know a plan rail is not free. `price_published` is service.py's, computed by
  // asking the running path.
  // Declared rather than read from the registry: glm-5.3 was this exemplar until
  // 2026-08-27, when the vendor published its metered rate, and nothing
  // plan-covered is unpriced today. The rendering still has to be right for the
  // shape, and the next plan-only launch will be it again.
  const plan = { provider: 'zai', billing_mode: 'plan', price_in: null,
                 price_out: null, price_published: false };
  assert.equal(api.pricePublished(plan, plan), false);
  const words = api.priceWords(plan, 2, 'plan', api.pricePublished(plan, plan));
  assert.match(words, /2× em hora de pico/);
  assert.match(words, /cobrado em créditos do plano/);
  assert.doesNotMatch(words, /\$0/, 'a plan rail rendered as $0 would win every comparison on screen');
  // And the real entry moved: it publishes dollars now, which is exactly why the
  // console may not read "plan" off the price column.
  const pricedPlan = catalogueEntry('glm-5.3');
  assert.equal(pricedPlan.billing_mode, 'plan');
  assert.equal(pricedPlan.price_published, true, 'glm-5.3 publishes a dollar rate since 2026-08-27');
  assert.match(api.priceWords(pricedPlan, 1, 'plan', true), /cobrado em créditos do plano/,
    'a published rate does not stop the credits qualifier — the unit is the billing mode');

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
  // glm-4.7 was the case this line was wrong about: plan-covered AND carrying a
  // published rate ("also purchasable metered at the same price"), so the console
  // rendered `2× peak · $1.20 in / $4.40 out per 1M` for a rail that draws 16 output
  // credits — 32 inside the window — and invoices none of those dollars on a plan
  // key. The credits-versus-dollars split is what cheapest_now buckets on and the
  // only thing a time_cap may act on, so the surface that shows prices must carry it.
  // glm-5.3-flash is that shape today (plan-covered, 0.15/0.50 list, 8 output
  // credits), and glm-4.7 stopped being it when the plan dropped the id.
  const { api } = loadConsole();
  const entry = catalogueEntry('glm-5.3-flash');
  const facts = registryFacts('glm-5.3-flash');
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
    assert.match(words, new RegExp(`\\$${facts.price_in.toFixed(2)} entrada / \\$${facts.price_out.toFixed(2)} saída por 1M`),
      'every mode still shows the published rate');
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
  // scrolled, and the inspector opens on the default. The default lives in the
  // catch-all tail (§5), so it is searched there.
  toDefault[0]._listeners.click();
  assert.equal(api.state.selected, 'default', 'the inspector opened on the default, not on a rule');
  const row = dom.get('sheetTailList').children.find((c) => c.dataset.ruleId === '__default');
  assert.ok(row, 'the synthetic default row is in the catch-all tail');
  assert.deepEqual(plain(row._scrolledTo), { block: 'center' }, 'and it was scrolled into view');
  assert.match(flat(dom.get('inspector')), /default/, 'the inspector names the default');

  // The tail row itself carries the §4.3 fifth prefix, like a rule's would.
  api.renderSheet();
  assert.match(flat(dom.get('sheetTailList')), /⚠ Grupo T9 — não existe/);
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

  // The two-click rule is per execution: a third click is a NEW question, never
  // a silent execution of the one that already wrote. (The mode-era "leaving
  // edit mode disarms" is gone with the mode; a refresh still drops the arming —
  // the next test pins that.)
  await api.requestRevert();
  assert.equal(dom.get('jsonRevert').textContent, 'Confirmar: voltar à versão anterior',
    'a third click asks again');
  await api.requestRevert();
  assert.deepEqual(writes(), ['/apply/revert', '/apply/revert'],
    'the second confirm of the new question is the one that acts');
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
  // The tier chip is the bare "T1 · Trivial" of the comp — no arrow (the
  // "Vai para" head says where it goes, and the 128px column cannot hold
  // arrow + gap + full label, measured). The other kinds keep their arrow.
  /^T\d+ · /,
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

test('the sheet counts rules (+ ban) in the numbered list and the two catch-all rows in the tail', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy();
  api.renderSheet();
  const rows = () => (dom.get('sheet').children || []).length;
  const tailRows = () => (dom.get('sheetTailList').children || []).length;
  const rules = api.state.policy.rules.length;
  // The catch-all rows (default, fail-safe) leave the numbered list for the
  // tail block (§5, comp-tarefas "se nada acima casou").
  assert.equal(rows(), rules, 'no manual ban, so no ban row: render nothing for nothing');
  assert.equal(tailRows(), 2, 'default and fail-safe live under the tail label');

  api.state.policy = sheetPolicy({ blocklist: { manual_ban: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }] } });
  api.renderSheet();
  assert.equal(rows(), rules + 1, 'a ban is the first thing that decides, so it is the first row');
  assert.equal(tailRows(), 2, 'the tail keeps its two rows when a ban exists');
});

test('every row on the sheet has a destination, and it is one of the five (CA2)', () => {
  // The fixture exercises all five on purpose: a group, the classifier, a refusal, a
  // fixed model id, and a group that does not exist.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = sheetPolicy({ blocklist: { manual_ban: [{ model: 'deepseek-v4-pro', provider: 'deepseek' }] } });
  api.renderSheet();

  // The tail rows (default, fail-safe) carry destinations too, so both lists
  // are read — §5 moved the rows, not the vocabulary.
  const dests = findAll(dom.get('sheet'), 'step-dest')
    .concat(findAll(dom.get('sheetTailList'), 'step-dest'))
    .map((node) => flat(node).replace(/\s+/g, ' ').trim());
  assert.ok(dests.length >= 8, `every row draws a destination, got ${dests.length}`);
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
    `'${api.WRITE.refresh}'`, `'${api.WRITE.refreshing}'`, `'${api.WRITE.routing}'`, `'${api.WRITE.routingOn}'`,
    `'${api.WRITE.routingOff}'`, `'${api.WRITE.routingVerdict}'`, `'${api.WRITE.banned}'`,
    `'${api.WRITE.cooldownLeft}'`, `'${api.WRITE.textEdit}'`, `'${api.WRITE.loading}'`,
    // The reorder's two words (card: reordenar pelo punho) — once each, in
    // the map, like every other gesture word.
    `'${api.WRITE.moveRule}'`, `'${api.WRITE.movingRule}'`,
    // Card t_3ba979a1: the JSON tools' static button words — Formatar,
    // Copiar and the fold button's two labels — live in the map once each,
    // like every other button word on this editor.
    `'${api.WRITE.format}'`, `'${api.WRITE.copy}'`, `'${api.WRITE.foldAll}'`, `'${api.WRITE.expandAll}'`,
    // The two writable keys that had no control at all until this card, and the
    // words their controls say: the master switch's two directions with the
    // consequence of each, and the ban gesture with its own. Same rule as every
    // other surface word — the map is the only copy.
    `'${api.WRITE.routingStop}'`, `'${api.WRITE.routingStart}'`,
    `'${api.WRITE.routingStopWhy}'`, `'${api.WRITE.routingStartWhy}'`,
    `'${api.WRITE.banSave}'`, `'${api.WRITE.banPick}'`, `'${api.WRITE.banWhy}'`,
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
// ── ONE authority for which models may be offered ─────────────────────────────
// Every model picker sourced `Object.keys(state.capabilities)` — the capability
// CATALOGUE, which answers what is KNOWN. An operator choosing a model is asking a
// different question: what can this Hermes actually call?
//
// Measured on the docker stack (2026-09-02): it runs `us.anthropic.claude-opus-5` on
// bedrock with AWS as its only provider credential, and the pickers offered glm,
// deepseek and gpt ids it has no key for while NOT offering the id it actually runs
// on — `us.anthropic.*` is deliberately absent from the catalogue (capabilities.py's
// docstring explains why: registering it would assert a price that rail may not
// charge). So the catalogue is exactly the wrong list for this job.
//
// The authority is /status.configured_models (the agent's own config.yaml) UNION the
// models the policy on screen already names — you must be able to keep what you have
// even after it leaves the install's config. The catalogue is demoted to annotation,
// and reaching it is a deliberate gesture rather than the default.

function oneModelPolicy() {
  // Deliberately smaller than capModels(): with tierPolicy(), which names all six
  // catalogue ids, "offered" and "the catalogue" are the same set and no assertion
  // about the difference can mean anything.
  return {
    rules: [], default: {},
    tiers: { T1: { model: 'glm-4.7', provider: 'zai', billing_mode: 'plan' } },
  };
}

function statusWithConfigured(extra) {
  return Object.assign({
    enabled: true,
    configured_models: [
      { model: 'us.anthropic.claude-opus-5', provider: 'bedrock', source: 'model.default' },
      { model: 'glm-5.3-flash', provider: 'zai', source: 'fallback_providers' },
    ],
  }, extra || {});
}

test('a model picker offers what this install is configured with, not the whole catalogue', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = oneModelPolicy();
  api.state.capabilities = capModels();
  api.state.status = statusWithConfigured();
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });

  const wrap = byLabel(dom.get('inspector'), 'Modelo');
  const select = wrap.children.find((c) => c.tagName === 'select');
  const offered = [];
  const walk = (n) => (n.children || []).forEach((k) => {
    if (k.tagName === 'option' && k.value) offered.push(k.value);
    walk(k);
  });
  walk(select);

  assert.ok(offered.includes('us.anthropic.claude-opus-5'),
    'the id this install actually runs on is offered, even though the catalogue has no entry for it');
  assert.ok(offered.includes('glm-5.3-flash'), 'and every other configured rail');
  // The catalogue's ids are NOT the default offer.
  assert.equal(offered.includes('deepseek-v4-pro'), false,
    'a catalogue model this install has no credential for is not offered by default');
  assert.equal(offered.includes('gpt-5.5'), false);
});

test('a model the policy already names stays offerable even when the install stopped listing it', () => {
  // Otherwise editing a group would silently drop the very model it routes on: the
  // select would not contain its own current value, and saving would rewrite it.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.capabilities = capModels();
  // The install is configured with something else entirely.
  api.state.status = statusWithConfigured({
    configured_models: [{ model: 'us.anthropic.claude-opus-5', provider: 'bedrock', source: 'model.default' }],
  });
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });

  const wrap = byLabel(dom.get('inspector'), 'Modelo');
  const select = wrap.children.find((c) => c.tagName === 'select');
  const offered = [];
  const walk = (n) => (n.children || []).forEach((k) => {
    if (k.tagName === 'option' && k.value) offered.push(k.value);
    walk(k);
  });
  walk(select);
  const primary = tierPolicy().tiers.T1.model;
  assert.ok(offered.includes(primary),
    `the group's own primary (${primary}) must stay in its picker`);
});

test('the catalogue is still reachable, as a gesture rather than the default', () => {
  // Nothing is lost: adding a model the install has not been configured with yet is
  // exactly what the toggle is for, and its words say which list is which.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = oneModelPolicy();
  api.state.capabilities = capModels();
  api.state.status = statusWithConfigured();
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });

  const wrap = byLabel(dom.get('inspector'), 'Modelo');
  const toggle = wrap.children.find((c) => c.className === 'btn' && /catálogo/.test(c.textContent || ''));
  assert.ok(toggle, `a control that reveals the catalogue must exist, got: ${wrap.children.map((c) => c.textContent).join(' | ')}`);
  assert.equal(toggle.hidden, false);
  toggle._listeners.click();
  const select = wrap.children.find((c) => c.tagName === 'select');
  const offered = [];
  const walk = (n) => (n.children || []).forEach((k) => {
    if (k.tagName === 'option' && k.value) offered.push(k.value);
    walk(k);
  });
  walk(select);
  assert.ok(offered.includes('deepseek-v4-pro'),
    'after the gesture the whole catalogue is selectable again');
});

test('with no served configured list the picker falls back to the catalogue, and says nothing false', () => {
  // An older sidecar serves no configured_models. Offering NOTHING would be worse
  // than offering the catalogue, so the old behaviour is the floor.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.capabilities = capModels();
  api.state.status = { enabled: true };
  api.renderInspector({ id: 'tier:T1', name: 'T1', bind: 'tier', tier: 'T1' });

  const wrap = byLabel(dom.get('inspector'), 'Modelo');
  const select = wrap.children.find((c) => c.tagName === 'select');
  const offered = [];
  const walk = (n) => (n.children || []).forEach((k) => {
    if (k.tagName === 'option' && k.value) offered.push(k.value);
    walk(k);
  });
  walk(select);
  assert.ok(offered.includes('deepseek-v4-pro') && offered.includes('gpt-5.5'),
    'no served list means the catalogue is the list, exactly as before');
});

test('the compaction picker uses the same authority, not its own copy of the catalogue', () => {
  // It is the ONE model select that does not go through modelField — it builds its
  // own — so "padronizar" means it has to be pointed at the same authority or the
  // standardisation would have a hole in it.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.capabilities = capModels();
  api.state.status = statusWithConfigured();
  api.state.compaction = compactionPayload();
  api.renderCompaction();

  const selects = findAll(dom.get('compactionBox'), 'ctl').filter((n) => n.tagName === 'select');
  assert.ok(selects.length, 'the compaction model select exists');
  const offered = [];
  (selects[0].children || []).forEach((o) => { if (o.value) offered.push(o.value); });
  assert.ok(offered.includes('us.anthropic.claude-opus-5'),
    'the configured ids reach the compaction picker too');
  assert.equal(offered.includes('gpt-5.5'), false,
    'and a catalogue model this install cannot call does not');
});

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

test('an EDITABLE surface opens its editor directly — no unlock note survives (§4.7)', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.state.capabilities = capModels();
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'tier', tier: 'T2' });
  assert.doesNotMatch(flat(dom.get('inspector')),
    /Só leitura/, 'the mode-era unlock phrase is gone');
  assert.ok(byLabel(dom.get('inspector'), 'Modelo'),
    'the editor is what a click opens — click-to-edit, no gesture to unlock');
});

test('a runtime node says why nothing is configurable, and never says "Só leitura"', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = tierPolicy();
  api.renderInspector({ id: 'tier:T2', name: 'T2', bind: 'replay', runtime: true });
  assert.match(flat(dom.get('inspector')), /Estado de execução/,
    'the one surface that cannot be edited says why');
  assert.doesNotMatch(flat(dom.get('inspector')), /Só leitura/,
    'and it never borrows the mode-era unlock phrase');
});

test('a §2.8 warning never disables or hides the write controls', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: { T1: { model: 'glm-4.7', provider: 'zai' } } };
  api.renderLadder();
  assert.match(flat(dom.get('ladder')), /só uma opção/, 'the warning is on screen');
  // The one thing that can make Salvar leave the DOM is a lint error (§3.4(a));
  // a §2.8 warning is advice, so the button stays in its container. The stub
  // has no markup parser, so the actions row is seeded the way the markup
  // declares it (see the bansMsg pattern above).
  const actions = dom.get('jsonActions');
  actions.append(dom.get('jsonPreview'), dom.get('jsonRevert'), dom.get('jsonApply'));
  api.renderWarnings();
  assert.ok(actions.children.includes(dom.get('jsonApply')),
    'Salvar stays in its container — avisar nunca é bloquear, the only gate is the server lint');
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

test('the missing-group row always shows the §3.4(a) inline destination select — no mode to arm', () => {
  const { api, dom } = loadConsole();
  missingGroupState(api);
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

  // There is no mode that hides a remedy: a re-render still carries the fix,
  // because the row that needs it never stops needing it.
  api.renderSheet();
  assert.equal(findAll(dom.get('sheet'), 'step-dest-fix').length, 1,
    'a re-render keeps the fix — absence only ever meant a mode, and the mode is gone');
});

test('choosing a destination on the §3.4(a) row writes the DRAFT and opens the rule editor', () => {
  const { api, dom } = loadConsole();
  missingGroupState(api);
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

test('while the file lints bad there is no Salvar in the DOM at all (§3.4(a))', () => {
  // The write path is always armed — the mode is gone — so the one thing that
  // can make Salvar leave the DOM is a lint error: absent, not disabled
  // (DESIGN.md:435-463). The mode-era editNote support text is gone with the
  // mode; the absence itself is the message now.
  const src = fs.readFileSync(sourcePath, 'utf8');
  assert.doesNotMatch(src, /id="editNote"/, 'the mode-era support text is gone from the markup');
  const { api, dom } = loadConsole();
  // The stub has no markup parser, so the actions row is seeded the way the
  // markup declares it (see the bansMsg pattern above).
  const actions = dom.get('jsonActions');
  actions.append(dom.get('jsonPreview'), dom.get('jsonRevert'), dom.get('jsonApply'));
  missingGroupState(api);
  api.renderWarnings();
  assert.ok(!dom.get('jsonActions').children.includes(dom.get('jsonApply')),
    'while the file lints bad there is no Salvar in the DOM at all');
  assert.ok(dom.get('jsonActions').children.includes(dom.get('jsonRevert')),
    'Voltar à versão anterior stays — it is two clicks, and it restores a .bak, not the broken file');

  // The gate rides the error set, not a mode: a clean file puts Salvar back,
  // in its own place — before "Ver o que muda", the order the flow reads.
  api.state.status = { validation_errors: [], error_targets: [], enabled: true };
  api.renderWarnings();
  assert.ok(actions.children.includes(dom.get('jsonApply')),
    'a clean file brings Salvar back');
  assert.ok(actions.children.indexOf(dom.get('jsonApply'))
    < actions.children.indexOf(dom.get('jsonPreview')),
    'and it lands before Ver o que muda — see what changes, then save');
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
// with the whole list minus the item), gated on the same staleness read — with
// one difference: GET /policy does not project blocklist, so the guard re-reads
// the /blocklist the screen already reads. The control itself is always born:
// there is no mode to arm (card t_f81c24ee).

test('a manual ban always offers removal; a breaker cooldown never does', () => {
  const { api, dom } = loadConsole();
  api.state.policy = {};
  api.state.blocklist = {
    manual_bans: [{ model: 'glm-5.3' }],
    breaker_cooldowns: [{ model_key: 'deepseek-v4-pro', cooldown_remaining_s: 300 }],
    fallback_chain: [],
  };
  api.renderHealth();
  assert.match(flat(dom.get('bans')), /banido/, 'a manual ban is named with the pt-BR state word');
  assert.match(flat(dom.get('bans')), /faltam 300s/, 'a breaker cooldown says the time owed in pt-BR, unit included');
  // The lift is always one tap away — the mode that used to hide it protected
  // nothing and cost a click on every removal (§3.3). The only gates are the
  // server's: token, CSRF, base_hash, lint.
  // Counted on the ROW, not on the block: the block also carries the gesture that
  // puts a model out of rotation (its own test above), so a block-wide count would
  // conflate the two controls and stop meaning "this row is liftable".
  const buttons = findAll(dom.get('bans').children[0], 'btn');
  assert.equal(buttons.length, 1, 'the manual ban row carries its control without arming anything');
  assert.equal(buttons[0].textContent, 'Remover o bloqueio');
  const breakerRow = dom.get('bans').children[1];
  assert.equal(findAll(breakerRow, 'btn').length, 0,
    'a breaker cooldown is not removable by hand: it expires on its own');
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
  // The bans block is drawn by renderHealth, which boot would call — but the
  // test harness strips the boot (loadConsole), so the draw is explicit here.
  api.renderHealth();
  // The markup ships the message line hidden; the fake DOM cannot read the
  // attribute, so the test arms the state the markup declares.
  dom.get('bansMsg').hidden = true;
  // The FIRST row's lift, addressed through the row — the block's last child is the
  // add control now, whose button is a ban, not a lift.
  findAll(dom.get('bans').children[0], 'btn')[0]._listeners.click();
  await tick();

  assert.equal(planBodies.length, 1);
  assert.deepEqual(planBodies[0], { blocklist: { manual_ban: [{ model: 'deepseek-v4-pro' }] } },
    'the plan body is the whole list WITHOUT the lifted item, and no other top-level key');
  assert.match(dom.get('bansMsg').textContent, /Vale para as próximas tarefas/,
    '§2.7: a written save says the temporal scope');
  assert.equal(dom.get('bansMsg').hidden, false, 'the message line stops hiding once a write speaks');
  const lifts = findAll(dom.get('bans'), 'btn').filter((b) => b.textContent === 'Remover o bloqueio');
  assert.equal(lifts.length, 1,
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
  // The bans block is drawn by renderHealth, which boot would call — but the
  // test harness strips the boot (loadConsole), so the draw is explicit here.
  api.renderHealth();
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
  api.renderHealth();
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
  // The bans block is drawn by renderHealth, which boot would call — but the
  // test harness strips the boot (loadConsole), so the draw is explicit here.
  api.renderHealth();
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

// ── JSON IS OPTIONAL, WHICH MEANS EVERY WRITABLE KEY HAS A CONTROL ──────────
// Two of the nine keys the server accepts had none, and both failed the same way:
// the value existed on screen and there was no way to change it there.
//
//   * blocklist.manual_ban could only be REMOVED. There was no add control at
//     all, and the block that would hold one hid itself whenever nobody was
//     banned — which is exactly when an operator wants to ban somebody. So a ban
//     could only be typed into the JSON editor.
//   * `enabled`, the router's master switch, was REPORTED in the Modelos lede
//     ("Roteamento: ligado") and had no control anywhere.
test('a model goes out of rotation from the screen, with no JSON editor', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = chipPolicy();
  api.state.status = { enabled: true };
  api.state.blocklist = { manual_bans: [], breaker_cooldowns: [], fallback_chain: [] };
  api.renderHealth();
  assert.equal(dom.get('bansGroup').hidden, false,
    'with nobody banned the block stays, because it carries the gesture that bans');
  const pick = findAll(dom.get('bans'), 'ban-pick')[0];
  assert.ok(pick, 'the model is CHOSEN from a list, never typed');
  const ids = pick.children.map((o) => o.value);
  assert.ok(ids.includes('gpt-5.6-terra') && ids.includes('glm-5.3'),
    `the candidates are the models this policy routes with, got ${ids.join(', ')}`);
  assert.equal(ids[0], '', 'and the list opens on no choice, so a stray change bans nobody');
  // Choosing arms the same proposal spine the price and compaction forms use —
  // a ban takes a rail out of rotation, so it is confirmed, never a side effect
  // of touching a select.
  pick.value = 'glm-5.3';
  pick._listeners.change();
  const area = findAll(dom.get('bans'), 'proposal-row')[0];
  assert.ok(area, 'the proposal row is mounted');
  assert.equal(area.hidden, false, 'and armed by the choice');
  assert.match(flat(area), /glm-5\.3/, 'the consequence names the model that stops being used');
  assert.equal(findAll(area, 'btn')[0].textContent, api.WRITE.banSave);
});

test('confirming a ban plans the whole list with the new row, and nothing else', async () => {
  const planBodies = [];
  const server = {
    blocklist: {
      manual_bans: [{ model: 'gpt-5.6-terra' }],
      fallback_chain: [], breaker_enabled: true, breaker_cooldowns: [],
    },
  };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (url.endsWith('/blocklist')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(server.blocklist)) });
      }
      // The freshness guard re-reads /policy and /blocklist and compares them to
      // what this screen holds, so the stub has to serve the SAME documents or
      // every write is (correctly) refused as stale.
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(chipPolicy())) });
      }
      if (url.endsWith('/plan')) {
        const policy = JSON.parse(opts.body).policy;
        planBodies.push(policy);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy, diff: '+ glm-5.3', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.policy = chipPolicy();
  api.state.blocklist = server.blocklist;
  api.renderHealth();
  const pick = findAll(dom.get('bans'), 'ban-pick')[0];
  pick.value = 'glm-5.3';
  pick._listeners.change();
  findAll(findAll(dom.get('bans'), 'proposal-row')[0], 'btn')[0]._listeners.click();
  await tick();
  assert.equal(planBodies.length, 1);
  assert.deepEqual(planBodies[0],
    { blocklist: { manual_ban: [{ model: 'gpt-5.6-terra' }, { model: 'glm-5.3' }] } },
    'the whole list PLUS the new row — the server merge replaces lists wholesale, '
    + 'so a one-item patch would delete every existing ban — and no other top-level key');
});

test('ago and utcClock agree about a timestamp neither can read', () => {
  // The decision row composes both halves into one string:
  //   [ago(r.ts, nowUtc()), utcClock(r.ts)].filter(Boolean).join(' · ')
  // utcClock has a Number.isFinite gate and renders nothing for junk; ago had only a
  // falsy check, so a TRUTHY unreadable ts (an ISO string where epoch seconds belong)
  // made every threshold comparison false and the day branch returned
  // `há ${Math.round(NaN / 86400)}d` — "há NaNd", printed beside a clock that had
  // correctly declined to answer. Two halves of one expression, opposite verdicts.
  //
  // Nothing in the repo emits such a ts today (decision_log writes time.time()), so
  // this is the asymmetry being removed rather than an operator-visible bug — the same
  // reasoning pickRoute already states next door: "Junk is no instant at all".
  const { api } = loadConsole();
  const now = new Date(Date.UTC(2026, 7, 19, 12, 0, 0));
  assert.equal(api.ago(0, now), '—', 'falsy stays the absence it already was');
  assert.equal(api.ago(undefined, now), '—');
  assert.equal(api.utcClock('2026-09-01T10:00:00+00:00'), '',
    'the clock half declines an unreadable instant');
  assert.equal(api.ago('2026-09-01T10:00:00+00:00', now), '—',
    'and so does the age half, instead of saying "há NaNd" beside it');
  assert.equal(api.ago(NaN, now), '—');
  // A real timestamp still reads normally.
  assert.equal(api.ago(Math.floor(now.getTime() / 1000) - 120, now), 'há 2m');

});

test('one unreadable mtime does not poison the header line it sits on', () => {
  // fmtAge is ago's sibling — it composes the header's provenance line — and had the
  // same hole in the same place: absent was guarded ('—'), unreadable was not, so
  // `new Date('not a date').getTime()` is NaN and the day branch returned "NaNd".
  //
  // The branch is entered on `procStart` ALONE, so a single bad mtime used to poison
  // its own clause while the other two read correctly. The sidecar serves isoformat()
  // and OMITS a field it cannot answer, so this closes the asymmetry rather than a
  // bug an operator hits — asserted through renderRail because fmtAge is internal and
  // widening the export surface for a test would be the wrong trade.
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.unreachable = false;
  api.state.status = {
    process_started_at: new Date(T - 2 * 3600 * 1000).toISOString(),
    code_mtime: 'not a date',
    config_mtime: new Date(T - 5 * 60 * 1000).toISOString(),
  };
  api.renderRail();
  const text = dom.get('reachText').textContent;
  assert.doesNotMatch(text, /NaN/, 'no NaN in the provenance line');
  assert.match(text, /código carregado há —/, 'the unreadable one states the absence');
  assert.match(text, /serviço no ar há 2h/, 'and its neighbours still read normally');
  assert.match(text, /arquivo mudou há 5m/);
});

test('a clause with no value prefills empty, never the literal word null', () => {
  // Reachable straight from an operator's file: `when: {keywords: {contains: }}` is
  // valid YAML for `{contains: null}`, /policy projects `when` verbatim, and the
  // inspector is not gated on the policy being tidy. null is not boolean, not number
  // and not an array, so it fell to the else branch and the input came up holding the
  // four characters "null" — a value the operator never wrote, offered back as if
  // they had.
  //
  // The commit guard below it (`if (String(next) === String(value)) return`) means
  // retyping "null" cannot be SAVED, so this is a display lie rather than a
  // corrupting write. The fix is the idiom this file already uses at three other
  // inputs: `x == null ? '' : String(x)`.
  const { api, dom } = loadConsole();
  api.state.loading = false;
  const policy = sheetPolicy();
  policy.rules[0].when = { keywords: { contains: null } };
  api.state.policy = policy;
  api.renderSheet();
  const row = dom.get('sheet').children.find((c) => c.dataset.ruleId === 'audit');
  findAll(row, 'step-when')[0]._listeners.click();
  const wrap = byLabel(dom.get('inspector'), 'keywords (contains)');
  assert.ok(wrap, 'the clause is still offered for editing');
  const input = wrap.children.find((c) => c.tagName === 'input');
  assert.equal(input.value, '', 'an absent value is an empty field, not the word "null"');
});

test('a saved ban says so where the message survives the reload it triggers', async () => {
  // The write reloads, and the reload rebuilds #bans — so a confirmation written
  // into the proposal row's own .msg is destroyed by the very success it reports.
  // Found in the real Hermes One app against the live local stack (2026-09-02): the
  // ban landed on disk and the screen said NOTHING.
  //
  // #bansMsg exists outside the group for exactly this reason; the markup says so
  // ("the confirmation must not hide with it") and the LIFT already uses it. This is
  // the add path joining it.
  const server = { blocklist: { manual_bans: [], fallback_chain: [], breaker_enabled: true, breaker_cooldowns: [] } };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (url.endsWith('/blocklist')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(server.blocklist)) });
      }
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(chipPolicy())) });
      }
      if (url.endsWith('/plan')) {
        const policy = JSON.parse(opts.body).policy;
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy, diff: '+ glm-5.3', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.policy = chipPolicy();
  api.state.blocklist = server.blocklist;
  api.renderHealth();
  // The markup ships this hidden; the stub cannot read the attribute.
  dom.get('bansMsg').hidden = true;
  const pick = findAll(dom.get('bans'), 'ban-pick')[0];
  pick.value = 'glm-5.3';
  pick._listeners.change();
  findAll(findAll(dom.get('bans'), 'proposal-row')[0], 'btn')[0]._listeners.click();
  await tick();
  assert.match(dom.get('bansMsg').textContent, /Vale para as próximas tarefas/,
    '§2.7: the saved ban states its temporal scope, in the node that outlives the rebuild');
  assert.equal(dom.get('bansMsg').hidden, false, 'and that node stops hiding once the write speaks');
});

test('the router is switched off from the fact that reports it, with no JSON editor', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.policy = chipPolicy();
  api.state.status = { enabled: true };
  api.renderHealth();
  const value = findAll(dom.get('healthFacts'), 'fact-value')
    .find((n) => n.textContent === api.WRITE.routingOn);
  assert.ok(value, 'the routing fact still says its state');
  assert.ok(value.classList.contains('is-edit'),
    'and the state IS the control — one vocabulary: a value you can change is underlined');
  value._listeners.click();
  const area = findAll(dom.get('healthFacts'), 'proposal-row')[0];
  assert.ok(area, 'clicking arms a proposal rather than writing on the spot');
  assert.equal(area.hidden, false);
  assert.match(flat(area), /nenhuma tarefa/,
    'the consequence says what switching the router off actually does');
  assert.equal(findAll(area, 'btn')[0].textContent, api.WRITE.routingStop);
});

test('the master switch writes only `enabled`, and reads its own current value', async () => {
  const planBodies = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      // Same document the screen holds: the freshness guard re-reads /policy and
      // refuses the write when it differs.
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(chipPolicy())) });
      }
      if (url.endsWith('/plan')) {
        const policy = JSON.parse(opts.body).policy;
        planBodies.push(policy);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy, diff: '- enabled: true', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.policy = chipPolicy();
  api.state.status = { enabled: true };
  api.renderHealth();
  const on = findAll(dom.get('healthFacts'), 'fact-value').find((n) => n.textContent === api.WRITE.routingOn);
  on._listeners.click();
  findAll(findAll(dom.get('healthFacts'), 'proposal-row')[0], 'btn')[0]._listeners.click();
  await tick();
  assert.deepEqual(planBodies, [{ enabled: false }],
    'switching off writes exactly one key, and no policy fragment rides along');
  // And the other direction: a router already off offers to turn it ON.
  api.state.status = { enabled: false };
  api.renderHealth();
  const off = findAll(dom.get('healthFacts'), 'fact-value').find((n) => n.textContent === api.WRITE.routingOff);
  assert.ok(off, 'the fact reports the off state');
  off._listeners.click();
  const area = findAll(dom.get('healthFacts'), 'proposal-row')[0];
  assert.equal(findAll(area, 'btn')[0].textContent, api.WRITE.routingStart,
    'the button names the direction it would move, never a generic Salvar');
  findAll(area, 'btn')[0]._listeners.click();
  await tick();
  assert.deepEqual(planBodies[1], { enabled: true });
});

// ── ONE QUEUE VOCABULARY ───────────────────────────────────────────────────────
// "Até você se perde nessa configuração de models e fallback - de tão complexa que ela
// é. Talvez só alinhando a forma como fazemos as coisas na UI já fique bom."
//
// Counted, across both files: FIVE spellings of one idea.
//   fallback_providers        [{label, provider, model, base_url?}]
//   auxiliary.vision          {provider, model, fallback_chain: [{provider, model}]}
//   tiers.Tn                  {model, provider, fallback: [...], fallback_strategy, …}
//   blocklist.fallback_chain  [bare strings]
//   compaction                fallback_mode: standalone|tier:Tn + fallback_chain
//
// Every one is a PRIMARY and its RESERVES tried in order. The console already has one
// renderer for that — chainList, which serves the rule sheet, the tier chains and the
// probed plan — and three of the five were not going through it. These pin that they do.

// ── the classifier: the sixth queue, and its knobs ────────────────────────────
// "não posso mudar o classifier?" — half of it. The door exists (the "decide na hora"
// cell) and so does an editor, and that editor offered Modelo + Provedor. The block also
// carries a `chain` (its own fallback queue, the SIXTH spelling in these two files) plus
// on_total_failure, temperature, max_tokens and timeout_seconds — none of it on screen,
// because /policy did not project `classifier` at all and /status sends only two fields.

function classifierPolicy() {
  return {
    rules: [{ id: 'ask', when: {}, then: { action: 'classify' } }],
    default: { action: 'classify' },
    tiers: { T1: { model: 'glm-4.7', provider: 'zai' } },
    classifier: {
      model: 'glm-4.7', provider: 'zai',
      chain: [
        { model: 'deepseek-v4-pro', provider: 'deepseek' },
        { model: 'gpt-5.5', provider: 'openai-codex' },
      ],
      on_total_failure: 'heuristic', temperature: 0, max_tokens: 128, timeout_seconds: 15,
    },
  };
}

test('the classifier is a queue like a group is, drawn and reordered the same way', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = classifierPolicy();
  api.state.capabilities = capModels();
  api.renderInspector({ id: 'classifier', name: 'classifier', bind: 'classifier' });
  const box = dom.get('inspector');

  const heads = findAll(box, 'chain-head').map((n) => n.textContent);
  assert.deepEqual(heads, ['Primeira tentativa', 'Reserva', 'Reserva'],
    'model+provider is attempt 1 and `chain` are its reserves — the tier vocabulary');
  const rows = findAll(box, 'chain-row');
  assert.equal(rows.length, 3);
  assert.deepEqual(rows.map((r) => rowField(r, 'Modelo').children.find((c) => c.tagName === 'select').value),
    ['glm-4.7', 'deepseek-v4-pro', 'gpt-5.5'], 'file order is screen order');
  rows.forEach((r, i) => {
    assert.deepEqual(rowButtons(r).children.map((b) => b.textContent), ['↑', '↓', 'Remover'],
      `row ${i} carries the same three controls every other queue gives an attempt`);
  });
});

test('the classifier knobs the file carries all reach the screen', () => {
  // Three of seven fields was a console silently dropping four operator decisions.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = classifierPolicy();
  api.state.capabilities = capModels();
  api.renderInspector({ id: 'classifier', name: 'classifier', bind: 'classifier' });
  const box = dom.get('inspector');
  ['Se todas falharem', 'Temperatura', 'Máximo de tokens', 'Tempo limite (s)'].forEach((label) => {
    assert.ok(byLabel(box, label), `${label} must be editable, it is in the file`);
  });
  // on_total_failure is a CLOSED set — the engine accepts two answers, so free text here
  // would be a field that lints clean and does nothing.
  const wrap = byLabel(box, 'Se todas falharem');
  const sel = wrap.children.find((c) => c.tagName === 'select');
  assert.ok(sel, 'it is a choice, not free text');
  assert.deepEqual(sel.children.map((o) => o.value).filter(Boolean), ['heuristic', 'fail_safe']);
});

test('moving a classifier reserve up writes the order that will be saved', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = classifierPolicy();
  api.state.capabilities = capModels();
  api.renderInspector({ id: 'classifier', name: 'classifier', bind: 'classifier' });
  const box = dom.get('inspector');
  // ↑ on the FIRST reserve promotes it to the primary, exactly as a tier's does.
  findAll(findAll(box, 'chain-row')[1], 'btn').find((b) => b.textContent === '↑')._listeners.click();
  const draft = api.state.draft.classifier;
  assert.equal(draft.model, 'deepseek-v4-pro', 'the promoted reserve became the first attempt');
  assert.deepEqual(draft.chain.map((h) => h.model), ['glm-4.7', 'gpt-5.5'],
    'and the demoted primary is the head of the reserves');
});

test('a classifier the file does not configure still opens, and offers to configure it', () => {
  // `{}` is served for an unconfigured block; the editor must not blank out.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  const policy = classifierPolicy();
  policy.classifier = {};
  api.state.policy = policy;
  api.state.capabilities = capModels();
  api.renderInspector({ id: 'classifier', name: 'classifier', bind: 'classifier' });
  const box = dom.get('inspector');
  assert.ok(byLabel(box, 'Modelo'), 'the first attempt is offered even with nothing set');
  assert.equal(findAll(box, 'chain-row').length, 1, 'one row: the primary, with no reserves');
});

test('the compaction block says when saving records a choice nothing here can enact', () => {
  // Measured on both installs, 2026-09-03: writing `compaction` into router.yaml works
  // (ordinary hot /plan + /apply). Projecting it into Hermes' own auxiliary.compression is
  // a RESTART-class apply that hands a candidate to ~/bin/hermes-safe-restart.sh — absent
  // on the WSL box AND in the docker container — and the console never exposed that step
  // (no action=compaction, no COMPACT confirm anywhere in this file).
  //
  // So: choose a model, press Gravar, read "Salvo", and nothing about summarisation
  // changes. The server's refusal was honest wherever it was reached; nothing reached it.
  // The screen has to say which half it did.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.capabilities = capModels();
  const payload = compactionPayload();
  payload.effective_apply = {
    available: false,
    reason: 'safe-restart launcher not found at /home/x/bin/hermes-safe-restart.sh — the '
      + 'compaction choice is recorded in router.yaml but nothing projects it into config.yaml here',
  };
  api.state.compaction = payload;
  api.renderCompaction();
  const drawn = flat(dom.get('compactionBox'));
  assert.match(drawn, /registra a escolha/,
    'it says that saving records the choice rather than enacting it');
  assert.match(drawn, /hermes-safe-restart\.sh/,
    'and names the exact thing that is missing — installing it is the whole remedy');
});

test('with the projection available the block says nothing extra', () => {
  // No empty disclosure: a warning that fires when nothing is wrong trains the operator
  // to ignore warnings.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.capabilities = capModels();
  const payload = compactionPayload();
  payload.effective_apply = { available: true, reason: '' };
  api.state.compaction = payload;
  api.renderCompaction();
  const drawn = flat(dom.get('compactionBox'));
  assert.doesNotMatch(drawn, /registra a escolha/);
  assert.doesNotMatch(drawn, /safe-restart/);
});

test('an older sidecar that does not report the projection says nothing either', () => {
  // Absent is not false: a sidecar too old to answer must not make the console claim the
  // projection is broken.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.capabilities = capModels();
  api.state.compaction = compactionPayload();
  api.renderCompaction();
  assert.doesNotMatch(flat(dom.get('compactionBox')), /registra a escolha/);
});

test('the compaction reserve is a queue you can see and reorder, not a chip cloud', () => {
  // Its own note promised what the control could not deliver: "escolha os modelos da
  // reserva, NA ORDEM — o primeiro é tentado primeiro", offered as toggle chips. Chips
  // carry no order: what you got was insertion order, invisible and unchangeable. A
  // control that states an order it cannot show is the worst of the five spellings.
  //
  // Now it is the same queue shape as a group's: ordinals, ↑ ↓ Remover per attempt, and
  // the chips demoted to what they always were — the way to ADD one.
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.capabilities = capModels();
  api.state.policy = tierPolicy();
  const payload = compactionPayload();
  payload.compaction = {
    provider: 'zai', model: 'glm-4.7', fallback_mode: 'standalone',
    fallback_chain: [{ model: 'deepseek-v4-pro', provider: 'deepseek' },
                     { model: 'gpt-5.5', provider: 'openai-codex' }],
  };
  api.state.compaction = payload;
  api.renderCompaction();
  const box = dom.get('compactionBox');

  const ordered = findAll(box, 'hops').filter((n) => /ordered/.test(n.className));
  assert.ok(ordered.length, 'the reserve is drawn as an ORDERED queue, so the order is visible');
  const models = findAll(box, 'hop-model').map((n) => n.textContent);
  assert.deepEqual(models, ['deepseek-v4-pro', 'gpt-5.5'],
    'in the order the file declares — which is the order it is tried in');

  // The same three controls the tier editor gives every attempt.
  const rows = findAll(box, 'cq-row');
  assert.equal(rows.length, 2, 'one control row per attempt');
  assert.deepEqual(rows.map((r) => findAll(r, 'btn').map((b) => b.textContent)),
    [['↑', '↓', 'Remover'], ['↑', '↓', 'Remover']],
    'the same ↑ ↓ Remover vocabulary as a group queue');
});

test('moving a compaction attempt up changes the order that will be saved', () => {
  const posted = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      // The freshness guard re-reads /policy and compares it to what the screen holds,
      // so the stub must serve the SAME document or the write is (correctly) refused.
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200,
          text: () => Promise.resolve(JSON.stringify(tierPolicy())) });
      }
      if (url.endsWith('/plan')) {
        posted.push(JSON.parse(opts.body).policy);
        return Promise.resolve({ ok: true, status: 200,
          text: () => Promise.resolve(JSON.stringify({ valid: true, policy: {}, diff: 'x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  api.state.capabilities = capModels();
  api.state.policy = tierPolicy();
  const payload = compactionPayload();
  payload.compaction = {
    provider: 'zai', model: 'glm-4.7', fallback_mode: 'standalone',
    fallback_chain: [{ model: 'deepseek-v4-pro', provider: 'deepseek' },
                     { model: 'gpt-5.5', provider: 'openai-codex' }],
  };
  api.state.compaction = payload;
  api.renderCompaction();
  const box = dom.get('compactionBox');
  // ↑ on the SECOND attempt promotes it.
  const second = findAll(box, 'cq-row')[1];
  findAll(second, 'btn')[0]._listeners.click();
  const after = findAll(dom.get('compactionBox'), 'hop-model').map((n) => n.textContent);
  assert.deepEqual(after, ['gpt-5.5', 'deepseek-v4-pro'], 'screen order follows the move');
  // And the armed proposal carries that order, so what is saved is what is shown.
  const area = findAll(dom.get('compactionBox'), 'proposal-row')[0];
  assert.equal(area.hidden, false, 'the move arms the block\'s proposal');
  findAll(area, 'btn')[0]._listeners.click();
  return tick().then(() => {
    assert.ok(posted.length, 'a plan was requested');
    const chain = posted[0].compaction.fallback_chain.map((h) => h.model);
    assert.deepEqual(chain, ['gpt-5.5', 'deepseek-v4-pro'],
      'the saved order is the order on screen');
  });
});

test('↑ on the first attempt and ↓ on the last move nothing', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.capabilities = capModels();
  api.state.policy = tierPolicy();
  const payload = compactionPayload();
  payload.compaction = {
    provider: 'zai', model: 'glm-4.7', fallback_mode: 'standalone',
    fallback_chain: [{ model: 'deepseek-v4-pro', provider: 'deepseek' },
                     { model: 'gpt-5.5', provider: 'openai-codex' }],
  };
  api.state.compaction = payload;
  api.renderCompaction();
  const rows = () => findAll(dom.get('compactionBox'), 'cq-row');
  findAll(rows()[0], 'btn')[0]._listeners.click();   // ↑ on the first
  findAll(rows()[1], 'btn')[1]._listeners.click();   // ↓ on the last
  assert.deepEqual(findAll(dom.get('compactionBox'), 'hop-model').map((n) => n.textContent),
    ['deepseek-v4-pro', 'gpt-5.5'], 'the ends of the queue are the ends');
});

test('Remover takes an attempt out of the compaction queue', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.capabilities = capModels();
  api.state.policy = tierPolicy();
  const payload = compactionPayload();
  payload.compaction = {
    provider: 'zai', model: 'glm-4.7', fallback_mode: 'standalone',
    fallback_chain: [{ model: 'deepseek-v4-pro', provider: 'deepseek' },
                     { model: 'gpt-5.5', provider: 'openai-codex' }],
  };
  api.state.compaction = payload;
  api.renderCompaction();
  const row = findAll(dom.get('compactionBox'), 'cq-row')[0];
  findAll(row, 'btn')[2]._listeners.click();
  assert.deepEqual(findAll(dom.get('compactionBox'), 'hop-model').map((n) => n.textContent),
    ['gpt-5.5'], 'the removed attempt is gone and the rest keep their order');
});

test('the substitute queue is drawn by the shared renderer, ordinals and all', () => {
  // It was a hand-rolled <ul class="hops drawn"> of bare spans: the same CSS classes as
  // a real chain, built by different code, with no provider and NO ORDINALS — while
  // Blocklist.fallback_for() walks this list POSITIONALLY. The order was load-bearing
  // and the drawing hid it.
  const { api, dom } = loadConsole();
  api.state.policy = {};
  api.state.capabilities = capModels();
  api.state.blocklist = {
    manual_bans: [{ model: 'glm-5.3' }],
    breaker_cooldowns: [],
    fallback_chain: ['deepseek-v4-pro', 'gpt-5.5'],
  };
  api.renderHealth();
  const box = dom.get('bans');
  const ordered = findAll(box, 'hops').filter((n) => /ordered/.test(n.className));
  assert.ok(ordered.length,
    'the substitute queue is an ORDERED chain, because fallback_for walks it positionally');
  const drawn = flat(box);
  assert.match(drawn, /deepseek-v4-pro/);
  assert.match(drawn, /gpt-5\.5/);
  // The shared renderer annotates from the catalogue; the hand-rolled one could not.
  assert.match(drawn, /deepseek/, 'and the rail each attempt bills to');
});

test('the agent\'s own chains are drawn in the same vocabulary, and say who owns them', () => {
  // model.default + fallback_providers IS a queue — that is what makes
  // `fallback_providers` legible: it is the RESERVES OF THE MAIN MODEL, not a list of
  // providers. Same for vision. Neither had any home in this console before.
  const { api, dom } = loadConsole();
  api.state.capabilities = capModels();
  api.state.status = {
    enabled: true,
    agent_queues: [
      { key: 'model', where: 'config.yaml: model.default + fallback_providers',
        editable: false,
        attempts: [
          { model: 'us.anthropic.claude-opus-5', provider: 'bedrock' },
          { model: 'us.anthropic.claude-sonnet-5', provider: 'bedrock' },
        ] },
      { key: 'auxiliary.vision', where: 'config.yaml: auxiliary.vision', editable: false,
        attempts: [{ model: 'us.anthropic.claude-opus-5', provider: 'bedrock' }] },
    ],
  };
  api.renderAgentQueues();
  const box = dom.get('agentQueues');
  // The GROUP is what the markup hides — the inner box is where the queues are drawn.
  assert.equal(dom.get('agentQueuesGroup').hidden, false,
    'the block shows when the install has chains to show');
  const drawn = flat(box);
  assert.match(drawn, /Modelo principal/, 'the main chain is named in operator words');
  assert.match(drawn, /Vis/, 'and vision');
  assert.match(drawn, /us\.anthropic\.claude-sonnet-5/, 'every attempt is listed');
  assert.match(drawn, /config\.yaml/, 'and each queue says which file and key owns it');
  // ORDERED, like every other queue whose order decides what runs first.
  assert.ok(findAll(box, 'hops').some((n) => /ordered/.test(n.className)));
  // No edit affordance: the router only READS config.yaml, and offering to edit it here
  // would put a second authority on a fact that already has one.
  assert.equal(findAll(box, 'is-edit').length, 0, 'read-only, and it does not pretend');
});

test('each agent queue hands you the exact YAML to paste, quoted so a colon cannot bite', () => {
  // The console cannot write config.yaml (four measured reasons live in the commit that
  // added this block), so the most actionable thing it can do is remove the TRANSCRIPTION
  // step — which is where the mistakes are. Copy, paste, adjust.
  //
  // Every scalar is quoted on purpose: `us.anthropic.claude-haiku-4-5-20251001-v1:0` ends
  // in `v1:0`. A bare colon-bearing plain scalar is legal YAML only because the colon has
  // no space after it, which is exactly the kind of accident that parses today and breaks
  // on the next id. Quoting removes the question.
  const { api, dom } = loadConsole();
  api.state.capabilities = capModels();
  api.state.status = {
    enabled: true,
    agent_queues: [
      { key: 'model', where: 'config.yaml: model.default + fallback_providers', editable: false,
        attempts: [
          { model: 'us.anthropic.claude-opus-5', provider: 'bedrock' },
          { model: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', provider: 'bedrock' },
        ] },
      { key: 'auxiliary.vision', where: 'config.yaml: auxiliary.vision', editable: false,
        attempts: [
          { model: 'us.anthropic.claude-opus-5', provider: 'bedrock' },
          { model: 'glm-4.5v', provider: 'zai' },
        ] },
    ],
  };
  api.renderAgentQueues();

  // The main chain spans TWO keys, so its fragment emits both.
  assert.equal(api.agentQueueYaml(api.state.status.agent_queues[0]),
    'model:\n'
    + '  default: "us.anthropic.claude-opus-5"\n'
    + '  provider: "bedrock"\n'
    + 'fallback_providers:\n'
    + '- provider: "bedrock"\n'
    + '  model: "us.anthropic.claude-haiku-4-5-20251001-v1:0"\n');

  assert.equal(api.agentQueueYaml(api.state.status.agent_queues[1]),
    'auxiliary:\n'
    + '  vision:\n'
    + '    provider: "bedrock"\n'
    + '    model: "us.anthropic.claude-opus-5"\n'
    + '    fallback_chain:\n'
    + '    - provider: "zai"\n'
    + '      model: "glm-4.5v"\n');

  // And a control that hands it over, one per queue.
  const buttons = findAll(dom.get('agentQueues'), 'btn')
    .filter((b) => /copiar/i.test(b.textContent || ''));
  assert.equal(buttons.length, 2, 'one copy control per queue');
});

test('a one-attempt agent queue emits no empty reserve list', () => {
  // An `auxiliary.vision` with no fallback_chain must not paste `fallback_chain:` with
  // nothing under it — that is a key an operator did not ask for, and in YAML it reads as
  // null rather than as "no reserves".
  const { api } = loadConsole();
  const single = { key: 'auxiliary.vision', attempts: [{ model: 'm', provider: 'p' }] };
  assert.equal(api.agentQueueYaml(single),
    'auxiliary:\n  vision:\n    provider: "p"\n    model: "m"\n');
  const mainOnly = { key: 'model', attempts: [{ model: 'm', provider: 'p' }] };
  assert.equal(api.agentQueueYaml(mainOnly),
    'model:\n  default: "m"\n  provider: "p"\n',
    'and a main chain with no reserves emits no fallback_providers key');
});

test('with no agent chains to show, the block is absent rather than empty', () => {
  // DESIGN.md rule 1. An older sidecar serves no agent_queues, and a heading with a
  // border around nothing is worse than no heading.
  const { api, dom } = loadConsole();
  api.state.status = { enabled: true };
  api.renderAgentQueues();
  assert.equal(dom.get('agentQueuesGroup').hidden, true);
  api.state.status = { enabled: true, agent_queues: [] };
  api.renderAgentQueues();
  assert.equal(dom.get('agentQueuesGroup').hidden, true, 'an empty list is still an absence');
  // And a served list whose entries carry no attempts is the same absence.
  api.state.status = { enabled: true, agent_queues: [{ key: 'model', attempts: [] }] };
  api.renderAgentQueues();
  assert.equal(dom.get('agentQueuesGroup').hidden, true, 'a queue with no attempts is not a queue');
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
  // The block itself no longer hides — it carries the ban gesture, which an
  // operator needs precisely when nobody is banned yet (see the JSON-optional
  // block above). What §2.6 is about is untouched and is what this pins: the
  // SUBSTITUTE QUEUE has nothing to substitute for and is not mounted.
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

// ── the reorder write (card: reordenar regras arrastando pelo punho) ──────
//
// Order is the engine's semantics, so every assertion here is about the
// WRITE, not the wiggle: which list reaches /apply, what the confirmation
// says, what warns before the write, and that the no-drag paths (keyboard,
// arrows) are the same write rather than a lesser parallel.

// The policy the reorder tests read: four rules where moving r4 above r2
// creates a shadow (r1 already covers what r4 asks) and moving r2/r3 in
// isolation does not. `when: {}` is not enough for shadowPairs (an empty
// when is skipped), so each rule carries one clause.
function reorderPolicy() {
  return {
    rules: [
      { id: 'r1', when: { size_lines: { gt: 10 } }, then: { model: 'T1' } },
      { id: 'r2', when: { size_lines: { gt: 100 } }, then: { model: 'T2' } },
      { id: 'r3', when: { needs_vision: { eq: true } }, then: { model: 'T3' } },
      { id: 'r4', when: { size_lines: { gt: 20 } }, then: { model: 'T4' } },
    ],
    default: { action: 'classify' },
    tiers: {
      T1: { model: 'a', provider: 'zai' },
      T2: { model: 'b', provider: 'zai' },
      T3: { model: 'c', provider: 'zai' },
      T4: { model: 'd', provider: 'zai' },
    },
    fail_safe: { model: 'glm-4.7', provider: 'zai' },
  };
}

test('reorderedRules moves one rule without touching the source list', () => {
  const { api } = loadConsole();
  const rules = reorderPolicy().rules;
  // Down: the rule lands at the target index, everything between shifts up.
  const down = api.reorderedRules(rules, 0, 2);
  assert.deepEqual(down.map((r) => r.id), ['r2', 'r3', 'r1', 'r4']);
  // Up: the rule lands at the target index, everything between shifts down.
  const up = api.reorderedRules(rules, 3, 1);
  assert.deepEqual(up.map((r) => r.id), ['r1', 'r4', 'r2', 'r3']);
  // The SOURCE is untouched — the write is built from a copy, never a
  // mutation of state.policy (a mutation there reads as an external edit to
  // the staleness guard and refuses every later save).
  assert.deepEqual(rules.map((r) => r.id), ['r1', 'r2', 'r3', 'r4']);
  // A move that is no movement, or leaves the array, is refused — a splice
  // outside the list would silently truncate it. `to === rules.length` is
  // INSIDE: it is the bottom half of the last row, "move to the end".
  assert.equal(api.reorderedRules(rules, 2, 2), null);
  assert.equal(api.reorderedRules(rules, -1, 0), null);
  assert.equal(api.reorderedRules(rules, 0, 5), null);
  assert.deepEqual(api.reorderedRules(rules, 0, 4).map((r) => r.id),
    ['r2', 'r3', 'r4', 'r1'], 'to === length lands after the last row');
  assert.equal(api.reorderedRules(null, 0, 1), null);
  // Every rule object is carried WHOLE: a reorder that rebuilt rules from
  // their rendered fields would drop everything the sheet does not show.
  assert.deepEqual(down[2], rules[0]);
});

test('the consequence sentence names the flip, in the file\'s own ids', () => {
  const { api } = loadConsole();
  const rules = reorderPolicy().rules;
  // Moving up: the moved rule now decides BEFORE the rule it lands in front
  // of — "antes de" is the precedence fact that changed.
  assert.equal(api.reorderConsequenceWords(rules, 3, 1),
    '“r4” passa a decidir antes de “r2”.');
  // Moving down: it now decides AFTER the rule it lands behind.
  assert.equal(api.reorderConsequenceWords(rules, 0, 2),
    '“r1” passa a decidir depois de “r3”.');
  // A rule with no id is named by its ordinal — the same fallback the
  // shadow vocabulary uses, so the two texts agree on who is who.
  const anon = rules.map((r, i) => ({ ...r, id: undefined }));
  assert.equal(api.reorderConsequenceWords(anon, 3, 1),
    '“regra 4” passa a decidir antes de “regra 2”.');
  // And the sentence never says the jargon the card forbids.
  const words = api.reorderConsequenceWords(rules, 3, 1);
  assert.ok(!/ordem alterada/i.test(words), 'never "ordem alterada"');
});

test('newShadowFindings reports only shadows the move itself creates', () => {
  const { api } = loadConsole();
  const rules = reorderPolicy().rules;
  // Baseline: r2 and r4 are already dead (r1 covers both) — that is the
  // sheet's known amber state before any move.
  const before = api.shadowPairs(rules);
  assert.equal(new Set(before.map((f) => f.later_id)).size, 2, 'fixture: r2 and r4 start shadowed');
  // A move that creates no new shadow reports none.
  const harmless = api.reorderedRules(rules, 1, 2);
  assert.equal(api.newShadowFindings(before, api.shadowPairs(harmless)).length, 0);
  // Moving r3 (live, needs_vision) BELOW r4 does not create a shadow —
  // different keys never subset.
  const crossFamily = api.reorderedRules(rules, 2, 3);
  assert.equal(api.newShadowFindings(before, api.shadowPairs(crossFamily)).length, 0);
  // Swapping r1's clause onto r3's place WOULD create one; the honest
  // construction is a synthetic list where a wide rule moves ABOVE a live
  // narrow one: r3 (needs_vision) with r1 (size_lines) in front covers
  // nothing new... so build it directly: a `size_lines gt 15` rule moved
  // above `size_lines gt 25` makes the latter newly dead.
  const livePair = [
    { id: 'narrow', when: { size_lines: { gt: 25 } }, then: { model: 'T1' } },
    { id: 'wide', when: { size_lines: { gt: 15 } }, then: { model: 'T2' } },
  ];
  assert.equal(api.shadowPairs(livePair).length, 0, 'fixture: narrow first, nothing dead');
  const wideFirst = api.reorderedRules(livePair, 1, 0);
  const created = api.newShadowFindings([], api.shadowPairs(wideFirst));
  assert.equal(created.length, 1);
  assert.equal(created[0].later_id, 'narrow', 'the formerly-live rule is the newly-dead one');
});

test('moveRule warns BEFORE writing when the new order creates a shadow', async () => {
  const posted = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(reorderPolicy())) });
      }
      // /plan echoes the DRAFT it was posted, merged over the file — what
      // the real sidecar does (service.py: /plan deep-merges then returns
      // the merged policy as the plan's own).
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.policy = reorderPolicy();
  const note = dom.get('reorderMsg');
  // Baseline shadows in the fixture: r1 (gt 10) already covers r2 (gt 100)
  // and r4 (gt 20) — the sheet's known amber state. Moving r4 to index 1
  // puts it in front of r2: r2's clause is inside r4's, so the pair
  // (r4 covers r2) is NEW — a rule the operator knew nothing about until
  // this move. The first attempt must refuse and name it; the repeat is
  // the confirmation.
  const first = await api.moveRule(3, 1);
  assert.equal(first, false, 'the first attempt refuses to write');
  assert.equal(posted.length, 0, 'nothing reaches the network before the warning is read');
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /fica sem efeito/, 'the warning says the consequence');
  assert.match(note.textContent, /a regra 3 nunca decide: a regra 2 já casa tudo que ela pede/,
    'and names the pair the shadow vocabulary always uses: ordinals, in the NEW order');
  // The SAME move repeated is the confirmation; now it writes.
  const second = await api.moveRule(3, 1);
  assert.equal(second, true);
  const apply = posted.find((p) => /\/apply$/.test(p.url));
  assert.ok(apply, 'the confirmed move reaches /apply');
  const sent = JSON.parse(apply.body).policy.rules.map((r) => r.id);
  assert.deepEqual(sent, ['r1', 'r4', 'r2', 'r3'], 'the COMPLETE list, in the new order');
});

test('moveRule writes the full reordered list through the plan spine', async () => {
  const posted = [];
  const policy = reorderPolicy();
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      // /plan echoes the posted draft (the sidecar merges it over the file
      // and returns the merged policy); /apply records its body for the
      // assertions below.
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.policy = policy;
  const note = dom.get('reorderMsg');
  // A harmless move (r2 below r3): no new shadow, straight to the write.
  const ok = await api.moveRule(1, 2);
  assert.equal(ok, true);
  const paths = posted.map((p) => p.url.replace(/^.*sidecar/, ''));
  assert.equal(paths[1], '/plan', 'the staleness read, then the plan — the spine every write rides');
  assert.equal(paths[2], '/apply');
  // §5.2 for lists: the PATCH (what moveRule handed doApply, i.e. what /plan
  // was posted) carries ONLY rules — the whole list, because the server
  // replaces lists wholesale (service.py:422-434).
  const planBody = JSON.parse(posted.find((p) => /\/plan$/.test(p.url)).body);
  assert.deepEqual(Object.keys(planBody.policy).sort(), ['rules']);
  assert.deepEqual(planBody.policy.rules.map((r) => r.id), ['r1', 'r3', 'r2', 'r4']);
  // Every rule WHOLE: a reorder that sent rendered fields would strip what
  // the sheet does not display.
  assert.deepEqual(planBody.policy.rules[0], policy.rules[0]);
  assert.match(note.textContent, /“r2” passa a decidir depois de “r3”/,
    'the confirmation names the flip, not "ordem alterada"');
});

test('a 409 on the reorder write is said, not swallowed', async () => {
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(reorderPolicy())) });
      }
      if (url.endsWith('/plan')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: reorderPolicy(), diff: '+x', base_hash: 'stale' })) });
      }
      return Promise.resolve({ ok: false, status: 409, text: () => Promise.resolve('{}') });
    },
  });
  api.state.policy = reorderPolicy();
  const note = dom.get('reorderMsg');
  const ok = await api.moveRule(1, 2);
  assert.equal(ok, false);
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /mudou por fora/, 'the §4.7 conflict sentence, on the reorder surface too');
  assert.equal(api.state.plan, null, 'the stale plan does not survive to be applied again');
});

test('every rule row carries the grip — always, and a synthetic row never does', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = reorderPolicy();
  api.renderSheet();
  const sheet = dom.get('sheet');
  const rows = sheet.children;
  // The catch-all rows (default, fail-safe) moved to the tail block (§5).
  assert.equal(rows.length, 4, '4 rules in the numbered list');
  assert.equal(dom.get('sheetTailList').children.length, 2, 'default + fail-safe in the tail');
  // Real rule rows: grip present, draggable, with both arrow buttons. The
  // grip is on every rule row now — reorder is an always-on gesture, there
  // is no mode that hides it (card t_f81c24ee).
  for (let i = 0; i < 4; i += 1) {
    const grip = findAll(rows[i], 'step-grip')[0];
    assert.ok(grip, `rule row ${i} has a grip`);
    assert.equal(grip.attrs.draggable, 'true', 'the grip is the drag source');
    assert.equal(grip.tagName, 'button', 'the grip is a button: focusable, named, keyboard-operable');
    const arrows = findAll(grip, 'grip-arrow');
    assert.equal(arrows.length, 2, 'up and down arrows — the no-drag path');
    assert.match(arrows[0].textContent, /↑/);
    assert.match(arrows[1].textContent, /↓/);
  }
  // Synthetic rows (default, fail-safe) are not in `rules`: no grip, no
  // drag — there is no index to move and no list to write.
  const tailRows = dom.get('sheetTailList').children;
  assert.equal(findAll(tailRows[0], 'step-grip').length, 0);
  assert.equal(findAll(tailRows[1], 'step-grip').length, 0);
  // A re-render draws the same always-on shape.
  api.renderSheet();
  assert.equal(findAll(dom.get('sheet'), 'step-grip').length, 4,
    "the grip does not come and go with a mode — it is the sheet's own shape");
  assert.equal(findAll(dom.get('sheetTailList'), 'step-grip').length, 0);
});

test('the keyboard path is the same write: ArrowUp on the grip moves the rule', async () => {
  const posted = [];
  const policy = reorderPolicy();
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      // /plan echoes the posted draft — the real sidecar merges the draft
      // over the file and returns the merged policy, so the apply that
      // follows carries the reordered list, not the file's old order.
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.policy = policy;
  api.renderSheet();
  const rows = dom.get('sheet').children;
  const grip = findAll(rows[2], 'step-grip')[0]; // r3
  const keys = grip._listeners.keydown;
  assert.ok(typeof keys === 'function', 'the grip listens for keys');
  const event = { key: 'ArrowUp', preventDefault() { this.prevented = true; } };
  keys(event);
  assert.equal(event.prevented, true, 'the arrow never scrolls the page while it moves a rule');
  await new Promise((resolve) => setImmediate(resolve));
  const apply = posted.find((p) => /\/apply$/.test(p.url));
  assert.ok(apply, 'the keyboard step wrote');
  const sent = JSON.parse(apply.body).policy.rules.map((r) => r.id);
  assert.deepEqual(sent, ['r1', 'r3', 'r2', 'r4'], 'r3 moved one row up — the same list a drag would send');
});

test('the touch path is the same write: the ↑ button on a coarse-pointer sheet', async () => {
  const posted = [];
  const policy = reorderPolicy();
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      // /plan echoes the posted draft — the real sidecar merges the draft
      // over the file and returns the merged policy, so the apply that
      // follows carries the reordered list, not the file's old order.
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.policy = policy;
  api.renderSheet();
  const rows = dom.get('sheet').children;
  const grip = findAll(rows[3], 'step-grip')[0]; // r4
  const arrows = findAll(grip, 'grip-arrow');
  arrows[0]._listeners.click({ stopPropagation() {} }); // ↑
  await new Promise((resolve) => setImmediate(resolve));
  const apply = posted.find((p) => /\/apply$/.test(p.url));
  assert.ok(apply, 'the arrow button wrote');
  const sent = JSON.parse(apply.body).policy.rules.map((r) => r.id);
  assert.deepEqual(sent, ['r1', 'r2', 'r4', 'r3'], 'r4 moved one row up via the button');
});

test('the drop lands before the target row above the midline and after it below', () => {
  const { api, dom } = loadConsole();
  api.state.policy = reorderPolicy();
  api.renderSheet();
  const rows = dom.get('sheet').children;
  // The stub node's box: width 900, height 300, top 0 (fakeDom) — midline 150.
  assert.equal(api.dropTargetIndex(2, rows[2], { clientY: 100 }), 2,
    'above the midline: the dragged rule lands BEFORE this row');
  assert.equal(api.dropTargetIndex(2, rows[2], { clientY: 200 }), 3,
    'below the midline: it lands AFTER');
  assert.equal(api.dropTargetIndex(2, rows[2], { clientY: 150 }), 2,
    'exactly ON the midline is above — the boundary belongs to the safer half');
  // No position (a stub or a synthetic drop): the row itself, never null —
  // a null would silently swallow the operator's drop.
  assert.equal(api.dropTargetIndex(2, rows[2], {}), 2);
  assert.equal(api.dropTargetIndex('x', rows[2], {}), null, 'a non-index is refused');
});

test('a drag between two editable rows writes the move', async () => {
  const posted = [];
  const policy = reorderPolicy();
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      // /plan echoes the posted draft — the real sidecar merges the draft
      // over the file and returns the merged policy, so the apply that
      // follows carries the reordered list, not the file's old order.
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.policy = policy;
  api.renderSheet();
  const rows = dom.get('sheet').children;
  const source = findAll(rows[0], 'step-grip')[0]; // r1
  const dt = { effectAllowed: null, dropEffect: null, setData() {}, setDragImage() {} };
  source._listeners.dragstart({ dataTransfer: dt });
  assert.equal(api.state.dragRule, 0, 'dragstart records the source row');
  // Over row 3 (r4), in its bottom half: land after it.
  rows[3]._listeners.dragover({ preventDefault() {}, dataTransfer: dt, clientY: 200 });
  assert.ok(rows[3].classList.contains('drop-below'), 'the insertion point is marked where the pointer is');
  rows[3]._listeners.drop({ preventDefault() {}, dataTransfer: dt, clientY: 200 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(api.state.dragRule, null, 'drop clears the drag state');
  const apply = posted.find((p) => /\/apply$/.test(p.url));
  assert.ok(apply, 'the drop wrote');
  const sent = JSON.parse(apply.body).policy.rules.map((r) => r.id);
  assert.deepEqual(sent, ['r2', 'r3', 'r4', 'r1'], 'r1 landed after r4');
});

// ── Card t_7c5d6f91: the Modelos tab's three read-only blocks ─────────────
// The contract is comp-modelos.html plus the LEIA-ME axis correction (spec
// t_c90c5336): the 24-cell strip is PER MODEL, grouped visually by provider —
// never aggregated by provider, because two models of one provider may declare
// different windows and the aggregation would hide the divergence.

// A registry table with TWO zai models sharing the 06-10 Mon-Fri peak, one
// deepseek model with an UNGATED 01-04 peak, and one flat openai-codex model.
// The zai windows are real registry shapes (capabilities.py carries exactly these
// hours/multipliers for that family), so the strip prices the same declarations the
// running path prices. The deepseek entry is deliberately SYNTHETIC: the real
// registry gates both deepseek windows Mon-Fri, and an ungated window is the other
// branch of `weekdaySet` — absent means every day — which nothing else here covers.
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
    'this fixture entry declares no weekdays, so all seven days peak - the real\n'
    + '     registry gates deepseek Mon-Fri; the ungated branch is what is under test');
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

test('the peak-policy selector is LIVE: a change arms the group proposal with the consequence', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  api.state.loading = false;
  api.state.policy = {
    rules: [], default: {},
    tiers: { T1: { model: 'glm-4.7', provider: 'zai', fallback: [], fallback_strategy: 'sequential' } },
  };
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', price_windows: [{ hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2.0 }] },
  };
  api.state.routes = [];
  api.renderLadder();
  const selects = findAll(dom.get('ladder'), 'peak-policy');
  assert.equal(selects.length, 1, 'one selector per group card');
  const sel = selects[0];
  assert.ok(!sel.disabled, 'the selector writes — the read-only delivery is gone');
  const opts = (sel.children || []).filter((c) => c.tagName === 'option');
  assert.deepEqual(opts.map((o) => o.textContent), ['manter a ordem', 'evitar o pico', 'usar o mais barato'],
    'the three contract states, in contract order');
  // "evitar o pico" is refused when nothing varies with the hour: the option
  // is disabled and the reason is said beside the selector.
  api.state.capabilities = {};
  api.renderLadder();
  const sel2 = findAll(dom.get('ladder'), 'peak-policy')[0];
  const avoid = (sel2.children || []).find((o) => o.textContent === 'evitar o pico');
  assert.equal(avoid.disabled, true, 'no peak to avoid → the state is not offered');
  assert.match(flat(dom.get('ladder')), /não há pico para evitar/);
});

test('the peak-policy consequence names what changes, and Gravar writes the tier patch', async () => {
  const posted = [];
  const policy = {
    rules: [], default: {},
    tiers: { T1: { model: 'glm-4.7', provider: 'zai', fallback: [], fallback_strategy: 'sequential' } },
  };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.loading = false;
  api.state.policy = policy;
  api.state.capabilities = {
    'glm-4.7': { provider: 'zai', price_windows: [{ hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2.0 }] },
  };
  api.state.routes = [];
  api.renderLadder();
  const sel = findAll(dom.get('ladder'), 'peak-policy')[0];
  sel.value = 'evitar o pico';
  sel._listeners.change();
  const proposal = findAll(dom.get('ladder'), 'proposal-row')[0];
  assert.equal(proposal.hidden, false);
  assert.match(flat(proposal), /as tentativas em pico \(zai\) passam para o fim da fila/,
    'the consequence names the demotion, not "política alterada"');
  await findAll(proposal, 'btn')[0]._listeners.click();
  const planBody = JSON.parse(posted.find((p) => /\/plan$/.test(p.url)).body);
  assert.deepEqual(planBody.policy, {
    tiers: { T1: { fallback_strategy: 'sequential', time_policy: { avoid_peak: ['zai'] } } },
  });
  const apply = posted.find((p) => /\/apply$/.test(p.url));
  assert.ok(apply, 'the confirmed change reaches /apply');
  assert.deepEqual(JSON.parse(apply.body).policy, {
    tiers: { T1: { fallback_strategy: 'sequential', time_policy: { avoid_peak: ['zai'] } } },
  });
});

test('a 409 on the peak-policy write says the §4.7 conflict sentence', async () => {
  const policy = { rules: [], default: {}, tiers: { T1: { model: 'glm-4.7', provider: 'zai', fallback: [] } } };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      if (url.endsWith('/plan')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy, diff: '+x', base_hash: 'stale' })) });
      }
      return Promise.resolve({ ok: false, status: 409, text: () => Promise.resolve('{}') });
    },
  });
  api.state.loading = false;
  api.state.policy = policy;
  api.state.capabilities = {};
  api.state.routes = [];
  api.renderLadder();
  const sel = findAll(dom.get('ladder'), 'peak-policy')[0];
  sel.value = 'usar o mais barato';
  sel._listeners.change();
  const proposal = findAll(dom.get('ladder'), 'proposal-row')[0];
  await findAll(proposal, 'btn')[0]._listeners.click();
  assert.match(flat(proposal), /mudou por fora/, 'the §4.7 conflict sentence, on the group surface too');
  assert.equal(api.state.plan, null, 'the stale plan does not survive to be applied again');
});

// ── the compaction block: state read off the real /compaction shape ───────

test('a sidecar with no /compaction route degrades to the stated absence, never to NaN', async () => {
  // A 404 HAS A BODY. one_sidecar answers `_error(404, "unknown route")`, so the
  // response carries `{"error":"unknown route"}`, and call() returns
  // `{missing: true, data}` — where `missing` is NOT `.error`. The read boundary
  // tested only `.error`, so the ERROR ENVELOPE was stored as the payload:
  //
  //     state.compaction = compaction.error ? state.compaction : (compaction.data || null)
  //
  // renderCompaction's own guard is `!data || typeof data !== 'object'`, which an
  // error envelope passes happily, so the panel drew itself over a payload with no
  // numbers in it. Every other clause degraded honestly — "A leitura veio sem limiar
  // nem janela" — and this one printed "Agressividade NaN de 100".
  //
  // The panel needs NO new words: its absent sentence already exists and is exactly
  // right. And the console already had the correct shape one read over, in
  // capabilityRegistry: `if (res.missing || res.error) return null` (console.html
  // ~3548). /compaction was the read that did not follow it.
  const { api, dom } = loadConsole({
    fetch: (url) => {
      const path = String(url).split('?')[0];
      if (path.endsWith('/compaction')) {
        return Promise.resolve({
          ok: false, status: 404,
          text: () => Promise.resolve(JSON.stringify({ error: 'unknown route' })),
        });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') });
    },
  });
  await api.fetchAll();
  assert.equal(api.state.compaction, null,
    'a route the sidecar does not have carries no payload — the 404 body is not data');
  api.renderCompaction();
  const drawn = flat(dom.get('compactionBox'));
  assert.doesNotMatch(drawn, /NaN/, 'and nothing renders NaN to the operator');
  assert.match(drawn, /Sem leitura de \/compaction/,
    'the block says the absence it already has words for');
});

test('a compaction payload that names no aggressiveness says so instead of printing NaN', () => {
  // Defence at the formatter as well as the boundary. Today's sidecar always emits
  // the field (it is `int(...)`), so this is not a shape the server produces — but
  // `String(Number(x))` is the one expression in this block that DEFEATS el()'s own
  // undefined/null skip, and the block's neighbours (fmt, fmtPt) all wrap the same
  // call in Number.isFinite and fall through to say(). This makes the dial agree
  // with them.
  const { api, dom } = loadConsole();
  const payload = compactionPayload();
  delete payload.aggressiveness;
  api.state.compaction = payload;
  api.renderCompaction();
  const drawn = flat(dom.get('compactionBox'));
  assert.doesNotMatch(drawn, /NaN/, 'no NaN');
  assert.doesNotMatch(drawn, /Agressividade\s+de 100/,
    'and not a half-sentence with a hole where the number was');
  assert.match(drawn, /Agressividade não informada/,
    'the dial states that it was not reported, in the block\'s own voice');
  // The clauses that WERE served still read normally.
  assert.match(drawn, /Compacta quando a conversa passa de/);
});

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

test('the compaction model and queue controls write: a change arms the proposal, Gravar sends the patch', async () => {
  const posted = [];
  const policy = {
    rules: [], default: {},
    tiers: { T1: { model: 'glm-4.7', provider: 'zai', fallback: [] } },
    compaction: null,
  };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.loading = false;
  api.state.policy = policy;
  api.state.compaction = compactionPayload();
  api.state.capabilities = stripRegistry();
  api.renderCompaction();
  const ctl = findAll(dom.get('compactionBox'), 'ctl');
  const modelCtl = ctl[0];
  assert.ok(!modelCtl.disabled, 'the model choice writes now');
  modelCtl.value = 'glm-5.3';
  modelCtl._listeners.change();
  const proposal = findAll(dom.get('compactionBox'), 'proposal-row')[0];
  assert.equal(proposal.hidden, false);
  assert.match(flat(proposal), /a compactação passa a usar glm-5\.3 \(zai\)/,
    'the consequence names model AND provider');
  await findAll(proposal, 'btn')[0]._listeners.click();
  const planBody = JSON.parse(posted.find((p) => /\/plan$/.test(p.url)).body);
  assert.deepEqual(planBody.policy, { compaction: { provider: 'zai', model: 'glm-5.3' } });
});

test('the compaction queue control writes a GROUP reference, dropping the old own chain', async () => {
  const posted = [];
  const policy = {
    rules: [], default: {},
    tiers: { T1: { model: 'glm-4.7', provider: 'zai', fallback: [] } },
    compaction: null,
  };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.loading = false;
  api.state.policy = policy;
  api.state.compaction = compactionPayload(); // resolved aux carries an own chain
  api.state.capabilities = stripRegistry();
  api.renderCompaction();
  const ctl = findAll(dom.get('compactionBox'), 'ctl');
  const queueCtl = ctl[1];
  queueCtl.value = 'tier:T1';
  queueCtl._listeners.change();
  const proposal = findAll(dom.get('compactionBox'), 'proposal-row')[0];
  assert.match(flat(proposal), /a reserva passa a ser a fila do grupo T1/);
  await findAll(proposal, 'btn')[0]._listeners.click();
  const planBody = JSON.parse(posted.find((p) => /\/plan$/.test(p.url)).body);
  assert.deepEqual(planBody.policy, {
    compaction: {
      provider: 'zai', model: 'glm-4.5-flash', fallback_mode: 'tier:T1', fallback_chain: null,
    },
  },
  'the group reference replaces the own chain — and carries the model the block is about (the motor refuses a block without provider/model)');
});

test('the own-chain picker with NO model keeps the write closed and says what is missing', async () => {
  const posted = [];
  const policy = {
    rules: [], default: {},
    tiers: {
      T1: { model: 'glm-4.7', provider: 'zai', fallback: [] },
      T2: { model: 'deepseek-v4-pro', provider: 'deepseek', fallback: [] },
    },
    compaction: null,
  };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.loading = false;
  api.state.policy = policy;
  // No declared block AND no resolved model: the motor's lint refuses a block
  // without provider+model (service.py _validate_compaction), so a queue-only
  // write can never land here — the sentence says what is missing instead.
  api.state.compaction = Object.assign({}, compactionPayload(), { compaction: null, compaction_errors: [] });
  api.state.capabilities = stripRegistry();
  api.renderCompaction();
  const ctl = findAll(dom.get('compactionBox'), 'ctl');
  const queueCtl = ctl[1];
  queueCtl.value = 'standalone';
  queueCtl._listeners.change();
  const proposal = findAll(dom.get('compactionBox'), 'proposal-row')[0];
  assert.equal(proposal.hidden, false);
  assert.match(flat(proposal), /escolha primeiro o modelo de compactação/,
    'without a model the sentence names the missing model, not a queue jargon');
  assert.equal(findAll(proposal, 'btn')[0].disabled, true, 'and the write stays closed');
  // The chips come from the policy chains: glm-4.7 and deepseek-v4-pro.
  let chips = findAll(dom.get('compactionBox'), 'chip');
  assert.deepEqual(chips.map((c) => c.textContent), ['glm-4.7', 'deepseek-v4-pro']);
  chips[0]._listeners.click();
  chips = findAll(dom.get('compactionBox'), 'chip');
  chips[1]._listeners.click();
  assert.match(flat(proposal), /a reserva já escolhida é glm-4\.7 → deepseek-v4-pro/,
    'the picks are remembered in the sentence, in click order');
  assert.equal(findAll(proposal, 'btn')[0].disabled, true,
    'still closed — the motor refuses a block without provider/model');
  assert.equal(posted.some((p) => /\/plan$/.test(p.url)), false,
    'no plan ever travels for a queue without a model — the write cannot land and is not pretended');
});

test('the own-chain picker WITH a model writes the block carrying model and provider', async () => {
  const posted = [];
  const policy = {
    rules: [], default: {},
    tiers: {
      T1: { model: 'glm-4.7', provider: 'zai', fallback: [] },
      T2: { model: 'deepseek-v4-pro', provider: 'deepseek', fallback: [] },
    },
    compaction: null,
  };
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(policy)) });
      }
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.loading = false;
  api.state.policy = policy;
  // The resolved aux carries the model (glm-4.5-flash @ zai): the queue write
  // may land, and must carry the block's model/provider with it.
  api.state.compaction = compactionPayload();
  api.state.capabilities = stripRegistry();
  api.renderCompaction();
  const ctl = findAll(dom.get('compactionBox'), 'ctl');
  const queueCtl = ctl[1];
  queueCtl.value = 'standalone';
  queueCtl._listeners.change();
  const proposal = findAll(dom.get('compactionBox'), 'proposal-row')[0];
  assert.equal(proposal.hidden, false);
  // The chain pre-fills from the resolved aux's own chain (currentStandaloneChain).
  assert.match(flat(proposal), /glm-4\.7 → gpt-5\.6-luna → mimo-v2\.5/,
    'the consequence names the reserved chain in order');
  assert.equal(findAll(proposal, 'btn')[0].disabled, false);
  await findAll(proposal, 'btn')[0]._listeners.click();
  const planBody = JSON.parse(posted.find((p) => /\/plan$/.test(p.url)).body);
  assert.deepEqual(planBody.policy, {
    compaction: {
      provider: 'zai', model: 'glm-4.5-flash', fallback_mode: 'standalone',
      fallback_chain: [
        { model: 'glm-4.7', provider: 'zai' },
        { model: 'gpt-5.6-luna', provider: 'openai-codex' },
        { model: 'mimo-v2.5', provider: 'xiaomi' },
      ],
    },
  }, 'the block travels with provider+model — the shape the motor lint accepts');
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

// ── the restart banner (card t_06d5abf9) ──────────────────────────────────
// The panel fetches /console on open and swaps the srcdoc only on a byte
// change, so the panel left open for hours keeps the pre-deploy document.
// This document is the only one that can notice: a deploy restarts the
// sidecar, so a later /status read carries a process_started_at DIFFERENT
// from the one this document first read. The banner below says that fact and
// names the gesture that resolves it — close and reopen the panel.

test('a process_started_at change between two reads warns and names the gesture', async () => {
  // The VM reads `fetch` from its own sandbox, so the probe stub rides in
  // through loadConsole — patching the test file's globalThis reaches nothing.
  const fetches = [];
  const fetchStub = (url, opts) => {
    fetches.push({ url, opts });
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ process_started_at: '2026-08-27T16:40:00.000Z' }) });
  };
  const { api, dom } = loadConsole({ fetch: fetchStub });
  // First read of this document's life: the birthmark lands.
  api.state.status = { process_started_at: '2026-08-27T10:00:00.000Z' };
  api.noteProcStart();
  assert.equal(api.state.sessionProcStart, '2026-08-27T10:00:00.000Z');

  // The quiet cycle reads a RESTARTED sidecar: same shape, new process.
  await api.watchStatus();
  assert.equal(fetches.length, 1);
  assert.match(fetches[0].url, /\/status$/);
  assert.equal(api.state.status.process_started_at, '2026-08-27T16:40:00.000Z');
  assert.equal(api.state.sessionProcStart, '2026-08-27T10:00:00.000Z',
    'a restart mid-session never re-marks the birthmark');

  // The banner says the fact and names the gesture — pinned words.
  assert.equal(dom.get('restartBanner').hidden, false);
  const banner = flat(dom.get('restartBanner'));
  assert.match(banner, /o roteador reiniciou depois que esta tela abriu/);
  assert.match(banner, /feche e reabra o painel para buscar a tela atual/);
});

test('an unchanged process_started_at renders nothing for nothing', () => {
  const { api, dom } = loadConsole();
  api.state.status = { process_started_at: '2026-08-27T10:00:00.000Z' };
  api.noteProcStart();
  api.renderRestartBanner();
  assert.equal(dom.get('restartBanner').hidden, true, 'no change → no banner');
  assert.equal(flat(dom.get('restartBanner')), '',
    'render nothing for nothing: no text, not empty-but-present text');
});

test('the restart banner does not repeat the stale banner’s fact', () => {
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.unreachable = false;
  // For BOTH banners to fire at once, the ages must nest: the process that
  // served this document's first read (T-2h) is older than the document's
  // birthmark session, the RESTARTED process (T-1h) is newer than that
  // birthmark, and the code on disk (T-30m) is newer than the restarted
  // process. That is exactly a deploy mid-session: new code, new process,
  // old document — all three ages true at once.
  api.state.status = {
    process_started_at: new Date(T - 2 * 3600 * 1000).toISOString(),
    code_mtime: new Date(T - 30 * 60 * 1000).toISOString(),
    config_mtime: new Date(T - 5 * 60 * 1000).toISOString(),
  };
  api.noteProcStart();
  api.state.status = { ...api.state.status, process_started_at: new Date(T - 1 * 3600 * 1000).toISOString() };
  api.renderRail();

  // Both banners fire together — the facts differ, and each keeps its own words.
  assert.equal(dom.get('staleBanner').hidden, false);
  assert.equal(dom.get('restartBanner').hidden, false);
  const restart = flat(dom.get('restartBanner'));
  const stale = flat(dom.get('staleBanner'));
  // The restart banner does NOT carry the stale banner's command...
  assert.doesNotMatch(restart, /systemctl/);
  // ...nor its fact, in either direction.
  assert.doesNotMatch(restart, /rodando código de/);
  assert.doesNotMatch(stale, /reiniciou depois que esta tela abriu/);
});

test('a document that never read /status stays silent about restarts', () => {
  const { api, dom } = loadConsole();
  api.renderRestartBanner();
  assert.equal(dom.get('restartBanner').hidden, true,
    'no birthmark, no comparison, no banner');
});

test('the quiet cycle dies quietly — a failed probe leaves the header words alone', async () => {
  const { api, dom } = loadConsole({ fetch: () => Promise.reject(new Error('sidecar down')) });
  api.state.status = { process_started_at: '2026-08-27T10:00:00.000Z' };
  api.noteProcStart();
  api.state.unreachable = false;
  api.state.readFailures = {};
  // The markup ships the banner hidden; the stub creates nodes unhidden, so
  // the test seeds the shipped state before probing.
  dom.get('restartBanner').hidden = true;
  await api.watchStatus();
  assert.equal(api.state.unreachable, false, 'a quiet probe never flips the dead-sidecar words');
  assert.equal(Object.keys(api.state.readFailures).length, 0);
  assert.equal(dom.get('restartBanner').hidden, true, 'and it never invents a restart either');
});

test('the refresh path marks the birthmark too, and re-renders the pair', () => {
  const { api, dom } = loadConsole();
  const T = Date.UTC(2026, 7, 19, 12, 0, 0);
  api.state.clock = new Date(T);
  api.state.unreachable = false;
  api.state.status = {
    process_started_at: new Date(T - 2 * 3600 * 1000).toISOString(),
    code_mtime: new Date(T - 3 * 3600 * 1000).toISOString(),
    config_mtime: new Date(T - 5 * 60 * 1000).toISOString(),
  };
  // The full read path (fetchAll → noteProcStart) lands the birthmark; the
  // operator then refreshes into a restarted sidecar.
  api.noteProcStart();
  api.renderRail();
  assert.equal(dom.get('restartBanner').hidden, true, 'same process → still nothing');
  api.state.status = { ...api.state.status, process_started_at: new Date(T - 30 * 60 * 1000).toISOString() };
  api.renderRail();
  assert.equal(dom.get('restartBanner').hidden, false);
  assert.match(flat(dom.get('restartBanner')), /reiniciou depois que esta tela abriu/);
});

// ── the Modelos controls (card t_5d8491bf): the write surface ─────────────
// Three surfaces, one spine: a click arms the block's ONE proposal — the
// consequence sentence + a Gravar button — and only that button writes through
// the /plan + /apply spine (doApply), which plans first, shows the diff and
// answers 409 with the §4.7 conflict sentence. No edit mode: the click IS the
// intention and the sentence IS the preview (CA5).

test('a click on a base hour arms the price proposal with the consequence; Gravar writes the model overlay', async () => {
  const posted = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ rules: [], default: {}, tiers: {} })) });
      }
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  api.state.clock = PEAK; // Monday 07:14 UTC
  api.renderPriceStrip();
  const bands = findAll(dom.get('priceStrip'), 'price-band');
  // glm-4.7 (zai, [6,10) seg-sex): hour 12 is base on every day -> declares 0.8x.
  const cells = findAll(bands[0], 'h-cell');
  assert.equal(cells[0].tagName, 'button', 'an editable cell is a button — a click can write');
  cells[12]._listeners.click();
  const proposal = findAll(dom.get('priceStrip'), 'proposal-row')[0];
  assert.equal(proposal.hidden, false, 'the click armed the confirm row');
  assert.match(flat(proposal), /das 12h às 13h este modelo passa a custar 0,8×/,
    'the consequence names the price, never "janela adicionada"');
  await findAll(proposal, 'btn')[0]._listeners.click();
  const planBody = JSON.parse(posted.find((p) => /\/plan$/.test(p.url)).body);
  assert.deepEqual(Object.keys(planBody.policy), ['price_windows'], 'only the overlay table travels');
  assert.deepEqual(Object.keys(planBody.policy.price_windows), ['glm-4.7'], 'only THIS model — never the provider\'s others');
  assert.deepEqual(planBody.policy.price_windows['glm-4.7'], [
    { hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2.0 },
    { hours_utc: [12, 13], multiplier: 0.8 },
  ]);
  const apply = posted.find((p) => /\/apply$/.test(p.url));
  assert.ok(apply, 'the confirmed click reaches /apply');
});

test('a click inside an inherited peak proposes the return to base', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  api.state.clock = PEAK; // Monday 07:14 UTC: hour 7 is inside zai's seg-sex peak
  api.renderPriceStrip();
  const cells = findAll(findAll(dom.get('priceStrip'), 'price-band')[0], 'h-cell');
  cells[7]._listeners.click();
  const proposal = findAll(dom.get('priceStrip'), 'proposal-row')[0];
  assert.match(flat(proposal), /das 07h às 08h este modelo volta ao preço base/,
    'the removal names the return to base');
});

test('voltar ao catálogo proposes removing the overlay entry, written as a null', async () => {
  const posted = [];
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      posted.push({ url, body: opts && opts.body });
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ rules: [], default: {}, tiers: {} })) });
      }
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: body.policy, diff: '+x', base_hash: 'h' })) });
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  const reg = stripRegistry();
  reg['glm-4.7'].price_windows_origin = 'overlay';
  api.state.capabilities = reg;
  api.state.clock = PEAK;
  api.renderPriceStrip();
  const back = findAll(dom.get('priceStrip'), 'linkish')[0];
  back._listeners.click();
  const proposal = findAll(dom.get('priceStrip'), 'proposal-row')[0];
  assert.match(flat(proposal), /volta a seguir as janelas do catálogo/);
  await findAll(proposal, 'btn')[0]._listeners.click();
  const planBody = JSON.parse(posted.find((p) => /\/plan$/.test(p.url)).body);
  assert.deepEqual(planBody.policy, { price_windows: { 'glm-4.7': null } },
    'the restore is the merge\'s delete-key spelling — the overlay entry leaves the file');
});

test('a 409 on the price write says the §4.7 conflict sentence', async () => {
  const { api, dom } = loadConsole({
    csrfToken: 'tok',
    fetch: (url) => {
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ rules: [], default: {}, tiers: {} })) });
      }
      if (url.endsWith('/plan')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ valid: true, policy: { rules: [], default: {}, tiers: {} }, diff: '+x', base_hash: 'stale' })) });
      }
      return Promise.resolve({ ok: false, status: 409, text: () => Promise.resolve('{}') });
    },
  });
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  api.state.capabilities = stripRegistry();
  api.state.clock = PEAK;
  api.renderPriceStrip();
  const cells = findAll(findAll(dom.get('priceStrip'), 'price-band')[0], 'h-cell');
  cells[12]._listeners.click();
  const proposal = findAll(dom.get('priceStrip'), 'proposal-row')[0];
  await findAll(proposal, 'btn')[0]._listeners.click();
  assert.match(flat(proposal), /mudou por fora/, 'the §4.7 conflict sentence, on the price surface too');
  assert.equal(api.state.plan, null, 'the stale plan does not survive to be applied again');
});

test('the strip shows where each window came from and the human-confirmed date', () => {
  const { api, dom } = loadConsole();
  api.state.loading = false;
  api.state.policy = { rules: [], default: {}, tiers: {} };
  const reg = stripRegistry();
  reg['glm-4.7'].price_windows_origin = 'overlay';
  reg['glm-5.3'].price_windows_origin = 'declared';
  reg['glm-5.3'].price_windows_verified = '2026-08-26';
  reg['deepseek-v4-pro'].price_windows_origin = 'registry';
  api.state.capabilities = reg;
  api.state.clock = PEAK;
  api.renderPriceStrip();
  const text = flat(dom.get('priceStrip'));
  assert.match(text, /janela declarada por você/, 'the overlay says who declared it');
  assert.match(text, /janela declarada numa tentativa do grupo/, 'a per-elo override is not presented as the operator\'s overlay');
  assert.match(text, /conferido por uma pessoa em 26\/08\/2026/, 'the human-confirmed date is served and shown');
  assert.match(text, /janela do catálogo/, 'and the registry windows say their origin too');
  // A model whose windows are declared on a group's elo renders INERT cells:
  // the owner is the group editor, and the strip never fakes a write.
  const bands = findAll(dom.get('priceStrip'), 'price-band');
  const declared = findAll(bands[1], 'h-cell');
  assert.equal(declared[0].tagName, 'i', 'declared-window cells are not buttons');
  assert.equal(declared[0]._listeners.click, undefined, 'and they carry no click');
  // The overlay model gets the restore gesture; the declared one does not.
  const back = findAll(dom.get('priceStrip'), 'linkish');
  assert.equal(back.length, 1, 'only the overlay model offers "voltar ao catálogo"');
});

// ── the pure Modelos write helpers (card t_5d8491bf) ──────────────────────
// The normalized window form the helpers take:
// { hours: [start, end), multiplier, weekdays: null | number[] }.
const M_SEGSEX = { hours: [6, 10], multiplier: 2.0, weekdays: [0, 1, 2, 3, 4] };

test('togglePriceHour: a base hour declares a 0.8x cheap window for that model only', () => {
  const { api } = loadConsole();
  const next = api.togglePriceHour([], 16, 0);
  assert.equal(next.length, 1);
  assert.deepEqual(plain(next[0].hours), [16, 17]);
  assert.equal(next[0].multiplier, 0.8);
  assert.equal(next[0].weekdays, null, 'an all-days declaration carries no weekdays key');
});

test('togglePriceHour: a click inside an inherited window splits it out (remove)', () => {
  const { api } = loadConsole();
  const next = api.togglePriceHour([M_SEGSEX], 7, 0);
  assert.equal(next.length, 2);
  assert.deepEqual(plain(next[0].hours), [6, 7]);
  assert.deepEqual(plain(next[1].hours), [8, 10]);
  assert.deepEqual(plain(next[0].weekdays), [0, 1, 2, 3, 4], 'the split keeps the weekday gate');
});

test('togglePriceHour: the FIRST hour of a window removes too — the boundary is not missed', () => {
  const { api } = loadConsole();
  // Clicking hour 6 (the start of [6,10)) must drop it: [7,10) remains.
  const next = api.togglePriceHour([M_SEGSEX], 6, 0);
  assert.equal(next.length, 1);
  assert.deepEqual(plain(next[0].hours), [7, 10]);
  assert.deepEqual(plain(next[0].weekdays), [0, 1, 2, 3, 4]);
});

test('togglePriceHour: a weekend click inside a weekday-gated window declares instead of removing', () => {
  const { api } = loadConsole();
  // Saturday 07:00: the seg-sex window does not cover Saturday, so the click
  // DECLARES a cheap window for exactly the days the hour is base (weekend) —
  // the declared window can never overlap the gated one (the lint's refusal).
  const next = api.togglePriceHour([M_SEGSEX], 7, 5);
  assert.equal(next.length, 2);
  assert.equal(next[1].multiplier, 0.8);
  assert.deepEqual(plain(next[1].hours), [7, 8]);
  assert.deepEqual(plain(next[1].weekdays), [5, 6]);
});

test('togglePriceHour: adjacent same-day windows merge; midnight is two entries', () => {
  const { api } = loadConsole();
  const a = api.togglePriceHour([], 5, 0);
  const b = api.togglePriceHour(a, 6, 0);
  assert.equal(b.length, 1, 'adjacent same-day windows merge');
  assert.deepEqual(plain(b[0].hours), [5, 7]);
  const c = api.togglePriceHour([], 23, 0);
  const d = api.togglePriceHour(c, 0, 0);
  assert.equal(d.length, 2, 'no merge across midnight — [23,24) and [0,1) are two entries');
  assert.deepEqual(plain(d[0].hours), [0, 1]);
  assert.deepEqual(plain(d[1].hours), [23, 24]);
});

test('weekdayWords names the day sets in pt-BR', () => {
  const { api } = loadConsole();
  assert.equal(api.weekdayWords(null), 'todos os dias');
  assert.equal(api.weekdayWords([0, 1, 2, 3, 4]), 'de segunda a sexta');
  assert.equal(api.weekdayWords([5, 6]), 'nos fins de semana');
  assert.equal(api.weekdayWords([5]), 'aos sábados');
  assert.equal(api.weekdayWords([0, 2, 4]), 'às segundas, quartas e sextas');
});

test('priceOriginWords and priceVerifiedWords never invent the field', () => {
  const { api } = loadConsole();
  assert.equal(api.priceOriginWords('registry'), 'janela do catálogo');
  assert.equal(api.priceOriginWords('overlay'), 'janela declarada por você');
  assert.equal(api.priceOriginWords('declared'), 'janela declarada numa tentativa do grupo');
  assert.equal(api.priceOriginWords('bogus'), null);
  assert.equal(api.priceVerifiedWords('2026-08-26'), 'conferido por uma pessoa em 26/08/2026');
  assert.equal(api.priceVerifiedWords(null), null, 'the detector\'s read time is not on the wire — never invented');
  assert.equal(api.priceVerifiedWords('not a date'), null);
});

test('priceTogglePatch freezes the effective set into the overlay; the restore is a null', () => {
  const { api } = loadConsole();
  const patch = api.priceTogglePatch('glm-5.3', [M_SEGSEX], 16, 0);
  assert.deepEqual(plain(patch), {
    price_windows: {
      'glm-5.3': [
        { hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2.0 },
        { hours_utc: [16, 17], multiplier: 0.8 },
      ],
    },
  });
  assert.deepEqual(plain(api.priceRestorePatch('glm-5.3')), { price_windows: { 'glm-5.3': null } });
});

test('tierAvoidProviders lists only chain providers whose models vary with the hour', () => {
  const { api } = loadConsole();
  const state = {
    policy: { tiers: { T1: { model: 'glm-4.7', provider: 'zai', fallback: [] } } },
    capabilities: {
      'glm-4.7': { provider: 'zai', price_windows: [{ hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2.0 }] },
    },
  };
  assert.deepEqual(plain(api.tierAvoidProviders(state, 'T1')), ['zai']);
  state.capabilities['glm-4.7'] = { provider: 'zai' }; // flat model -> nothing to avoid
  assert.deepEqual(plain(api.tierAvoidProviders(state, 'T1')), []);
});

test('peakPolicyPatch writes the three contract states and removes time_policy with null', () => {
  const { api } = loadConsole();
  const state = {
    policy: {
      tiers: {
        T1: { model: 'glm-4.7', provider: 'zai', fallback: [], fallback_strategy: 'sequential' },
      },
    },
    capabilities: {
      'glm-4.7': { provider: 'zai', price_windows: [{ hours_utc: [6, 10], weekdays: [0, 1, 2, 3, 4], multiplier: 2.0 }] },
    },
  };
  assert.deepEqual(plain(api.peakPolicyPatch('T1', state, 'evitar o pico')), {
    tiers: { T1: { fallback_strategy: 'sequential', time_policy: { avoid_peak: ['zai'] } } },
  });
  assert.deepEqual(plain(api.peakPolicyPatch('T1', state, 'usar o mais barato')), {
    tiers: { T1: { fallback_strategy: 'cheapest_now', time_policy: null } },
  });
  assert.deepEqual(plain(api.peakPolicyPatch('T1', state, 'manter a ordem')), {
    tiers: { T1: { fallback_strategy: 'sequential', time_policy: null } },
  });
});

test('the compaction patches: model change, group reference, no queue, own chain', () => {
  const { api } = loadConsole();
  assert.deepEqual(plain(api.compactionModelPatch('glm-4.5-flash', 'zai')),
    { compaction: { provider: 'zai', model: 'glm-4.5-flash' } });
  // The motor's lint REQUIRES provider+model on the raw block even for a
  // queue-only change (service.py _validate_compaction) — the queue patch
  // carries the current model, it is not a standalone knob.
  assert.deepEqual(plain(api.compactionQueuePatch('none', null, null, 'glm-4.5-flash', 'zai')), {
    compaction: { provider: 'zai', model: 'glm-4.5-flash', fallback_mode: null, fallback_chain: null },
  });
  assert.deepEqual(plain(api.compactionQueuePatch('tier', 'T1', null, 'glm-4.5-flash', 'zai')), {
    compaction: { provider: 'zai', model: 'glm-4.5-flash', fallback_mode: 'tier:T1', fallback_chain: null },
  });
  assert.deepEqual(plain(api.compactionQueuePatch('standalone', null,
    [{ model: 'glm-4.7', provider: 'zai' }], 'glm-4.5-flash', 'zai')), {
    compaction: {
      provider: 'zai', model: 'glm-4.5-flash', fallback_mode: 'standalone',
      fallback_chain: [{ model: 'glm-4.7', provider: 'zai' }],
    },
  });
});

test('doApply sends the DRAFT as the /apply changes, so a null removal actually removes', async () => {
  let applyBody = null;
  const { api } = loadConsole({
    csrfToken: 'tok',
    fetch: (url, opts) => {
      if (url.endsWith('/policy')) {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({})) });
      }
      if (url.endsWith('/plan')) {
        const body = JSON.parse(opts.body);
        // Real server: plan returns the MERGED policy (null already absorbed).
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({
          valid: true, policy: { price_windows: {} }, diff: '-x', base_hash: 'h',
        })) });
      }
      if (url.endsWith('/apply')) { applyBody = JSON.parse(opts.body); }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ok: true })) });
    },
  });
  api.state.policy = {};
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg, { price_windows: { 'glm-5.3': null } });
  assert.ok(applyBody, '/apply was reached');
  assert.deepEqual(plain(applyBody.policy), { price_windows: { 'glm-5.3': null } },
    'the removal travels as the CHANGE (null), not as the merged plan.policy — re-merging the merged result would leave the key on disk');
});

// ── the breaker's numbers, on the row they explain (card t_376a14ac) ─────────
// `GET /blocklist` publishes `auto_breaker`: the threshold, the sliding window and
// what each failure kind weighs. Without them a cooldown row's `failure_count: 3`
// is a number the operator has to read the source to interpret — 3 is one
// ttfb_stall (weight 3) away from tripping, or three nonzero_exits (weight 1) that
// would need five. Opposite diagnoses, same digit on screen.

test('a cooldown row says how it opened, against the threshold that opened it', () => {
  const { api } = loadConsole();
  const policy = {
    threshold: 5, window_seconds: 600,
    failure_weights: { ttfb_stall: 3, nonzero_exit: 1 },
  };
  const notes = plain(api.breakerNotes(
    { model_key: 'deepseek-v4-pro', failure_count: 3, last_failure_kind: 'ttfb_stall' },
    policy,
  ));
  assert.deepEqual(notes, ['3 de 5 em 10 min', 'ttfb_stall pesa 3']);
});

test('the failure kind is named ONCE — with its weight, or bare, never twice', () => {
  const { api } = loadConsole();
  // §2, one authority per fact. The kind used to be this row's only note; it now
  // carries the weight beside it, and a second note repeating the kind would be
  // the same fact from two places.
  const withWeight = plain(api.breakerNotes(
    { failure_count: 2, last_failure_kind: 'idle_stall' },
    { threshold: 5, window_seconds: 600, failure_weights: { idle_stall: 2 } },
  ));
  assert.deepEqual(withWeight, ['2 de 5 em 10 min', 'idle_stall pesa 2']);
  assert.equal(withWeight.filter((n) => n.includes('idle_stall')).length, 1);

  // A kind the served table does not price: the kind still shows, bare. Inventing
  // a weight here would be inventing the criterion.
  const unknownKind = plain(api.breakerNotes(
    { failure_count: 2, last_failure_kind: 'kind_nobody_declared' },
    { threshold: 5, window_seconds: 600, failure_weights: { idle_stall: 2 } },
  ));
  assert.deepEqual(unknownKind, ['2 de 5 em 10 min', 'kind_nobody_declared']);
});

test('an older sidecar that serves no policy still renders the row it always did', () => {
  const { api } = loadConsole();
  // The route gained `auto_breaker` on this branch. A console pointed at a sidecar
  // without it must not go blank and must not print "undefined de undefined": the
  // pre-existing note (the bare kind) is what it always showed.
  assert.deepEqual(plain(api.breakerNotes(
    { failure_count: 3, last_failure_kind: 'ttfb_stall' }, {})),
    ['ttfb_stall']);
  assert.deepEqual(plain(api.breakerNotes(
    { failure_count: 3, last_failure_kind: 'ttfb_stall' }, null)),
    ['ttfb_stall']);
  // And with neither policy nor kind there is nothing to say — no line, per
  // DESIGN.md rule 1, rather than an empty phrase.
  assert.deepEqual(plain(api.breakerNotes({}, {})), []);
  assert.deepEqual(plain(api.breakerNotes(null, null)), []);
});

test('the score needs all three numbers, or it is not said at all', () => {
  const { api } = loadConsole();
  // "3 de 5" with no window is a rate with no period, and a period the route did
  // not serve must not be guessed. Each of the three missing in turn.
  const base = { failure_count: 3, last_failure_kind: 'crash' };
  const full = { threshold: 5, window_seconds: 600, failure_weights: { crash: 1 } };
  assert.match(plain(api.breakerNotes(base, full))[0], /^3 de 5 em/);
  for (const missing of ['threshold', 'window_seconds']) {
    const partial = Object.assign({}, full);
    delete partial[missing];
    const notes = plain(api.breakerNotes(base, partial));
    assert.equal(notes.length, 1, `sem ${missing} a frase do placar não sai`);
    assert.doesNotMatch(notes[0], /de 5/);
  }
  // No count on the entry: same refusal, from the other side.
  const noCount = plain(api.breakerNotes({ last_failure_kind: 'crash' }, full));
  assert.deepEqual(noCount, ['crash pesa 1']);
});

test('the window is said in minutes only when it really is whole minutes', () => {
  const { api } = loadConsole();
  assert.equal(api.breakerWindowWords(600), '10 min');
  assert.equal(api.breakerWindowWords(120), '2 min');
  assert.equal(api.breakerWindowWords(60), '1 min');
  // 90s is not "1min30": that is a unit nobody configured, and the seconds are
  // what the operator typed.
  assert.equal(api.breakerWindowWords(90), '90s');
  assert.equal(api.breakerWindowWords(45), '45s');
  // Nothing to say beats a wrong period.
  for (const bad of [0, -60, null, undefined, 'dez', NaN]) {
    assert.equal(api.breakerWindowWords(bad), null, String(bad));
  }
});

test('the numbers reach the cooldown row the sheet really draws', () => {
  const { api, dom } = loadConsole();
  // Through renderBans, not through the pure function: a helper nothing calls is
  // the shape this card is about.
  api.state.blocklist = {
    manual_bans: [],
    fallback_chain: [],
    breaker_cooldowns: [{
      model_key: 'deepseek-v4-pro', cooldown_remaining_s: 300,
      failure_count: 4, last_failure_kind: 'ttfb_stall',
    }],
    auto_breaker: {
      threshold: 5, window_seconds: 600,
      failure_weights: { ttfb_stall: 3 },
    },
  };
  // Driven through renderHealth, which is what calls renderBans in production: a
  // helper reached only by a test is the shape this card is about.
  api.state.policy = { rules: [], tiers: {} };
  api.state.liveness = { models: [] };
  api.renderHealth();
  const text = findAll(dom.get('bans'), 'row-note').map((n) => n.textContent);
  assert.ok(text.includes('4 de 5 em 10 min'), `o placar está na linha: ${text}`);
  assert.ok(text.includes('ttfb_stall pesa 3'), `o peso está na linha: ${text}`);
  // The row still says the time it always said.
  const values = findAll(dom.get('bans'), 'row-value').map((n) => n.textContent);
  assert.ok(values.includes('faltam 300s'), values.join(' | '));
});

test('every phrase these notes say lives in the WRITE map, once', () => {
  // §4.7: the map is the single copy of every phrase the screen says. Two of them
  // are new; a literal built inline here is how one drifts from the other.
  const source = fs.readFileSync(sourcePath, 'utf8');
  for (const phrase of ['{N} de {MAX} em {WIN}', '{KIND} pesa {W}']) {
    const hits = source.split(phrase).length - 1;
    assert.equal(hits, 1, `${phrase} deve aparecer exatamente uma vez, no mapa`);
  }
  // ...and it has to be USED from the map. Counting the literal alone was not
  // enough: a mutation that inlined `${count} de ${max} em ${win}` left the map
  // entry sitting there dead, the literal still appearing exactly once, and this
  // test green. What forbids the second copy is the REFERENCE.
  for (const key of ['WRITE.breakerScore', 'WRITE.breakerWeight']) {
    assert.ok(source.includes(`fill(${key}`),
      `${key} deve ser lido do mapa por fill(), não recopiado inline`);
  }
});

test('no two functions in this file share a name', () => {
  // Found the hard way on this card: a new `windowWords(seconds)` silently
  // overwrote the pricing `windowWords(windows, when)` — same scope, later
  // declaration wins, no error anywhere. Three pricing tests failed and named a
  // price phrase, which points at the wrong file. Nothing here forbade it, so
  // this does.
  const script = fs.readFileSync(sourcePath, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
  const names = [...script.matchAll(/^\s*function ([A-Za-z_$][\w$]*)\s*\(/gm)]
    .map((m) => m[1]);
  const seen = new Map();
  names.forEach((n) => seen.set(n, (seen.get(n) || 0) + 1));
  const dupes = [...seen.entries()].filter(([, n]) => n > 1);
  assert.deepEqual(dupes, [], `nomes declarados duas vezes: ${JSON.stringify(dupes)}`);
  assert.ok(names.length > 50, `o scanner precisa achar as funções, achou ${names.length}`);
});

// ── A VALUE THAT OPENS AN EDITOR IS A CONTROL, SO A KEYBOARD REACHES IT ──────
// The no-mode sheet is the right answer to "the Edit button does nothing", and the
// dotted underline is a real affordance. What the cells did not have was any way in
// without a pointer: a <div> carrying a click handler is not in the tab order, Enter and
// Space do nothing on it, and a screen reader announces it as text. A comp can say where
// a control sits and how it reads; it cannot ask for a control only a mouse can reach.

// The cells take `is-edit` through classList, which the DOM stub keeps apart from
// className — so they are found by the contract under test instead: a node carrying
// role=button inside the sheet.
function editCells(dom) {
  const out = [];
  const walk = (node) => {
    (node.children || []).forEach((kid) => {
      if (kid.attrs && kid.attrs.role === 'button') out.push(kid);
      walk(kid);
    });
  };
  walk(dom.get('sheet'));
  return out;
}

function editableSheet(api) {
  api.state.loading = false;
  api.state.policy = {
    rules: [{ id: 'hard-verbs', when: { verb_class: { eq: 'hard' } }, then: { model: 'T4', profile: 'coder' } }],
    default: { action: 'classify' }, classifier: { model: 'm' },
    fail_safe: { model: 'm' }, tiers: { T4: { model: 'x', provider: 'p' } },
  };
  api.renderSheet();
}

test('an editable cell is announced and reachable as the control it is', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  editableSheet(api);
  const cells = editCells(dom);
  assert.ok(cells.length >= 1, `the sheet draws its editable cells, got ${cells.length}`);
  cells.forEach((cell) => {
    assert.equal(cell.getAttribute('role'), 'button',
      'a div that opens an editor must say it is a button, or it is read as text');
    assert.equal(cell.tabIndex, 0, 'and it must be in the tab order');
    assert.ok(cell.title, 'and carry the title the comp gives it');
    assert.equal(typeof cell._listeners.keydown, 'function', 'and answer a key, not only a click');
  });
});

test('Enter and Space open the editor, and Space does not scroll the panel', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  editableSheet(api);
  const cell = editCells(dom)[0];

  for (const key of ['Enter', ' ']) {
    api.state.selected = null;
    let prevented = false;
    cell._listeners.keydown({ key, preventDefault() { prevented = true; }, stopPropagation() {} });
    assert.equal(api.state.selected, 'rule:hard-verbs', `${key} opens the row`);
    // Space scrolls a scroll container by default, and this one is inside the host's
    // scrolling body: opening a row must not also jump the panel.
    assert.equal(prevented, true, `${key} is consumed rather than left to the page`);
  }

  // Anything else is left alone — a keydown handler that swallowed keys would break
  // tabbing off the cell.
  api.state.selected = null;
  cell._listeners.keydown({ key: 'a', preventDefault() {}, stopPropagation() {} });
  assert.equal(api.state.selected, null, 'an unrelated key does nothing');
});

test('Escape puts the open row away, and it is wired to the document', () => {
  const { api, dom } = loadConsole({ csrfToken: 'tok' });
  editableSheet(api);
  const cell = editCells(dom)[0];
  cell._listeners.click({ stopPropagation() {} });
  assert.equal(api.state.selected, 'rule:hard-verbs', 'a row is open');

  api.closeRow();
  assert.equal(api.state.selected, null, 'Escape closes it');
  assert.equal(api.state.draft, null, 'and drops the draft with it');
  // Idempotent, and asserted by its OBSERVABLE effect rather than by not throwing:
  // without the guard, Escape with nothing open still re-renders the sheet, which
  // rebuilds every row under a reader who pressed a key for nothing. A marker child
  // survives only if the render did not happen. (A mutation proved the
  // `doesNotThrow` form here caught nothing at all.)
  const marker = { id: 'marker', className: 'marker', children: [] };
  dom.get('sheet').children.push(marker);
  api.closeRow();
  assert.ok(dom.get('sheet').children.includes(marker),
    'closing what is already closed must not rebuild the sheet');

  const script = fs.readFileSync(sourcePath, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
  const wired = script.slice(script.indexOf('function wire()'));
  assert.match(wired, /event\.key === 'Escape'/, 'the exit is wired, not only exported');
  assert.match(wired, /closeRow\(\)/);
});

test("the browser's own surfaces wear the skin, not the engine's defaults", () => {
  // Text selection, the caret and the scrollbars ship with colours that belong to no
  // design system: a blue selection inside a gold-on-navy shell, a white caret, a grey
  // platform scrollbar. Each reads the same host token the rest of the file does.
  const { style } = consoleStyle();
  assert.match(style, /::selection \{ background: var\(--accent-bg-strong\); color: var\(--text\); \}/);
  assert.match(style, /caret-color: var\(--accent\)/);
  assert.match(style, /scrollbar-color: var\(--line-strong\) transparent/);
  assert.match(style, /::-webkit-scrollbar-thumb \{[\s\S]*?background-color: var\(--line-strong\)/);
  // No literal colour sneaks in with them: this file has no palette, and a hex here
  // would be the one colour that does not repaint with the skin.
  const surfaces = style.slice(style.indexOf('::selection'), style.indexOf('h1, h2 {'));
  assert.doesNotMatch(surfaces, /#[0-9a-fA-F]{3}/, 'every value is a host token');
});

// ── U19: a save must not orphan the open inspector ────────────────────────────
//
// `renderAll` only rebuilds the inspector `if (!state.selected)`, which is FALSE
// after a row click. The success path did `state.draft = null; await load()` and
// stopped — leaving the open panel bound to a draft that had just been nulled, so
// `surfacePatch` returned null and every later Salvar answered "Não há o que
// salvar." Measured: the second edit from one open panel was silently discarded,
// with zero POSTs. `refuseStale` (the 409 path) already captured the node,
// reloaded, and re-rendered; the success path did not.

test('a save rebuilds the open inspector, re-stamps the line, re-hides the button', () => {
  // A SOURCE SCAN, deliberately, and the reason is worth stating: doApply's
  // success path is reachable only through the inspector's own save, whose draft
  // comes from `state.draft` and whose message and button nodes are created by
  // `renderInspector`. Driving that through the DOM stub means standing up the
  // whole panel and a `load()` that re-fetches six endpoints; and calling
  // `doApply` with an explicit `draft` argument — the easy way — BYPASSES
  // `state.draft`, i.e. the exact field the bug lives in, so such a test passes
  // with the bug present. Measured: it did.
  //
  // This file already scans source for contracts a DOM cannot show (the one
  // wall-clock read, the pt-BR vocabulary, the single-authority write labels), so
  // the scan is the idiom here rather than a shortcut. Verified by reverting the
  // fix: this test fails, and the DOM-level one did not.
  //
  // What it pins, and why each half matters: the naive fix (capture, reload,
  // re-render, stop) loses both the saved LINE and the eight-second hide, because
  // `msg` and `saveBtn` in doApply's scope point at the panel renderInspector just
  // replaced.
  const src = fs.readFileSync(sourcePath, 'utf8')
    .match(/<script>([\s\S]*?)<\/script>/)[1];
  const body = src.slice(src.indexOf('async function doApply'));
  const success = body.slice(0, body.indexOf('} finally {'));
  assert.match(success, /const openNode = state\.selectedNode;/,
    'the node is captured BEFORE the reload');
  assert.match(success, /if \(openNode\) renderInspector\(openNode\);/);
  assert.match(success, /\$\('nodeMsg'\)/,
    'the saved line is re-stamped on the FRESH message node');
  assert.match(success, /freshBtn/,
    'and the hide applies to the FRESH button, resolved by id');
});

// ── U33: an unpriced failure kind reads the SERVED default ────────────────────

test('a failure kind the weight table omits still says what it is worth', () => {
  const { api } = loadConsole();
  // FAILURE_WEIGHTS does not enumerate every kind: `agent_error`, `spawn_error`,
  // `inline_error` and `at_capacity` are all documented first-class kinds that
  // score the default. /blocklist carries `default_weight` beside the table for
  // exactly that reason (breaker.py:270) and this read ignored it, so a reachable
  // kind rendered BARE while the payload said what it was worth.
  const policy = {
    threshold: 5, window_seconds: 600, default_weight: 1,
    failure_weights: { ttfb_stall: 3 },
  };
  const named = api.breakerNotes({ last_failure_kind: 'ttfb_stall' }, policy);
  assert.ok(named.some((n) => /ttfb_stall/.test(n) && /3/.test(n)),
    'a kind the table names keeps its own weight');

  const defaulted = api.breakerNotes({ last_failure_kind: 'agent_error' }, policy);
  assert.ok(defaulted.some((n) => /agent_error/.test(n) && /1/.test(n)),
    'a kind the table omits takes the SERVED default, not a bare label');
});

test('a payload with no weight policy at all still renders the kind bare', () => {
  // Back-compat: an older sidecar serves no policy, and the console must not
  // invent a weight. Falling back to a hardcoded 1 would be a second authority.
  const { api } = loadConsole();
  const bare = api.breakerNotes({ last_failure_kind: 'ttfb_stall' }, {});
  assert.ok(bare.includes('ttfb_stall'), 'the kind is named, with no number');
  assert.ok(!bare.some((n) => /ttfb_stall.*\d/.test(n)));
});
