(() => {
  'use strict';

  /*
   * THESIS: Put the router console where an operator already works, and where it
   *   can actually write.
   * OWN-WORLD: The host's rail and main-view; the console owns its own world once
   *   mounted. This file contributes navigation and a frame, nothing visual.
   * STORY: An operator taps Router — in the rail on a desktop, in the drawer on a
   *   phone — the console opens in the same panel that holds Sessions and Memory,
   *   the drawer closes behind it, and editing works there.
   * FIRST VIEWPORT: The console's own — this file adds no chrome above it.
   * FORM: Existing Hermes One rail/sidebar extension, not a new application route.
   */
  // Capability Router — Hermes One extension.
  //
  // This file used to carry a five-tab UI that duplicated every read the console
  // already does, which meant two surfaces to keep true and only one of them
  // actually maintained. It is now a frame around the single console at /console.
  //
  // Mounting matters for more than tidiness: the WebUI derives a CSRF token from
  // the session and injects it only into pages it renders itself (/, /index.html,
  // /session/*). A console served by the sidecar therefore never receives one and
  // can read but never write. Mounted under the host document it is same-origin,
  // reaches the consented sidecar proxy with cookies, and borrows the host's
  // token via window.parent. Verified end to end: POST /plan answers 200 from
  // inside this frame and 403 from a standalone tab.
  //
  // Navigation and visibility come from HermesPanelNav (the shared hermes-panel
  // extension), because doing it here meant doing it three times, three slightly
  // different ways. It implements the pattern the host documents for extensions
  // (hermes-webui/docs/EXTENSIONS.md:551-580): toggle `hidden` across
  // main > .main-view, and drive the host's own switchPanel rather than writing its
  // classes off behind its back. See that module for what each rule fixes.
  const EXT_ID = 'hermes-one-capability-router';
  const SIDE = `/api/extensions/${EXT_ID}/sidecar`;
  const PANEL_ID = 'hermes-one-capability-router-panel';
  const ICON = '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/>'
    + '<circle cx="18" cy="12" r="3"/><path d="M9 6h4a2 2 0 0 1 2 2v1"/>'
    + '<path d="M9 18h4a2 2 0 0 0 2-2v-1"/>';

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  let nav = null;

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    panel = el('section', 'main-view hermes-panel hermes-one-capability-router-panel');
    panel.id = PANEL_ID;
    // srcdoc, not src: the sidecar sends X-Frame-Options DENY and
    // frame-ancestors 'none', so the served page cannot be framed by URL. srcdoc
    // inherits this document's origin, which is the whole point — it is what lets
    // the console reach the proxy with cookies and read the host's token.
    const frame = el('iframe', 'hp-frame');
    frame.title = 'Capability Router console';
    frame.dataset.consoleFrame = 'true';
    panel.append(frame);
    document.querySelector('main')?.append(panel);
    if (nav) nav.adopt(panel);
    return panel;
  }

  // Load the console shell once; reopening the panel must not refetch it or throw
  // away the operator's place in it.
  async function load(panel) {
    const frame = panel.querySelector('[data-console-frame]');
    if (!frame || frame.dataset.loaded === 'true') return;
    try {
      const response = await fetch(`${SIDE}/console`, {
        credentials: 'same-origin',
        headers: { Accept: 'text/html' },
      });
      if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
      frame.srcdoc = await response.text();
      frame.dataset.loaded = 'true';
    } catch (error) {
      renderError(panel, error);
    }
  }

  // The console reports on the router; only this file can report that the console
  // itself could not be fetched.
  function renderError(panel, error) {
    panel.querySelector('[data-console-frame]')?.remove();
    let message = panel.querySelector('.hp-error');
    if (!message) {
      message = el('div', 'hp-error');
      message.setAttribute('role', 'alert');
      message.setAttribute('aria-live', 'assertive');
      panel.append(message);
    }
    const code = error?.status || '?';
    message.textContent = code === 403
      ? 'Sidecar proxy not consented. Approve it in Settings → Extensions, then refresh.'
      : code === 503
        ? 'Sidecar token file missing (503). Start the router-sidecar service, then refresh.'
        : `Could not reach the router sidecar (HTTP ${code}).`;
  }

  function onOpen() {
    const panel = ensurePanel();
    // The panel is created after register(), so the first open is what tags it;
    // show() then finds it by token. Re-showing is idempotent.
    if (nav) nav.show();
    load(panel);
  }

  // A missing shared module must say so. Without this the next line throws
  // "Cannot read properties of undefined", the script dies before installing
  // anything, and the symptom is a button that simply is not there — which is
  // exactly how the office launcher failed silently once already.
  if (!window.HermesPanelNav) {
    console.error('[hermes-one-capability-router] hermes-panel extension did not load; '
      + 'the Router button cannot be installed. Check that "hermes-panel" is '
      + 'listed BEFORE "hermes-one-capability-router" in extensions.json.');
    return;
  }

  nav = window.HermesPanelNav.register({
    token: 'router',
    label: 'Router',
    title: 'Capability Router',
    iconPath: ICON,
    navClass: 'hermes-one-capability-router-nav',
    onOpen,
  });
})();
