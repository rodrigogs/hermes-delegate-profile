// The extension script's whole job is now navigation + mounting the console.
//
// It stopped being a second UI: it used to reimplement every read the console
// already does, so the two could disagree and only one was maintained. What
// remains is load-bearing for a different reason — mounting inside the host
// document is the ONLY way writing works, because the WebUI grants its CSRF
// token only to pages it renders itself. These tests pin that mechanism.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = 'webui_extension/hermes-one-capability-router/router-nav.js';

function fakeDom() {
  const make = (tag) => {
    const node = {
      tagName: tag, className: '', textContent: '', title: '', id: '',
      hidden: false, srcdoc: '', innerHTML: '', dataset: {}, attrs: {},
      children: [], parent: null,
      append(...kids) { kids.forEach((k) => { k.parent = node; node.children.push(k); }); },
      insertBefore(k) { k.parent = node; node.children.unshift(k); },
      remove() { if (node.parent) node.parent.children = node.parent.children.filter((c) => c !== node); },
      addEventListener(type, fn) { (node._on ||= {})[type] = fn; },
      setAttribute(n, v) { node.attrs[n] = String(v); },
      getAttribute(n) { return node.attrs[n]; },
      // Depth-first search over the tree we actually build.
      querySelector(sel) { return descendants(node).find((n) => matches(n, sel)) || null; },
      querySelectorAll(sel) { return descendants(node).filter((n) => matches(n, sel)); },
    };
    return node;
  };
  const descendants = (root) => root.children.flatMap((c) => [c, ...descendants(c)]);
  const matches = (n, sel) => {
    if (sel.startsWith('[data-') ) {
      const key = sel.slice(6, sel.indexOf(']')).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return n.dataset[key] !== undefined;
    }
    if (sel.startsWith('.')) return String(n.className).split(' ').includes(sel.slice(1));
    return false;
  };
  const main = make('main');
  const rail = make('div'); rail.className = 'rail';
  const sidebar = make('nav'); sidebar.className = 'sidebar-nav';
  const byId = new Map();
  const document = {
    createElement: make,
    getElementById: (id) => byId.get(id) || null,
    querySelector: (sel) => sel === 'main' ? main : sel === '.rail' ? rail : sel === '.sidebar-nav' ? sidebar : null,
    querySelectorAll: (sel) => sel === 'main > .main-view'
      ? main.children.filter((c) => String(c.className).includes('main-view')) : [],
    addEventListener() {}, readyState: 'complete',
    documentElement: make('html'),
  };
  return { document, main, rail, sidebar, byId };
}

function loadNav({ fetchStub } = {}) {
  const source = fs.readFileSync(sourcePath, 'utf8').replace(
    /\n}\)\(\);\s*$/,
    '\n  globalThis.__nav = { ensurePanel, load, onOpen, renderError };\n})();\n',
  );
  const dom = fakeDom();
  // Navigation moved to the shared hermes-panel-nav module, so this script now
  // returns early unless window.HermesPanelNav exists — a deliberate guard, because
  // the alternative is a button that silently never installs. This file was left
  // without a `window` at all and went 4/4 red on disk while nothing ran it. The
  // stub supplies the smallest honest double: register() hands back the same three
  // methods the real one does, and records what the surface asked for.
  const registered = [];
  const window = {
    HermesPanelNav: {
      register(spec) {
        registered.push(spec);
        return {
          open: () => spec.onOpen(),
          show: () => { shown.push(spec.token); },
          adopt: (element) => { element.dataset.panelToken = spec.token; element.hidden = true; return element; },
        };
      },
    },
  };
  const shown = [];
  const context = {
    console, document: dom.document, window, globalThis: {}, setTimeout() {}, Object,
    MutationObserver: class { observe() {} disconnect() {} },
    fetch: fetchStub || (() => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('<html>console</html>') })),
  };
  vm.runInNewContext(source, context, { filename: sourcePath });
  // The panel registers itself by id the way getElementById would find it.
  const api = context.globalThis.__nav;
  const wrapped = { ...api, ensurePanel: () => { const p = api.ensurePanel(); dom.byId.set(p.id, p); return p; } };
  return { api: wrapped, dom, registered, shown };
}

test('the console is mounted in the host main view, not opened as its own screen', () => {
  const { api, dom } = loadNav();
  const panel = api.ensurePanel();
  // A new screen would put the console outside the panel that holds Sessions and
  // Memory — and, fatally, outside the document that owns the CSRF token.
  assert.ok(String(panel.className).includes('main-view'));
  assert.equal(panel.parent, dom.main, 'the panel lives inside <main>');
  // adopt() sets this now, which is the point of routing it through the shared
  // module: the rule "a panel does not appear before it is asked for" is enforced in
  // one place for all three surfaces instead of three times.
  assert.equal(panel.hidden, true, 'mounting must not steal the current view');
  assert.equal(panel.dataset.panelToken, 'router', 'and the panel is adopted');
});

test('the frame is srcdoc, because a served page cannot be framed at all', async () => {
  const fetched = [];
  const { api } = loadNav({
    fetchStub: (url, opts) => {
      fetched.push({ url, credentials: opts.credentials, cache: opts.cache });
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('<html>the console</html>') });
    },
  });
  const panel = api.ensurePanel();
  await api.load(panel);
  const frame = panel.querySelector('[data-console-frame]');
  // src= would be refused: the sidecar sends X-Frame-Options DENY and
  // frame-ancestors 'none'. srcdoc inherits this origin, which is what lets the
  // console reach the proxy with cookies and borrow the host's token.
  assert.equal(frame.srcdoc, '<html>the console</html>');
  assert.equal(frame.attrs.src, undefined, 'never framed by URL');
  assert.match(fetched[0].url, /\/console$/);
  assert.equal(fetched[0].credentials, 'same-origin', 'the proxy needs the session cookie');
  // Os dois lados dizem no-store (o sidecar na ida, isto na volta). O que faz um
  // deploy aparecer sem recarregar a página é o refetch por abertura, no teste
  // seguinte. Medido em 2026-08-26.
  assert.equal(fetched[0].cache, 'no-store', 'um console cacheado é um deploy que não aparece');
});

test('reopening refetches, and an unchanged console keeps the operator in place', async () => {
  let calls = 0;
  const { api } = loadNav({
    fetchStub: () => { calls += 1; return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('<html/>') }); },
  });
  const panel = api.ensurePanel();
  await api.load(panel);
  const frame = panel.querySelector('[data-console-frame]');
  const first = frame.srcdoc;
  await api.load(panel);
  // Refetched: a tab left open across a deploy was holding the old document, and a
  // full page reload was the only way out of it (measured 2026-08-26).
  assert.equal(calls, 2);
  // ...and nothing was replaced, because the bytes were identical: the operator's
  // tab, filter and selected trace survive.
  assert.equal(frame.srcdoc, first);
});

test('a console replaced by a deploy shows up when the panel is reopened', async () => {
  const bodies = ['<html>antigo</html>', '<html>novo</html>'];
  const { api } = loadNav({
    fetchStub: () => Promise.resolve({
      ok: true, status: 200, text: () => Promise.resolve(bodies.shift() || '<html>novo</html>'),
    }),
  });
  const panel = api.ensurePanel();
  await api.load(panel);
  await api.load(panel);
  assert.equal(panel.querySelector('[data-console-frame]').srcdoc, '<html>novo</html>');
});

test('a refetch that fails keeps the console already on screen', async () => {
  let first = true;
  const { api } = loadNav({
    fetchStub: () => {
      if (first) {
        first = false;
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('<html>ok</html>') });
      }
      return Promise.resolve({ ok: false, status: 502, text: () => Promise.resolve('') });
    },
  });
  const panel = api.ensurePanel();
  await api.load(panel);
  await api.load(panel);
  const frame = panel.querySelector('[data-console-frame]');
  assert.ok(frame, 'um 502 passageiro não pode custar um console que funciona');
  assert.equal(frame.srcdoc, '<html>ok</html>');
  assert.equal(panel.querySelector('.hp-error'), null, 'nada de erro sobre um console vivo');
});

test('a console that cannot be fetched says which fix applies', async () => {
  for (const [status, expected] of [[403, /not consented/], [503, /token file missing/], [500, /HTTP 500/]]) {
    const { api } = loadNav({ fetchStub: () => Promise.resolve({ ok: false, status, text: () => Promise.resolve('') }) });
    const panel = api.ensurePanel();
    await api.load(panel);
    const error = panel.querySelector('.hp-error');
    assert.match(error.textContent, expected);
    assert.equal(error.attrs.role, 'alert', 'a blocked console must be announced');
    assert.equal(panel.querySelector('[data-console-frame]'), null, 'no blank frame left behind');
  }
});
