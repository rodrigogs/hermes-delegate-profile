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

const sourcePath = 'webui_extension/capability-router/console.html';

// A DOM stub good enough for the console's init path.
function fakeDom() {
  const nodes = new Map();
  const make = (id) => {
    const node = {
      id, className: '', textContent: '', value: '', title: '',
      hidden: false, readOnly: false, max: '0',
      style: {}, dataset: {}, attrs: {}, children: [],
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
      addEventListener() {},
      setAttribute(n, v) { node.attrs[n] = String(v); },
      getAttribute(n) { return node.attrs[n]; },
      querySelector(sel) { return get(`${id}${sel}`); },
      querySelectorAll() { return []; },
      getBoundingClientRect() { return { width: 900, height: 300, left: 0, right: 900 }; },
      clientWidth: 900,
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

test('a step says what it concluded, so the JSON is optional', () => {
  const { api } = loadConsole();
  assert.equal(api.stepOutcome({ out: { rule_id: 'hard-verbs' } }), 'hard-verbs');
  assert.equal(api.stepOutcome({ out: { deny: true } }), 'refused');
  assert.equal(api.stepOutcome({ out: { blocked: false } }), 'clear');
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

  assert.equal(dom.get('countPipeline').textContent, '3', 'pipeline counts rules');
  assert.equal(dom.get('countRoutes').textContent, '2', 'routes counts recorded decisions');
  // One degraded target must surface, not be averaged into "fine".
  assert.match(dom.get('stateHealth').className, /is-degraded/);
});

test('the rail survives being rendered before any data arrives', () => {
  const { api, dom } = loadConsole();
  // setMode() renders the rail at init, before the first poll. An unguarded read
  // here kills the whole IIFE and the operator gets a blank page.
  assert.doesNotThrow(() => api.renderRail());
  assert.equal(dom.get('countPipeline').hidden, true, 'no policy yet → no count shown');
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
  assert.match(button.title, /Edit the routing policy/,
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
  assert.equal(api.absence('No routable models reported.'), 'Loading…');

  api.state.loading = false;
  api.state.unreachable = true;
  assert.match(api.absence('No routable models reported.'), /unreachable/);

  api.state.unreachable = false;
  assert.equal(api.absence('No routable models reported.'), 'No routable models reported.');
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
  assert.match(msg.textContent, /changed underneath/);
  assert.match(msg.className, /bad/);
  assert.equal(api.state.plan, null, 'the stale plan must not survive to be applied again');
});

test('nothing to apply is said, not silently ignored', async () => {
  let called = 0;
  const { api } = loadConsole({ csrfToken: 'tok', fetch: () => { called += 1; return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') }); } });
  const msg = { textContent: '', className: '' };
  await api.doApply('/apply', msg);
  assert.match(msg.textContent, /Nothing to apply/);
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
  assert.match(msg.textContent, /no \/plan endpoint/);
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
  assert.match(text(rows[0]), /13s cooldown/);
  assert.match(text(rows[0]), /degraded/, 'a cooling model is degraded, not dead');
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
  assert.match(dom.get('modelsNote').textContent, /all 2 reachable/);

  api.state.unreachable = true;
  api.renderHealth();
  assert.doesNotMatch(dom.get('modelsNote').textContent, /reachable/,
    'a dead sidecar must not yield a reachability claim');
  assert.match(dom.get('modelsNote').textContent, /last known/);
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
  assert.match(dom.get('modelsNote').textContent, /2 of 3 not routable/);
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
  api.renderHealth();
  const note = dom.get('compactionNote').textContent;
  assert.match(note, /fires once/, 'it must read as a trigger, not a level');
  assert.match(note, /77%/);
  assert.match(note, /272,000/, 'the window is separated for comparison');
  assert.doesNotMatch(note, /^at \d/, 'a bare "at 77%" reads as current usage');
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
  api.renderHealth();
  const text = JSON.stringify(dom.get('compaction'));
  assert.match(text, /balanced/, 'the server has names for these presets');
  assert.match(text, /compacts sooner/, 'and the direction must be stated');
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
  assert.match(pill, /Not by rule/);
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
  assert.match(note, /4 refused/);
  assert.match(note, /17 caught by the fail-safe/);
  assert.match(note, /21 of 50/, 'and the subset is scoped to what is on screen');
});

test('a truncated log says so, and an untruncated one stays quiet', () => {
  const { api, dom } = loadConsole();
  decisionLog(api, { total: 71 });
  api.renderRoutes();
  // 50 rows presented as the record understated every hit count by 27%, and a rule
  // that fired only in the dropped 21 would have rendered "never fired".
  assert.match(dom.get('routesNote').textContent, /most recent 50 of 71 recorded/);

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
  assert.match(note, /19 of 50 match/);
  assert.match(note, /most recent 50 of 71/, 'the window applies to a search too');
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
  assert.match(msg.textContent, /already the policy on disk/);
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

  assert.match(second.textContent, /Still writing/, 'the second click is told to wait');
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
    if (node.className === 'step-target' && node.style && node.style.color) {
      painted.push([node.textContent, node.style.color]);
    }
    (node.children || []).forEach(walk);
  };
  walk(dom.get('sheet'));

  // Exactly the two refusals: the blocklist veto and the deny rule.
  assert.deepEqual(painted.map((p) => p[0]).sort(), ['refuse', 'refuse'],
    `only refusals are coloured, got ${JSON.stringify(painted)}`);
  for (const [, colour] of painted) {
    assert.match(colour, /--bad-text/, 'and a refusal is the danger token');
  }
  // The classifier's destinations exist and are legible — they are just not paint.
  const words = JSON.stringify(dom.get('sheet'));
  assert.match(words, /classifier/, 'the classifier is still named');
  assert.match(words, /costs a model call/, 'and inference is still flagged, once');
});

test('on a phone the clock yields, never the name of the surface', () => {
  // Measured at 390px: the header's three items claimed 232px and left the title
  // 114px, so "Capability Router" rendered as "Capability R…". The one element
  // that says what you are looking at was the one that gave way. Dropping the
  // "checked HH:MM" text returns 99px, which fits the title whole — so the clock
  // is what collapses, and only while there is nothing to report about the read.
  const { api, dom } = loadConsole();

  api.state.checkedAt = new Date();
  api.renderRail();
  assert.match(dom.get('reach').className, /is-fresh/,
    'a current read is collapsible at phone width');

  // A dead sidecar keeps its words at every width: that is a condition, not chrome.
  api.state.unreachable = true;
  api.renderRail();
  assert.doesNotMatch(dom.get('reach').className, /is-fresh/);
  assert.match(dom.get('reachText').textContent, /unreachable/);

  // And so does "we have not read anything yet", which is not the same as fine.
  api.state.unreachable = false;
  api.state.checkedAt = null;
  api.renderRail();
  assert.doesNotMatch(dom.get('reach').className, /is-fresh/);
  assert.equal(dom.get('reachText').textContent, 'checking');

  // The title itself never truncates by being hidden — it is always in the DOM.
  const fs = require('node:fs');
  const html = fs.readFileSync(sourcePath, 'utf8');
  assert.match(html, /<h1 class="view-title">Capability Router<\/h1>/);
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

  assert.match(sentence, /Caught by hard-verbs/, 'words must not run together');
  assert.match(sentence, /routed to gpt-5\.6-terra/);
  assert.match(sentence, /on openai-codex/);
  assert.match(sentence, /Falls back to us\.anthropic\.claude-opus-5 → deepseek-v4-pro/);
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
