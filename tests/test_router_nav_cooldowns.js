const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = 'webui_extension/capability-router/router-nav.js';

function loadFormatter() {
  const source = fs.readFileSync(sourcePath, 'utf8').replace(
    /\n}\)\(\);\s*$/,
    '\n  globalThis.__routerNavTest = { formatCooldowns };\n})();\n',
  );
  const context = {
    console,
    document: { addEventListener() {}, readyState: 'loading' },
    globalThis: {},
    setTimeout() {},
  };
  vm.runInNewContext(source, context, { filename: sourcePath });
  return context.globalThis.__routerNavTest.formatCooldowns;
}

test('formats breaker cooldown entries with model, state, and remaining time', () => {
  const formatCooldowns = loadFormatter();

  assert.equal(formatCooldowns([]), 'none');
  assert.equal(
    formatCooldowns([
      { model_key: 'glm-5.2@zai', state: 'OPEN', cooldown_remaining_s: 42.9 },
      { model_key: 'deepseek-v3.2@deepseek', state: 'HALF_OPEN', cooldown_remaining_s: 62 },
    ]),
    'glm-5.2@zai OPEN 0:42, deepseek-v3.2@deepseek HALF_OPEN 1:02',
  );
});
