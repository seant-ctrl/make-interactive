/* make-interactive overlay — designer comment canvas.
 *
 * Architecture:
 *  - A single shadow-DOM host attached to <html> isolates overlay styles from the page.
 *  - The host contains: floating toolbar, pin layer (positioned over targets), modal layer, toast.
 *  - SSE connection to /api/events drives live reload after Claude edits the source.
 *  - Drafts live in-memory until "Send" — then POSTed as a batch.
 *  - Pin badges are anchored to elements via CSS selectors + bounding rects, repositioned on scroll/resize.
 */
(() => {
  if (window.__miLoaded) return;
  window.__miLoaded = true;

  const QUICK_ACTIONS = [
    { id: 'rewrite',  label: 'Rewrite' },
    { id: 'tighter',  label: 'Tighter' },
    { id: 'clearer',  label: 'Clearer' },
    { id: 'variants', label: 'Variants' },
    { id: 'copy',     label: 'Copy only' },
    { id: 'layout',   label: 'Layout only' },
    { id: 'motion',   label: 'Add motion' },
    { id: 'question', label: 'Ask, don’t edit' },
  ];

  /* ---------- Shadow host & UI shell ---------- */
  const host = document.createElement('div');
  host.id = 'mi-host';
  host.setAttribute('data-mi-skip', '1');
  document.documentElement.appendChild(host);
  const root = host.attachShadow({ mode: 'open' });

  const styleLink = document.createElement('link');
  styleLink.rel = 'stylesheet';
  styleLink.href = '/__overlay.css';
  root.appendChild(styleLink);

  const shell = document.createElement('div');
  shell.className = 'mi-shell';
  shell.innerHTML = `
    <div class="mi-toolbar" data-mode="off">
      <button class="mi-btn mi-toggle" title="Toggle comment mode (c)">
        <span class="mi-dot"></span>
        <span class="mi-toggle-label">Comment</span>
      </button>
      <div class="mi-mode-switch" hidden>
        <button class="mi-mode" data-submode="pin" title="Click any element to pin a comment">\u{1F4CC} Pin element</button>
        <button class="mi-mode" data-submode="select" title="Highlight text in the page to comment">✏️ Highlight text</button>
      </div>
      <button class="mi-btn mi-send" hidden>
        Send <span class="mi-count">0</span>
      </button>
    </div>
    <div class="mi-pin-layer"></div>
    <div class="mi-modal-host" hidden></div>
    <div class="mi-selection-cta" hidden>\u{1F4AC} Comment</div>
    <div class="mi-toast" hidden></div>
    <div class="mi-hover-outline" hidden></div>
  `;
  root.appendChild(shell);

  const $ = (sel) => root.querySelector(sel);
  const $$ = (sel) => [...root.querySelectorAll(sel)];

  const toolbar = $('.mi-toolbar');
  const toggleBtn = $('.mi-toggle');
  const modeSwitch = $('.mi-mode-switch');
  const sendBtn = $('.mi-send');
  const countEl = $('.mi-count');
  const pinLayer = $('.mi-pin-layer');
  const modalHost = $('.mi-modal-host');
  const selectionCTA = $('.mi-selection-cta');
  const toast = $('.mi-toast');
  const hoverOutline = $('.mi-hover-outline');

  /* ---------- State ---------- */
  const state = {
    mode: 'off',      // 'off' | 'pin' | 'select'
    drafts: [],       // unsent comments {tempId, mode, selector, xpath, previewHTML, selectionText, comment, quickAction, anchor}
    pins: new Map(),  // id (server c-id OR tempId) -> {entry, badgeEl, target}
  };

  /* ---------- Helpers ---------- */
  // Bulletproof shadow-DOM detection: use composedPath when given an Event,
  // walk parentNode/host chain when given an Element. event.target retargeting
  // is not consistently reliable across browsers + framework wrappers.
  function isOverlay(eventOrEl) {
    if (eventOrEl && typeof eventOrEl.composedPath === 'function') {
      const path = eventOrEl.composedPath();
      for (let i = 0; i < path.length; i++) if (path[i] === host) return true;
      return false;
    }
    let el = eventOrEl;
    while (el) {
      if (el === host) return true;
      el = el.parentNode || el.host || null;
    }
    return false;
  }

  // Is the keyboard focus currently inside our overlay (textarea, etc.)?
  function focusInsideOverlay() {
    let el = document.activeElement;
    while (el) {
      if (el === host) return true;
      // hop through shadow boundary
      if (el.shadowRoot && el.shadowRoot.activeElement) {
        el = el.shadowRoot.activeElement;
        continue;
      }
      el = el.parentNode || el.host || null;
    }
    // also check shadow root's active element
    return root.activeElement != null;
  }

  function getSelector(el) {
    if (!el || el.nodeType !== 1) return null;
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && cur !== document.documentElement) {
      let part = cur.tagName.toLowerCase();
      if (cur.classList && cur.classList.length) {
        const cls = [...cur.classList].filter(c => !c.startsWith('mi-')).slice(0, 2);
        if (cls.length) part += '.' + cls.map(c => CSS.escape(c)).join('.');
      }
      const parent = cur.parentElement;
      if (parent) {
        const sibs = [...parent.children].filter(s => s.tagName === cur.tagName);
        if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  function getXPath(el) {
    if (!el || el.nodeType !== 1) return null;
    if (el.id) return `//*[@id="${el.id}"]`;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1) {
      let idx = 1;
      let sib = cur.previousElementSibling;
      while (sib) {
        if (sib.tagName === cur.tagName) idx++;
        sib = sib.previousElementSibling;
      }
      parts.unshift(`${cur.tagName.toLowerCase()}[${idx}]`);
      cur = cur.parentElement;
    }
    return '/' + parts.join('/');
  }

  function previewOf(el) {
    if (!el) return null;
    const html = el.outerHTML || '';
    return html.length > 320 ? html.slice(0, 320) + '…' : html;
  }

  function viewportLabel() {
    const w = window.innerWidth;
    if (w < 640) return 'mobile';
    if (w < 1024) return 'tablet';
    return 'desktop';
  }

  function showToast(msg, ms = 2200) {
    toast.textContent = msg;
    toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toast.hidden = true; }, ms);
  }

  /* ---------- Mode management ---------- */
  function setMode(next) {
    const prev = state.mode;
    state.mode = next;
    toolbar.dataset.mode = next;
    document.documentElement.dataset.miMode = next;
    modeSwitch.hidden = next === 'off';
    $$('.mi-mode').forEach(b => b.classList.toggle('mi-active', b.dataset.submode === next));
    hoverOutline.hidden = true;
    hideSelectionCTA();
    if (next === 'off') closeModal();
    if (prev !== next && next === 'select') showToast('Highlight any text in the page to comment on it.', 2600);
    if (prev !== next && next === 'pin' && prev === 'off') showToast('Click any element to pin a comment.', 2200);
  }

  toggleBtn.addEventListener('click', () => {
    setMode(state.mode === 'off' ? 'pin' : 'off');
  });
  $$('.mi-mode').forEach(b => {
    b.addEventListener('click', () => setMode(b.dataset.submode));
  });

  document.addEventListener('keydown', (e) => {
    // Don't hijack keys when typing — either in the page or inside our overlay
    if (isOverlay(e) || focusInsideOverlay()) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (e.key === 'c' || e.key === 'C') {
      e.preventDefault();
      setMode(state.mode === 'off' ? 'pin' : 'off');
    } else if (e.key === 'Escape') {
      closeModal();
      if (state.mode !== 'off') setMode('off');
    }
  });

  /* ---------- Pin mode: hover outline + click capture ---------- */
  document.addEventListener('mousemove', (e) => {
    if (state.mode !== 'pin') return;
    if (isOverlay(e)) { hoverOutline.hidden = true; return; }
    const el = e.target;
    if (!el || el.nodeType !== 1) { hoverOutline.hidden = true; return; }
    const r = el.getBoundingClientRect();
    Object.assign(hoverOutline.style, {
      left: r.left + 'px',
      top: r.top + 'px',
      width: r.width + 'px',
      height: r.height + 'px',
    });
    hoverOutline.hidden = false;
  }, true);

  document.addEventListener('click', (e) => {
    if (state.mode !== 'pin') return;
    // The single most important check: never treat an overlay click as a pin click.
    if (isOverlay(e)) return;
    const el = e.target;
    if (!el || el.nodeType !== 1) return;
    e.preventDefault();
    e.stopPropagation();
    openModalFor({
      mode: 'pin',
      target: el,
      selector: getSelector(el),
      xpath: getXPath(el),
      previewHTML: previewOf(el),
      anchor: { x: e.clientX, y: e.clientY },
    });
  }, true);

  // Also intercept mousedown so the page can't react before we cancel.
  document.addEventListener('mousedown', (e) => {
    if (state.mode !== 'pin') return;
    if (isOverlay(e)) return;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  /* ---------- Select mode: text selection -> floating CTA ---------- */
  let pendingSelection = null;

  document.addEventListener('mouseup', (e) => {
    if (state.mode !== 'select') { hideSelectionCTA(); return; }
    if (isOverlay(e)) return; // don't process selection on overlay clicks
    setTimeout(() => {
      const sel = window.getSelection();
      const txt = sel && sel.toString().trim();
      if (!txt) { hideSelectionCTA(); return; }
      const range = sel.getRangeAt(0);
      const rect = range.getBoundingClientRect();
      if (!rect || (rect.width === 0 && rect.height === 0)) { hideSelectionCTA(); return; }
      let container = range.commonAncestorContainer;
      if (container.nodeType !== 1) container = container.parentElement;
      if (isOverlay(container)) { hideSelectionCTA(); return; }
      pendingSelection = {
        mode: 'select',
        target: container,
        selector: getSelector(container),
        xpath: getXPath(container),
        previewHTML: previewOf(container),
        selectionText: txt,
        anchor: { x: rect.right, y: rect.bottom },
      };
      selectionCTA.style.left = (rect.right + 8) + 'px';
      selectionCTA.style.top = (rect.bottom + 6) + 'px';
      selectionCTA.hidden = false;
    }, 0);
  });

  selectionCTA.addEventListener('click', () => {
    if (!pendingSelection) return;
    openModalFor(pendingSelection);
    hideSelectionCTA();
  });

  function hideSelectionCTA() {
    selectionCTA.hidden = true;
    pendingSelection = null;
  }

  /* ---------- Modal ---------- */
  function openModalFor(ctx) {
    modalHost.hidden = false;
    modalHost.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.className = 'mi-modal';

    const preview = ctx.selectionText
      ? `“${ctx.selectionText.length > 180 ? ctx.selectionText.slice(0, 180) + '…' : ctx.selectionText}”`
      : truncate(stripTags(ctx.previewHTML || ''), 120) || '(element)';

    wrap.innerHTML = `
      <div class="mi-modal-head">
        <span class="mi-modal-mode">${ctx.mode === 'pin' ? '\u{1F4CC} Pin' : '✏️ Selection'}</span>
        <span class="mi-modal-preview" title="${escapeAttr(ctx.selector || '')}">${escapeHtml(preview)}</span>
        <button class="mi-modal-close" aria-label="Close">×</button>
      </div>
      <textarea class="mi-textarea" rows="3" placeholder="What should change here? (Cmd/Ctrl+Enter to send)"></textarea>
      <div class="mi-chips">
        ${QUICK_ACTIONS.map(a => `<button class="mi-chip" data-id="${a.id}">${a.label}</button>`).join('')}
      </div>
      <div class="mi-modal-foot">
        <button class="mi-btn mi-ghost mi-add">Add to batch</button>
        <button class="mi-btn mi-primary mi-send-now">Send now</button>
      </div>
    `;
    modalHost.appendChild(wrap);

    // Position near anchor, but keep within viewport
    const margin = 12;
    const w = 360, h = 240;
    const ax = ctx.anchor ? ctx.anchor.x : window.innerWidth / 2;
    const ay = ctx.anchor ? ctx.anchor.y : window.innerHeight / 2;
    let left = Math.min(ax + 16, window.innerWidth - w - margin);
    let top  = Math.min(ay + 16, window.innerHeight - h - margin);
    left = Math.max(margin, left);
    top  = Math.max(margin, top);
    wrap.style.left = left + 'px';
    wrap.style.top = top + 'px';

    const ta = wrap.querySelector('.mi-textarea');
    setTimeout(() => ta.focus(), 0);

    let activeChip = null;
    wrap.querySelectorAll('.mi-chip').forEach(c => {
      c.addEventListener('click', () => {
        if (activeChip === c) {
          c.classList.remove('mi-active');
          activeChip = null;
        } else {
          wrap.querySelectorAll('.mi-chip').forEach(x => x.classList.remove('mi-active'));
          c.classList.add('mi-active');
          activeChip = c;
        }
      });
    });

    wrap.querySelector('.mi-modal-close').addEventListener('click', closeModal);

    function commit(sendNow) {
      const txt = ta.value.trim();
      if (!txt && !activeChip) { ta.focus(); return; }
      const draft = {
        tempId: 't' + Date.now() + Math.floor(Math.random() * 1000),
        mode: ctx.mode,
        selector: ctx.selector,
        xpath: ctx.xpath,
        previewHTML: ctx.previewHTML,
        selectionText: ctx.selectionText || null,
        comment: txt,
        quickAction: activeChip ? activeChip.dataset.id : null,
        viewport: viewportLabel(),
        anchor: ctx.anchor,
        target: ctx.target,
      };
      state.drafts.push(draft);
      renderDraftPin(draft);
      updateSendButton();
      closeModal();
      if (sendNow) sendBatch();
    }

    wrap.querySelector('.mi-add').addEventListener('click', () => commit(false));
    wrap.querySelector('.mi-send-now').addEventListener('click', () => commit(true));
    ta.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        commit(true);
      }
    });
  }

  function closeModal() {
    modalHost.hidden = true;
    modalHost.innerHTML = '';
  }

  function truncate(s, n) { return s.length > n ? s.slice(0, n) + '…' : s; }
  function stripTags(s) { return s.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(); }
  function escapeHtml(s) { return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function escapeAttr(s) { return escapeHtml(String(s)); }

  /* ---------- Pin badges ---------- */
  function countPinsOnTarget(target) {
    let n = 0;
    state.pins.forEach(p => { if (p.target === target) n++; });
    return n;
  }

  function renderDraftPin(draft) {
    const badge = document.createElement('div');
    badge.className = 'mi-pin mi-pin-draft';
    badge.textContent = state.pins.size + 1;
    badge.title = (draft.quickAction ? `[${draft.quickAction}] ` : '') + draft.comment;
    badge.addEventListener('click', (e) => {
      e.stopPropagation();
      const idx = state.drafts.indexOf(draft);
      if (idx >= 0) state.drafts.splice(idx, 1);
      state.pins.delete(draft.tempId);
      badge.remove();
      updateSendButton();
      // re-pack remaining pins on the same target
      repackTarget(draft.target);
    });
    pinLayer.appendChild(badge);
    const stackIndex = countPinsOnTarget(draft.target);
    state.pins.set(draft.tempId, { entry: draft, badgeEl: badge, target: draft.target, stackIndex });
    positionPin(draft.tempId);
  }

  function renderServerPin(entry) {
    if (entry.status === 'dismissed') return;
    const target = entry.selector ? document.querySelector(entry.selector) : null;
    if (!target) return;
    const badge = document.createElement('div');
    badge.className = 'mi-pin mi-pin-' + entry.status;
    badge.textContent = entry.id.replace('c', '');
    const tip = entry.appliedNote ? `✓ ${entry.appliedNote}` : entry.comment;
    badge.title = (entry.quickAction ? `[${entry.quickAction}] ` : '') + tip;
    pinLayer.appendChild(badge);
    const stackIndex = countPinsOnTarget(target);
    state.pins.set(entry.id, { entry, badgeEl: badge, target, stackIndex });
    positionPin(entry.id);
  }

  // Re-assign stack indices for all pins sharing this target (after a deletion).
  function repackTarget(target) {
    if (!target) return;
    let i = 0;
    state.pins.forEach((pin, id) => {
      if (pin.target === target) {
        pin.stackIndex = i++;
        positionPin(id);
      }
    });
  }

  // Pin position: anchored to target's top-right corner.
  // Multiple pins on the same target cascade horizontally with a slight overlap.
  function positionPin(id) {
    const pin = state.pins.get(id);
    if (!pin || !pin.target) return;
    const r = pin.target.getBoundingClientRect();
    const off = (pin.stackIndex || 0) * 22; // overlap to keep visually grouped
    pin.badgeEl.style.left = (r.right - 14 - off) + 'px';
    pin.badgeEl.style.top  = (r.top - 14) + 'px';
  }

  function repositionAll() {
    state.pins.forEach((_, id) => positionPin(id));
  }
  window.addEventListener('scroll', repositionAll, true);
  window.addEventListener('resize', repositionAll);

  /* ---------- Send batch ---------- */
  function updateSendButton() {
    const n = state.drafts.length;
    countEl.textContent = n;
    sendBtn.hidden = n === 0;
  }

  async function sendBatch() {
    if (!state.drafts.length) return;
    const payload = state.drafts.map(d => ({
      mode: d.mode,
      selector: d.selector,
      xpath: d.xpath,
      previewHTML: d.previewHTML,
      selectionText: d.selectionText,
      comment: d.comment,
      quickAction: d.quickAction,
      viewport: d.viewport,
      anchor: d.anchor,
    }));
    try {
      const res = await fetch('/api/comments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const json = await res.json();
      // upgrade drafts -> pending using returned ids in order
      state.drafts.forEach((d, i) => {
        const newId = json.ids[i];
        const pin = state.pins.get(d.tempId);
        if (pin && newId) {
          state.pins.delete(d.tempId);
          pin.entry.id = newId;
          pin.entry.status = 'pending';
          pin.badgeEl.className = 'mi-pin mi-pin-pending';
          pin.badgeEl.textContent = newId.replace('c', '');
          state.pins.set(newId, pin);
        }
      });
      state.drafts = [];
      updateSendButton();
      showToast(`Sent ${payload.length} — Claude is working…`);
    } catch (err) {
      showToast('Failed to send. Server reachable?');
    }
  }

  sendBtn.addEventListener('click', sendBatch);

  /* ---------- Load existing queue on boot ---------- */
  async function loadQueue() {
    try {
      const res = await fetch('/api/queue');
      const data = await res.json();
      (data.comments || []).forEach(renderServerPin);
    } catch (err) { /* ignore */ }
  }
  loadQueue();

  /* ---------- SSE: live reload ---------- */
  function connectSSE() {
    const es = new EventSource('/api/events');
    es.addEventListener('reload', () => {
      showToast('Updated by Claude — reloading…', 1200);
      setTimeout(() => window.location.reload(), 600);
    });
    es.onerror = () => {
      // browser auto-retries; do nothing
    };
  }
  connectSSE();

  /* ---------- Re-show toolbar after every reload ---------- */
  setMode('off');
})();
