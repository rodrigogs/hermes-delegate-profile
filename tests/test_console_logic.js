// Behavioural tests for the console's decision logic — the parts a static scan
// cannot check. The console is one self-contained IIFE, so (like
// test_router_nav_*.js) we rewrite its footer to publish the internals, then run
// it in a VM over a DOM stub. Every assertion below pins a rule that would
// silently rot otherwise: which node a replay step lights up, how a health
// rollup degrades, what the rail badge counts, and that Status details never
// echo a number the panel head already shows.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = 'webui_extension/capability-router/console.html';

const EXPORTS = [
  'stepNodeId', 'causeColor', 'worstLivenessClass', 'curatedStatusDetails',
  'pipelineNodes', 'pipelineEdges', 'renderRail', 'state', 'setMode',
].join(', ');

// A DOM stub rich enough for the console's init path: element lookups return
// recording nodes, so setMode()/renderRail() can run without a browser.
function fakeDom() {
  const nodes = new Map();
  const make = (id) => ({
    id,
    className: '',
    textContent: '',
    value: '',
    hidden: false,
    readOnly: false,
    style: {},
    dataset: {},
    attrs: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on === undefined) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); } else if (on) { this._set.add(c); } else { this._set.delete(c); } },
      contains(c) { return this._set.has(c); },
    },
    children: [],
    append(...kids) { this.children.push(...kids); },
    appendChild(k) { this.children.push(k); return k; },
    removeChild(k) { this.children = this.children.filter((x) => x !== k); },
    remove() {},
    addEventListener() {},
    setAttribute(n, v) { this.attrs[n] = v; },
    getAttribute(n) { return this.attrs[n]; },
    toggleAttribute(n, on) { if (on) this.attrs[n] = ''; else delete this.attrs[n]; },
    hasAttribute(n) { return n in this.attrs; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    get firstChild() { return this.children[0] || null; },
    getBoundingClientRect() { return { width: 900, height: 400, left: 0, right: 900 }; },
    clientWidth: 900,
  });
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, make(id)); return nodes.get(id); };
  return {
    nodes,
    document: {
      getElementById: get,
      createElement: (tag) => Object.assign(make(`el:${tag}`), { tagName: tag }),
      createElementNS: (_ns, tag) => Object.assign(make(`svg:${tag}`), { tagName: tag }),
      // Keyed by selector so '#modeButton .mode-button-label' and its siblings
// are distinct nodes, the way they are in the document.
      querySelector: (sel) => get(`sel:${sel}`),
      querySelectorAll: () => [],
      addEventListener() {},
      readyState: 'complete',
    },
  };
}

function loadConsole() {
  const html = fs.readFileSync(sourcePath, 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
    // Publish internals, and drop the init calls that need a live browser.
    .replace(/\n      wireEvents\(\);[\s\S]*?refresh\(\);\n/, '\n')
    .replace(/\n\s*\}\)\(\);\s*$/, `\n  globalThis.__consoleTest = { ${EXPORTS} };\n})();\n`);
  const dom = fakeDom();
  const context = {
    console,
    document: dom.document,
    globalThis: {},
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') }),
    setTimeout() {},
    Math,
    JSON,
    Number,
    Object,
    Array,
    String,
    Set,
    Map,
    Date,
  };
  vm.runInNewContext(script, context, { filename: sourcePath });
  return { api: context.globalThis.__consoleTest, dom };
}

test('a replay step lights the node that actually made the decision', () => {
  const { api } = loadConsole();
  // A rules step names the rule that fired — replay must highlight THAT rule
  // node, not the generic stage, or the trace lies about which row matched.
  assert.equal(api.stepNodeId({ stage: 'rules', out: { rule_id: 'hard-verbs' } }), 'rule:hard-verbs');
  // A rules step with no rule_id fell through to the default row.
  assert.equal(api.stepNodeId({ stage: 'rules', out: {} }), 'default');
  // The veto is recorded as its own stage but belongs to the blocklist node.
  assert.equal(api.stepNodeId({ stage: 'veto' }), 'blocklist');
  // Runtime stages map to themselves.
  assert.equal(api.stepNodeId({ stage: 'cache' }), 'cache');
  assert.equal(api.stepNodeId({ stage: 'session_pin' }), 'session_pin');
  assert.equal(api.stepNodeId({ stage: 'classifier' }), 'classifier');
});

test('health rollup reports the WORST state, never an average or the first', () => {
  const { api } = loadConsole();
  const worst = (...states) => api.worstLivenessClass(states.map((state) => ({ state })));
  assert.equal(worst('alive', 'alive'), 'alive');
  // One bad target must dominate however many healthy ones surround it.
  assert.equal(worst('alive', 'degraded', 'alive'), 'degraded');
  assert.equal(worst('degraded', 'quota_exhausted'), 'quota');
  assert.equal(worst('quota_exhausted', 'dead'), 'dead');
  // Order must not matter — a dead first entry and a dead last entry agree.
  assert.equal(worst('dead', 'alive'), worst('alive', 'dead'));
});

test('cause colours separate a refusal from a normal route', () => {
  const { api } = loadConsole();
  const red = api.causeColor('blocklist_veto');
  assert.equal(api.causeColor('fail_safe_strong'), red, 'both refusals read as the same alarm');
  assert.notEqual(api.causeColor('hard_rule'), red, 'a deterministic rule hit is not an alarm');
  assert.notEqual(api.causeColor('classifier'), api.causeColor('hard_rule'), 'inferred and deterministic causes stay distinguishable');
});

test('Status details never echo a number the panel head already shows', () => {
  const { api } = loadConsole();
  api.state.status = {
    // Canonical elsewhere (metric cards + rail badges) — must NOT be repeated.
    rules_count: 3, valid: true, enabled: true, tiers: ['T1', 'T2'], classifier: { model: 'x' },
    // Only shown here.
    last_event: 'routed', reason: 'ok',
  };
  const curated = api.curatedStatusDetails();
  for (const echoed of ['rules_count', 'valid', 'enabled', 'tiers', 'classifier']) {
    assert.ok(!(echoed in curated), `${echoed} is duplicated into the details list`);
  }
  assert.equal(curated.last_event, 'routed');
  assert.equal(curated.reason, 'ok');
});

test('the graph is built from policy, and rules point at the tier they route to', () => {
  const { api } = loadConsole();
  api.state.policy = {
    rules: [{ id: 'hard-verbs', then: { model: 'T4' } }, { id: 'ask', then: { action: 'classify' } }],
    tiers: { T1: { model: 'cheap' }, T4: { model: 'strong' } },
  };
  const nodes = api.pipelineNodes(1200);
  const ids = nodes.map((n) => n.id);
  assert.ok(ids.includes('rule:hard-verbs') && ids.includes('rule:ask'), 'one node per policy rule');
  assert.ok(ids.includes('tier:T1') && ids.includes('tier:T4'), 'one node per tier');

  const edges = api.pipelineEdges(nodes).map((e) => `${e.from.id}->${e.to.id}`);
  // then.model:T4 must draw the rule straight at that tier…
  assert.ok(edges.includes('rule:hard-verbs->tier:T4'), 'a concrete rule edges to its tier');
  // …and then.action:classify must edge at the classifier instead.
  assert.ok(edges.includes('rule:ask->classifier'), 'a classify rule edges to the classifier');
});

test('the graph spreads into a wider canvas instead of staying compact', () => {
  const { api } = loadConsole();
  api.state.policy = { rules: [{ id: 'r', then: { model: 'T1' } }], tiers: { T1: {} } };
  const spanOf = (width) => {
    const nodes = api.pipelineNodes(width);
    const xs = nodes.map((n) => n.x);
    return Math.max(...xs) - Math.min(...xs);
  };
  // Freeing width (rail collapse / inspector hide) must actually widen the
  // layout — this is the whole point of the space fix.
  assert.ok(spanOf(1400) > spanOf(700), 'a wider canvas produces a wider graph');
});

test('rail badges count live state and hide when a section has nothing', () => {
  const { api, dom } = loadConsole();
  api.state.policy = { rules: [{ id: 'a' }, { id: 'b' }, { id: 'c' }], tiers: { T1: {} } };
  api.state.routes = [{ id: '1' }, { id: '2' }];
  api.state.blocklist = { manual_bans: [{ model: 'bad' }] };
  api.state.liveness = { models: [{ state: 'alive' }, { state: 'degraded' }] };
  api.renderRail();

  assert.equal(dom.nodes.get('railPipelineBadge').textContent, '3', 'pipeline badge counts rules');
  assert.equal(dom.nodes.get('railReplayBadge').textContent, '2', 'replay badge counts recorded routes');
  assert.equal(dom.nodes.get('railBlocklistBadge').textContent, '1', 'blocklist badge counts bans + breakers');
  // A degraded target must surface on the Status dot, not be averaged away.
  assert.match(dom.nodes.get('railStatus').className, /is-degraded/);
  // Compaction has no natural count: its badge stays hidden rather than showing 0.
  assert.equal(dom.nodes.get('railCompactionBadge').hidden, true);
});

test('renderRail survives being called before the first poll', () => {
  const { api, dom } = loadConsole();
  // At init setMode() → renderRail() runs while every state field is still
  // empty. If any source is read unguarded the whole IIFE dies and the console
  // renders blank, so this is the cheapest guard against a white screen.
  assert.doesNotThrow(() => api.renderRail());
  assert.equal(dom.nodes.get('railPipelineBadge').hidden, true, 'no policy yet → no count');
});

test('the mode control states the current mode AND what pressing it will do', () => {
  const { api, dom } = loadConsole();
  const button = dom.nodes.get('modeButton');

  // Read: locked, and the hint tells you the way in. A control that only
  // changed colour would leave "how do I edit?" unanswered — the complaint
  // this redesign exists to fix.
  api.setMode('read');
  assert.equal(button.attrs['aria-pressed'], 'false');
  assert.match(button.title, /Unlock/i, 'the tooltip names the action, not the state');
  assert.equal(dom.nodes.get('policyEditor').readOnly, true, 'read mode locks the editor');

  api.setMode('edit');
  assert.equal(api.state.mode, 'edit');
  assert.equal(button.attrs['aria-pressed'], 'true', 'pressed state carries the armed mode');
  assert.match(button.title, /Lock/i, 'now the action is to lock again');
  assert.equal(dom.nodes.get('policyEditor').readOnly, false, 'edit mode arms the write surface');
});
