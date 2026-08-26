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
  // Navigation and visibility come from HermesPanelNav (the shared Hermes One
  // Extension Kit), because doing it here meant doing it three times, three slightly
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

  // Load the console shell, and on every reopen check whether a deploy replaced it.
  //
  // Reported three times as "ainda vejo a tela antiga", and all three times the
  // server was right. Measured 2026-08-26: the sidecar and the proxy both send
  // `Cache-Control: no-store`, and the WebUI access log shows ZERO console requests
  // from the client in the 30 minutes after a deploy. The console was fetched once
  // per PAGE load, so a tab left open across a deploy held the old document
  // forever. An app whose panels live for hours cannot make a full page reload the
  // only way to see a deploy.
  //
  // Replacing the frame costs the operator their place in the console — scroll, the
  // open tab, a half-typed edit — so the document is swapped only when the bytes
  // actually differ. Bytes that differ ARE the deploy, and that is exactly what
  // must become visible.
  async function load(panel) {
    const frame = panel.querySelector('[data-console-frame]');
    if (!frame) return;
    const loaded = frame.dataset.loaded === 'true';
    try {
      const response = await fetch(`${SIDE}/console`, {
        credentials: 'same-origin',
        // Both ends say no-store: the sidecar on the way out and this on the way
        // in, so a stale copy cannot arrive from a cache the proxy forgot to pass
        // the header to.
        cache: 'no-store',
        headers: { Accept: 'text/html' },
      });
      if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
      const html = await response.text();
      // Unchanged: leave the document the operator is working in untouched.
      if (loaded && frame.srcdoc === html) return;
      // Not once: a reload replaces the document and the observer with it.
      frame.addEventListener('load', watchSections);
      frame.srcdoc = html;
      frame.dataset.loaded = 'true';
    } catch (error) {
      // A failed REFETCH must not destroy a console that is already on screen:
      // renderError removes the frame, and a transient 502 would cost the operator
      // a working document. The console's own data layer already reports a router
      // it cannot reach.
      if (!loaded) renderError(panel, error);
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
    // Re-open of an already-loaded console: the frame fired 'load' long ago, so
    // nothing would re-mirror without this.
    watchSections();
  }

  // A missing shared module must say so. Without this the next line throws
  // "Cannot read properties of undefined", the script dies before installing
  // anything, and the symptom is a button that simply is not there — which is
  // exactly how the office launcher failed silently once already.

  /** The sidebar's section list, kept so the console can report back into it. */
  let sideNav = null;

  // The Office's shape, applied here.
  //
  // This console carried a 45px masthead reading "Capability Router" directly under
  // a rail icon that is already lit and labelled Capability Router, and a row of
  // section tabs below it — 82px of chrome above the content. The Office lost the
  // same masthead for the same reason and gave its column to the host sidebar.
  //
  // This console has no column of its own, so the SECTIONS become the sidebar: the
  // rail selects the panel, the sidebar selects the section, exactly like Chats
  // selects a conversation. The head mirrors the host's .panel-head — 11px/600
  // uppercase at .08em in --muted, one hairline below — so the two read as one
  // system rather than two.
  function buildSidebar(view) {
    const head = el('div', 'panel-head');
    const title = document.createElement('span');
    title.textContent = 'Capability Router';
    head.append(title);
    view.append(head);

    const list = el('nav', 'router-sections');
    list.setAttribute('aria-label', 'Se\u00e7\u00f5es do console');
    for (const [tab, label] of [['pipeline', 'Tarefas'], ['health', 'Modelos'], ['routes', 'Decis\u00f5es']]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'router-section';
      button.dataset.tab = tab;
      button.textContent = label;
      // Same-origin, so the click drives the console's own tab rather than
      // reimplementing its state here: one source of truth for which section is up.
      button.addEventListener('click', () => selectSection(tab));
      list.append(button);
    }
    view.append(list);
    sideNav = list;
    syncSections();
  }

  /** Re-mirror once the console document exists, and whenever it changes section.
   *
   * buildSidebar runs at install time, before the frame has a document, so its own
   * syncSections() finds no tabs and leaves every row unmarked — measured, the
   * console had tab-health aria-selected="true" while all three rows were off. The
   * observer then keeps the two in step when the operator uses the console's own
   * tabs (they still exist; they are only undrawn) or when a section's count moves.
   */
  let sectionObserver = null;

  function watchSections() {
    const frame = document.querySelector('.hermes-one-capability-router-panel [data-console-frame]');
    if (!frame) return;
    let doc = null;
    try { doc = frame.contentDocument; } catch (error) { return; }
    const tablist = doc && doc.querySelector('nav.tabs');
    if (!tablist) return;
    syncSections();
    if (sectionObserver) sectionObserver.disconnect();
    sectionObserver = new MutationObserver(syncSections);
    sectionObserver.observe(tablist, {
      subtree: true, attributes: true,
      attributeFilter: ['aria-selected', 'hidden'], childList: true, characterData: true,
    });
  }

  /** Drive the console's own tab, then mirror its state back into the sidebar. */
  function selectSection(tab) {
    const frame = document.querySelector('.hermes-one-capability-router-panel [data-console-frame]');
    try {
      const doc = frame && frame.contentDocument;
      const button = doc && doc.getElementById(`tab-${tab}`);
      if (button) button.click();
    } catch (error) {
      console.warn('[hermes-one-capability-router] cannot reach the console tabs', error);
    }
    syncSections();
  }

  /** Mirror aria-selected from the console's tabs onto the sidebar rows. */
  function syncSections() {
    if (!sideNav) return;
    const frame = document.querySelector('.hermes-one-capability-router-panel [data-console-frame]');
    let doc = null;
    try { doc = frame && frame.contentDocument; } catch (error) { return; }
    for (const row of sideNav.querySelectorAll('.router-section')) {
      const tab = doc && doc.getElementById(`tab-${row.dataset.tab}`);
      const on = tab ? tab.getAttribute('aria-selected') === 'true' : false;
      row.classList.toggle('is-on', on);
      row.setAttribute('aria-current', on ? 'true' : 'false');
      // The console puts a count and a dot in each tab; carry the count across so
      // the sidebar says how many, not just which.
      const count = tab && tab.querySelector('.count');
      const n = count && !count.hasAttribute('hidden') ? count.textContent.trim() : '';
      let badge = row.querySelector('.router-section-n');
      if (n) {
        if (!badge) { badge = el('span', 'router-section-n'); row.append(badge); }
        badge.textContent = n;
      } else if (badge) badge.remove();
    }
  }

  if (!window.HermesPanelNav) {
    console.error('[hermes-one-capability-router] Hermes One Extension Kit did not load; '
      + 'the Router button cannot be installed. Check that "hermes-one-extension-kit" is '
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
    sidebarView: buildSidebar,
  });
})();
