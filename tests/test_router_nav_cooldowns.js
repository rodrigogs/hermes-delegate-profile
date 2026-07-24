const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = 'webui_extension/capability-router/router-nav.js';

function loadFormatter() {
  const source = fs.readFileSync(sourcePath, 'utf8').replace(
    /\n}\)\(\);\s*$/,
    '\n  globalThis.__routerNavTest = { formatCooldowns, showTab, rollupHealth };\n})();\n',
  );
  const context = {
    console,
    document: { addEventListener() {}, readyState: 'loading' },
    globalThis: {},
    setTimeout() {},
  };
  vm.runInNewContext(source, context, { filename: sourcePath });
  return context.globalThis.__routerNavTest;
}

test('formats breaker cooldown entries with model, state, and remaining time', () => {
  const { formatCooldowns } = loadFormatter();

  assert.equal(formatCooldowns([]), 'none');
  assert.equal(
    formatCooldowns([
      { model_key: 'glm-5.2@zai', state: 'OPEN', cooldown_remaining_s: 42.9 },
      { model_key: 'deepseek-v3.2@deepseek', state: 'HALF_OPEN', cooldown_remaining_s: 62 },
    ]),
    'glm-5.2@zai OPEN 0:42, deepseek-v3.2@deepseek HALF_OPEN 1:02',
  );
});

test('rolls liveness states into a worst-of badge without a rate-limit state', () => {
  const { rollupHealth } = loadFormatter();

  assert.equal(rollupHealth([]), 'alive');
  assert.equal(rollupHealth([{ state: 'alive' }, { state: 'degraded' }]), 'degraded');
  assert.equal(rollupHealth([{ state: 'quota_exhausted' }]), 'quota_exhausted');
  assert.equal(rollupHealth([{ state: 'quota_exhausted' }, { state: 'dead' }]), 'dead');
});

test('showTab scopes visibility and selection to the router panel', () => {
  const { showTab } = loadFormatter();
  const panelA = { hidden: false, dataset: { tabPanel: 'status' } };
  const panelB = { hidden: false, dataset: { tabPanel: 'policy' } };
  const tabA = { dataset: { tab: 'status' }, setAttribute(name, value) { this[name] = value; } };
  const tabB = { dataset: { tab: 'policy' }, setAttribute(name, value) { this[name] = value; } };
  const root = {
    querySelectorAll(selector) {
      return selector === '[data-tab-panel]' ? [panelA, panelB] : [tabA, tabB];
    },
  };

  showTab(root, 'policy');

  assert.equal(panelA.hidden, true);
  assert.equal(panelB.hidden, false);
  assert.equal(tabA['aria-selected'], 'false');
  assert.equal(tabB['aria-selected'], 'true');
});
