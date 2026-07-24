const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = 'webui_extension/capability-router/router-nav.js';

// A DOM stub rich enough for el()/field(): createElement returns nodes that
// record className/textContent, support append, addEventListener, setAttribute,
// and (for inputs) a mutable value + input-event dispatch.
function fakeDocument() {
  function makeNode(tag) {
    return {
      tagName: tag,
      className: '',
      textContent: '',
      value: '',
      children: [],
      _listeners: {},
      attrs: {},
      append(...nodes) { this.children.push(...nodes); },
      addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
      setAttribute(name, val) { this.attrs[name] = val; },
      dispatch(type) { (this._listeners[type] || []).forEach((fn) => fn()); },
    };
  }
  return {
    createElement: (tag) => makeNode(tag),
    addEventListener() {},
    readyState: 'loading',
  };
}

// Same injection seam as test_router_nav_cooldowns.js: rewrite the IIFE footer
// to publish the write-spine internals. The destructure list lives HERE, not in
// source, so the panel keeps its private surface in production.
function loadSpine(doc) {
  const source = fs.readFileSync(sourcePath, 'utf8').replace(
    /\n}\)\(\);\s*$/,
    '\n  globalThis.__routerNavTest = { postJSON, field, formatPlanDiff, setMode, canWrite, state };\n})();\n',
  );
  const context = {
    console,
    document: doc || { addEventListener() {}, readyState: 'loading' },
    globalThis: {},
    setTimeout() {},
  };
  vm.runInNewContext(source, context, { filename: sourcePath });
  return context.globalThis.__routerNavTest;
}

test('formatPlanDiff prefers the server diff over the preview', () => {
  const { formatPlanDiff } = loadSpine();
  assert.equal(formatPlanDiff({ diff: '--- a\n+++ b\n', preview: { x: 1 } }), '--- a\n+++ b\n');
  assert.equal(formatPlanDiff({ preview: { default: { action: 'T1' } } }),
    JSON.stringify({ default: { action: 'T1' } }, null, 2));
  assert.equal(formatPlanDiff(null), '');
  assert.equal(formatPlanDiff({}), '');
});

test('field builds a labelled input seeded with the value and reports edits', () => {
  const { field } = loadSpine(fakeDocument());
  const appended = [];
  const body = { append(node) { appended.push(node); } };
  let seen = null;

  const input = field(body, 'default.action', 'T3', (val) => { seen = val; });

  assert.equal(input.value, 'T3', 'input seeded with the value');
  assert.equal(input.attrs['aria-label'], 'default.action');
  assert.equal(appended.length, 1, 'one row appended to the body');
  input.value = 'T4';
  input.dispatch('input');
  assert.equal(seen, 'T4', 'onInput receives the edited value');
});

test('setMode toggles the write gate and clears staged plan on read', () => {
  const { setMode, canWrite, state } = loadSpine();
  // Fake panel: query returns simple stubs for .cr-mode and .cr-apply-bar.
  const modeLabel = { textContent: '', setAttribute() {} };
  const bar = { hidden: true };
  const panel = {
    querySelector(sel) {
      if (sel === '.cr-mode') return modeLabel;
      if (sel === '.cr-apply-bar') return bar;
      return null;
    },
  };

  assert.equal(canWrite(), false, 'starts read-only');
  state.plan = { valid: true };
  setMode(panel, 'edit');
  assert.equal(canWrite(), true);
  assert.equal(bar.hidden, false, 'apply bar revealed in edit');
  assert.equal(modeLabel.textContent, 'Editing');

  setMode(panel, 'read');
  assert.equal(canWrite(), false);
  assert.equal(bar.hidden, true, 'apply bar hidden in read');
  assert.equal(state.plan, null, 'staged plan discarded on read so no stale base_hash lingers');
});

test('postJSON posts JSON and throws with status on a non-2xx (409 conflict)', async () => {
  const { postJSON } = loadSpine();
  // postJSON closes over the module fetch; re-run source with a fetch stub in
  // the VM context to observe the request and force a 409.
  const source = fs.readFileSync(sourcePath, 'utf8').replace(
    /\n}\)\(\);\s*$/,
    '\n  globalThis.__routerNavTest = { postJSON };\n})();\n',
  );
  const calls = [];
  const context = {
    console,
    document: { addEventListener() {}, readyState: 'loading' },
    globalThis: {},
    setTimeout() {},
    fetch(url, opts) {
      calls.push({ url, opts });
      return Promise.resolve({
        ok: false,
        status: 409,
        json: () => Promise.resolve({ conflict: true, base_hash: 'abc' }),
      });
    },
  };
  vm.runInNewContext(source, context, { filename: sourcePath });
  const { postJSON: post } = context.globalThis.__routerNavTest;

  await assert.rejects(
    () => post('/apply', { plan: { base_hash: 'x' } }),
    (err) => {
      assert.equal(err.status, 409);
      assert.equal(err.body.conflict, true);
      return true;
    },
  );
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/apply$/);
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(calls[0].opts.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[0].opts.body), { plan: { base_hash: 'x' } });
});
