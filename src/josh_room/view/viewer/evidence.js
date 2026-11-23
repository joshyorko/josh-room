// Inspector & Read-Only Content / Evidence Viewer:
// - Bounded sanitized typed Content (syntax-highlighted source/config/log, unified diff,
//   interactive collapsible JSON tree, structured execution run timeline & logs, explicit
//   REDACTED / OMITTED / UNAVAILABLE notices)
// - Clickable bidirectional Object Relationship Links (producer -> output -> consumer)
// - Lazy PDF.js, side-by-side image compare + <=1.25x lightbox, inert DOMParser HTML
// - Live-poll refresh preserving active tab and slot selection
(function (global) {
  'use strict';

  const MAX_CANVAS_DIM = 2048;
  const PY_KEYWORDS = new Set([
    'def', 'class', 'return', 'import', 'from', 'as', 'if', 'elif', 'else', 'for', 'in',
    'while', 'try', 'except', 'finally', 'with', 'raise', 'assert', 'pass', 'True', 'False',
    'None', 'and', 'or', 'not', 'is', 'lambda', 'yield',
  ]);
  const SH_KEYWORDS = new Set(['set', 'exec', 'if', 'then', 'else', 'fi', 'for', 'do', 'done', 'export', 'local']);

  function appendToken(parent, text, cls) {
    if (!text) return;
    if (!cls) {
      parent.appendChild(document.createTextNode(text));
      return;
    }
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = text;
    parent.appendChild(span);
  }

  function highlightCodeLine(parent, line, language) {
    const lang = (language || '').toLowerCase();
    const trimmed = line.trim();
    if (lang === 'log') {
      if (/\b(?:ERROR|FAIL)\b/.test(line)) {
        appendToken(parent, line, 'tok-log-err');
      } else if (/\bWARN(?:ING)?\b/.test(line)) {
        appendToken(parent, line, 'tok-log-warn');
      } else if (/\bPASS(?:ED)?\b/.test(line)) {
        appendToken(parent, line, 'tok-log-pass');
      } else {
        appendToken(parent, line, null);
      }
      return;
    }
    if (trimmed.startsWith('#')) {
      appendToken(parent, line, 'tok-cmt');
      return;
    }
    if (lang === 'ini' || lang === 'makefile') {
      if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
        appendToken(parent, line, 'tok-kw');
        return;
      }
      const eq = line.indexOf('=');
      if (eq > 0) {
        appendToken(parent, line.slice(0, eq), 'tok-key');
        appendToken(parent, '=', null);
        appendToken(parent, line.slice(eq + 1), 'tok-str');
        return;
      }
    }
    if (lang === 'markdown') {
      if (/^#+\s/.test(trimmed)) {
        appendToken(parent, line, 'tok-kw');
        return;
      }
      appendToken(parent, line, null);
      return;
    }
    // Lightweight zero-HTML tokenizer for python/shell/source
    const kwSet = lang === 'shell' ? SH_KEYWORDS : PY_KEYWORDS;
    const tokenRe = /("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#.*$|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g;
    let lastIdx = 0;
    let match;
    while ((match = tokenRe.exec(line)) !== null) {
      if (match.index > lastIdx) {
        appendToken(parent, line.slice(lastIdx, match.index), null);
      }
      const tok = match[0];
      if (tok.startsWith('#')) appendToken(parent, tok, 'tok-cmt');
      else if (tok.startsWith('"') || tok.startsWith("'")) appendToken(parent, tok, 'tok-str');
      else if (/^\d/.test(tok)) appendToken(parent, tok, 'tok-num');
      else if (kwSet.has(tok)) appendToken(parent, tok, 'tok-kw');
      else appendToken(parent, tok, null);
      lastIdx = tokenRe.lastIndex;
    }
    if (lastIdx < line.length) {
      appendToken(parent, line.slice(lastIdx), null);
    }
  }

  function renderCodeViewer(bodyText, { language = 'text', isDiff = false } = {}) {
    const wrap = document.createElement('div');
    wrap.className = 'code-viewer';
    const lines = String(bodyText || '').split('\n');
    lines.forEach((line, idx) => {
      const row = document.createElement('div');
      row.className = 'code-line';
      if (isDiff) {
        if (line.startsWith('+++') || line.startsWith('---')) row.classList.add('diff-meta');
        else if (line.startsWith('@@')) row.classList.add('diff-hunk');
        else if (line.startsWith('+')) row.classList.add('diff-add');
        else if (line.startsWith('-')) row.classList.add('diff-del');
      }
      const ln = document.createElement('span');
      ln.className = 'code-ln';
      ln.textContent = String(idx + 1);
      const src = document.createElement('span');
      src.className = 'code-src';
      if (isDiff) src.textContent = line;
      else highlightCodeLine(src, line, language);
      row.append(ln, src);
      wrap.appendChild(row);
    });
    return wrap;
  }

  function renderJsonNode(keyName, value, depth) {
    const isObj = value !== null && typeof value === 'object';
    if (!isObj) {
      const leaf = document.createElement('div');
      leaf.className = 'json-leaf';
      if (keyName !== null) {
        const k = document.createElement('span');
        k.className = 'json-key';
        k.textContent = JSON.stringify(keyName);
        leaf.append(k, document.createTextNode(': '));
      }
      const v = document.createElement('span');
      if (value === null) {
        v.className = 'json-null';
        v.textContent = 'null';
      } else if (typeof value === 'boolean') {
        v.className = 'json-bool';
        v.textContent = String(value);
      } else if (typeof value === 'number') {
        v.className = 'json-num';
        v.textContent = String(value);
      } else {
        v.className = 'json-str';
        v.textContent = JSON.stringify(value);
      }
      leaf.appendChild(v);
      return leaf;
    }

    const isArray = Array.isArray(value);
    const entries = isArray ? value.map((v, i) => [String(i), v]) : Object.entries(value);
    const details = document.createElement('details');
    details.open = depth < 2;

    const summary = document.createElement('summary');
    summary.className = 'json-summary';
    const caret = document.createElement('span');
    caret.className = 'json-caret';
    caret.textContent = details.open ? '▾' : '▸';
    details.addEventListener('toggle', () => {
      caret.textContent = details.open ? '▾' : '▸';
    });
    summary.appendChild(caret);
    if (keyName !== null) {
      const k = document.createElement('span');
      k.className = 'json-key';
      k.textContent = JSON.stringify(keyName);
      summary.append(k, document.createTextNode(': '));
    }
    const meta = document.createElement('span');
    meta.className = 'json-meta';
    meta.textContent = isArray ? `[ ${entries.length} items ]` : `{ ${entries.length} keys }`;
    summary.appendChild(meta);
    details.appendChild(summary);

    const branch = document.createElement('div');
    branch.className = 'json-branch';
    for (const [childKey, childVal] of entries) {
      branch.appendChild(renderJsonNode(isArray ? null : childKey, childVal, depth + 1));
    }
    details.appendChild(branch);
    return details;
  }

  function renderCollapsibleJson(bodyText) {
    const container = document.createElement('div');
    let parsed;
    try {
      parsed = JSON.parse(bodyText);
    } catch (_) {
      return renderCodeViewer(bodyText, { language: 'text' });
    }

    const toolbar = document.createElement('div');
    toolbar.className = 'json-toolbar';
    const btnExpand = document.createElement('button');
    btnExpand.type = 'button';
    btnExpand.className = 'hud-btn hud-sm';
    btnExpand.textContent = 'Expand all';
    const btnCollapse = document.createElement('button');
    btnCollapse.type = 'button';
    btnCollapse.className = 'hud-btn hud-sm';
    btnCollapse.textContent = 'Collapse all';
    toolbar.append(btnExpand, btnCollapse);

    const treeEl = document.createElement('div');
    treeEl.className = 'json-tree';
    treeEl.appendChild(renderJsonNode(null, parsed, 0));

    btnExpand.addEventListener('click', () => {
      treeEl.querySelectorAll('details').forEach((d) => { d.open = true; });
    });
    btnCollapse.addEventListener('click', () => {
      treeEl.querySelectorAll('details').forEach((d, idx) => { d.open = idx === 0; });
    });

    container.append(toolbar, treeEl);
    return container;
  }

  function renderExecutionRun(bodyText) {
    let run;
    try {
      run = JSON.parse(bodyText);
    } catch (_) {
      return renderCodeViewer(bodyText, { language: 'log' });
    }
    const card = document.createElement('div');
    card.className = 'run-card';

    const header = document.createElement('div');
    header.className = 'run-header';
    const left = document.createElement('strong');
    left.style.fontFamily = 'var(--font-mono)';
    left.textContent = run.run_id || 'Execution Run';
    const right = document.createElement('span');
    right.className = 'muted-text';
    right.style.fontFamily = 'var(--font-mono)';
    const exitStr = run.exit_code != null ? `exit ${run.exit_code}` : (run.status || '');
    const durStr = run.duration_ms != null ? ` · ${run.duration_ms} ms` : '';
    right.textContent = `${exitStr}${durStr}`;
    header.append(left, right);
    card.appendChild(header);

    if (run.command) {
      const cmd = document.createElement('div');
      cmd.className = 'run-cmd';
      cmd.textContent = `$ ${run.command}`;
      card.appendChild(cmd);
    }
    if (run.error) {
      const errBox = document.createElement('div');
      errBox.className = 'run-error-box';
      errBox.textContent = `ERROR: ${run.error}`;
      card.appendChild(errBox);
    }
    if (Array.isArray(run.steps) && run.steps.length) {
      const heading = document.createElement('div');
      heading.className = 'links-heading';
      heading.textContent = 'Execution Steps';
      card.appendChild(heading);
      for (const st of run.steps) {
        const isFail = st.status === 'fail' || st.status === 'failed' || st.status === 'error';
        const row = document.createElement('div');
        row.className = `run-step-row ${isFail ? 'run-step-fail' : 'run-step-pass'}`;
        const name = document.createElement('span');
        name.textContent = `${isFail ? '×' : '●'} ${st.name}`;
        const meta = document.createElement('span');
        meta.className = 'muted-text';
        meta.textContent = `${st.status}${st.duration_ms != null ? ` · ${st.duration_ms} ms` : ''}`;
        row.append(name, meta);
        card.appendChild(row);
      }
    }
    if (Array.isArray(run.logs) && run.logs.length) {
      const heading = document.createElement('div');
      heading.className = 'links-heading';
      heading.style.marginTop = '10px';
      heading.textContent = 'Sanitized Log Excerpt';
      card.appendChild(heading);
      card.appendChild(renderCodeViewer(run.logs.join('\n'), { language: 'log' }));
    }
    return card;
  }

  class EvidenceDrawer {
    constructor(dataLayer, loadPdfJsFn, callbacks) {
      this.data = dataLayer;
      this.loadPdfJs = loadPdfJsFn;
      this.callbacks = callbacks || {};
      this.drawerEl = document.getElementById('inspector-drawer');
      this.titleEl = document.getElementById('inspector-title');
      this.pathEl = document.getElementById('inspector-path');
      this.badgeEl = document.getElementById('inspector-kind-badge');
      this.contentCountBadgeEl = document.getElementById('content-count-badge');
      this.countBadgeEl = document.getElementById('evidence-count-badge');
      this.tabContentEl = document.getElementById('tab-content');
      this.tabDetailsEl = document.getElementById('tab-details');
      this.tabEvidenceEl = document.getElementById('tab-evidence');
      this.tabTimelineEl = document.getElementById('tab-timeline');
      this.comparison = new global.RoomSavedComparison(dataLayer, document.getElementById('tab-compare'),
        document.getElementById('btn-compare-tab'));
      this.lightboxEl = document.getElementById('lightbox-modal');
      this.lightboxImg = document.getElementById('lightbox-img');
      this.lightboxCaption = document.getElementById('lightbox-caption');

      this.currentItem = null;
      this.contentFingerprint = null;
      this.currentObjectId = null;
      this.activeTab = 'content';
      this.activePreviewSlot = null;
      this._attRequestSeq = 0;
      this._pdfObserver = null;
      this._pdfDoc = null;
      this._pdfScale = 1.0;
      this._pdfFitMode = 'width';
      this._pdfPagesRendered = 0;

      this._bindTabs();
      this._bindLightbox();
    }

    _bindTabs() {
      this.drawerEl.querySelectorAll('.tab-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
          this.switchTab(btn.getAttribute('data-tab') || 'content');
        });
      });
    }

    _bindLightbox() {
      const closeBtn = document.getElementById('btn-lightbox-close');
      if (closeBtn) {
        closeBtn.addEventListener('click', () => { this.lightboxEl.hidden = true; });
      }
      this.lightboxEl.addEventListener('click', (ev) => {
        if (ev.target === this.lightboxEl) this.lightboxEl.hidden = true;
      });
    }

    switchTab(tabName) {
      if (tabName === 'compare' && this.comparison.tab.hidden) tabName = 'content';
      if (tabName === 'compare') this.comparison.open();
      else this.comparison.close();
      this.activeTab = tabName;
      this.drawerEl.querySelectorAll('.tab-btn').forEach((btn) => {
        const active = btn.getAttribute('data-tab') === tabName;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      this.tabContentEl.hidden = tabName !== 'content';
      this.tabDetailsEl.hidden = tabName !== 'details';
      this.tabEvidenceEl.hidden = tabName !== 'evidence';
      this.tabTimelineEl.hidden = tabName !== 'timeline';
    }

    captureReadingPosition() {
      if (!this.isOpen() || !this.currentItem) return null;
      const viewport = this.tabEvidenceEl.querySelector('.pdf-viewport');
      const page = viewport && Array.from(viewport.querySelectorAll('.pdf-page-slot')).find(
        slot => slot.getBoundingClientRect().bottom > viewport.getBoundingClientRect().top);
      return {
        id: this.currentObjectId, tab: this.activeTab, slot: this.activePreviewSlot,
        digest: this._contentDigest(this.currentItem),
        attachment: this.tabEvidenceEl.querySelector('.att-pill.active')?.getAttribute('data-attachment-name'),
        page: Number(page?.dataset.pageNum) || 1, fit: this._pdfFitMode, scale: this._pdfScale,
        top: this.tabContentEl.scrollTop, detailsTop: this.tabDetailsEl.scrollTop,
      };
    }

    async _contentDigest(target) {
      const bytes = new TextEncoder().encode(JSON.stringify([target.previews || [], target.attachments || []]));
      const hash = await crypto.subtle.digest('SHA-256', bytes);
      return Array.from(new Uint8Array(hash)).join(',');
    }

    async readingUnchanged(target, position) {
      return target.objectId === position.id && await position.digest === await this._contentDigest(target);
    }

    hide() {
      this.comparison.select(null);
      this._attRequestSeq++;
      this._teardownPdf();
      this.lightboxEl.hidden = true;
      this.lightboxImg.removeAttribute('src');
      this.lightboxCaption.textContent = '';
      for (const el of [this.tabContentEl, this.tabDetailsEl, this.tabEvidenceEl, this.tabTimelineEl, this.titleEl, this.pathEl]) {
        el.textContent = '';
      }
      this.currentItem = null;
      this.contentFingerprint = null;
      this.currentObjectId = null;
      this.activePreviewSlot = null;
      this.drawerEl.hidden = true;
    }

    isOpen() {
      return !this.drawerEl.hidden;
    }

    showTarget(target, descriptor, state, layout, { quantileBin = null, preserveTab = false, reading = null, deferContent = false } = {}) {
      this.comparison.select(target);
      if (this.activeTab === 'compare' && !this.comparison.active) this.switchTab('content');
      const sameObject = this.currentObjectId === (target.objectId || target.id);
      const fingerprint = JSON.stringify([target.previews || [], target.attachments || []]);
      if (preserveTab && sameObject && fingerprint === this.contentFingerprint) {
        this.currentItem = target;
        this._renderDetails(target, descriptor, layout, quantileBin);
        return;
      }
      const changed = preserveTab && sameObject && this.contentFingerprint !== fingerprint;
      this._attRequestSeq++;
      this._teardownPdf();
      this.lightboxEl.hidden = true;
      this.lightboxImg.removeAttribute('src');
      this.contentFingerprint = fingerprint;
      if (!sameObject) this.activePreviewSlot = null;
      this.currentItem = target;
      this.currentObjectId = target.objectId || target.id;
      this.drawerEl.hidden = false;

      const entry = global.RoomViewLayout.getLegendEntry(descriptor, target.kind);
      this.badgeEl.textContent = target.nodeKind === 'directory' ? 'Directory' : `${entry.symbol} ${entry.label}`;
      this.badgeEl.style.color = entry.color;
      this.badgeEl.style.borderColor = entry.color;
      this.badgeEl.style.borderStyle = entry.shape === 'hollow' ? 'dashed' : 'solid';
      this.badgeEl.style.background = `${entry.color}26`;

      this.titleEl.textContent = target.label;
      this.pathEl.textContent = target.path || `/${target.group}/${target.stage}/${target.objectId || target.id}`;
      const rawPreviews = target.previews || [];
      const slotPriority = (slot) => {
        const order = { execution: 0, source: 1, config: 2, json: 3, log: 4, diff: 5 };
        return slot in order ? order[slot] : 10;
      };
      const previews = rawPreviews.slice().sort((a, b) => slotPriority(a.slot) - slotPriority(b.slot) || a.slot.localeCompare(b.slot));
      const atts = target.attachments || [];
      if (this.contentCountBadgeEl) this.contentCountBadgeEl.textContent = String(previews.length);
      this.countBadgeEl.textContent = String(atts.length);

      if (changed || deferContent) {
        this.tabContentEl.textContent = '';
        this.tabEvidenceEl.textContent = '';
        const reopen = document.createElement('button');
        reopen.type = 'button';
        reopen.textContent = 'Content changed. Open current version';
        reopen.addEventListener('click', () => this.showTarget(this.currentItem, descriptor, state, layout, { quantileBin }));
        this.tabContentEl.append(reopen);
        this._renderDetails(target, descriptor, layout, quantileBin);
        this._renderTimeline(target, state);
        this.switchTab('content');
        return;
      }
      if (reading) this.activePreviewSlot = reading.slot;
      this._renderContent(target, descriptor, layout);
      this._renderDetails(target, descriptor, layout, quantileBin);
      this._renderTimeline(target, state);
      this._renderEvidence(target, reading);

      if (reading) {
        this.switchTab(reading.tab);
        this.tabContentEl.scrollTop = reading.top;
        this.tabDetailsEl.scrollTop = reading.detailsTop;
      } else if (preserveTab) {
        this.switchTab(this.activeTab);
      } else if (previews.length > 0) {
        this.switchTab('content');
      } else if (atts.length > 0) {
        this.switchTab('evidence');
      } else {
        this.switchTab('details');
      }
    }

    // Backwards-compatible alias
    showItem(item, descriptor, state, quantileBin, layout) {
      this.showTarget(item, descriptor, state, layout || null, { quantileBin, preserveTab: false });
    }

    _buildLinksSection(target, layout) {
      const objId = target.objectId || target.id;
      const outgoing = target.links || [];
      const incoming = (layout && layout.incomingLinksByObjectId && layout.incomingLinksByObjectId.get(objId)) || [];
      if (!outgoing.length && !incoming.length) return null;

      const section = document.createElement('div');
      section.className = 'links-section';

      const heading = document.createElement('div');
      heading.className = 'links-heading';
      heading.textContent = 'Object Relationships (Click to Fly)';
      section.appendChild(heading);

      const list = document.createElement('div');
      list.className = 'link-chip-list';

      const resolveLabelAndPath = (targetId) => {
        if (layout && layout.allLeavesByObjectId && layout.allLeavesByObjectId.has(targetId)) {
          const t = layout.allLeavesByObjectId.get(targetId);
          return { path: t.path, label: t.label };
        }
        return { path: targetId, label: targetId };
      };

      for (const lk of outgoing) {
        const info = resolveLabelAndPath(lk.target_id);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'link-chip';
        btn.setAttribute('data-link-target', lk.target_id);
        btn.setAttribute('data-link-relation', lk.relation);

        const rel = document.createElement('span');
        rel.className = 'link-rel';
        rel.textContent = `${lk.relation} →`;
        const pathSpan = document.createElement('span');
        pathSpan.className = 'link-target-path';
        pathSpan.textContent = info.path;
        btn.append(rel, pathSpan);
        if (lk.label) {
          const desc = document.createElement('span');
          desc.className = 'link-desc';
          desc.textContent = lk.label;
          btn.appendChild(desc);
        }
        btn.addEventListener('click', () => {
          if (this.callbacks.onJumpToObject) this.callbacks.onJumpToObject(lk.target_id);
        });
        list.appendChild(btn);
      }

      for (const back of incoming) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'link-chip';
        btn.setAttribute('data-link-target', back.source_id);
        btn.setAttribute('data-link-relation', `incoming:${back.relation}`);

        const rel = document.createElement('span');
        rel.className = 'link-rel';
        rel.textContent = `← ${back.relation}`;
        const pathSpan = document.createElement('span');
        pathSpan.className = 'link-target-path';
        pathSpan.textContent = back.source_path;
        btn.append(rel, pathSpan);
        if (back.label) {
          const desc = document.createElement('span');
          desc.className = 'link-desc';
          desc.textContent = back.label;
          btn.appendChild(desc);
        }
        btn.addEventListener('click', () => {
          if (this.callbacks.onJumpToObject) this.callbacks.onJumpToObject(back.source_id);
        });
        list.appendChild(btn);
      }

      section.appendChild(list);
      return section;
    }

    _renderContent(target, descriptor, layout) {
      this.tabContentEl.textContent = '';
      if (target.summary) {
        const sumEl = document.createElement('p');
        sumEl.className = 'drawer-summary-banner';
        sumEl.textContent = target.summary;
        this.tabContentEl.appendChild(sumEl);
      }

      const rawPreviews = target.previews || [];
      const slotPriority = (slot) => {
        const order = { execution: 0, source: 1, config: 2, json: 3, log: 4, diff: 5 };
        return slot in order ? order[slot] : 10;
      };
      const previews = rawPreviews.slice().sort((a, b) => slotPriority(a.slot) - slotPriority(b.slot) || a.slot.localeCompare(b.slot));
      if (!previews.length) {
        const box = document.createElement('div');
        box.className = 'unavail-card';
        const badge = document.createElement('div');
        badge.className = 'unavail-badge';
        badge.textContent = 'NO INLINE CONTENT PREVIEW';
        const p = document.createElement('p');
        p.className = 'muted-text';
        const atts = target.attachments || [];
        p.textContent = atts.length
          ? `This object has ${atts.length} binary evidence file${atts.length > 1 ? 's' : ''} (PDF / image / HTML). Switch to the Evidence tab to inspect.`
          : 'No read-only preview slot was supplied by the provider for this object.';
        box.append(badge, p);
        if (atts.length) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'hud-btn hud-btn-accent';
          btn.textContent = `Open Evidence (${atts.length})`;
          btn.addEventListener('click', () => this.switchTab('evidence'));
          box.appendChild(btn);
        }
        this.tabContentEl.appendChild(box);
      } else {
        const slotBar = document.createElement('div');
        slotBar.className = 'preview-slot-bar';
        const stageEl = document.createElement('div');
        stageEl.className = 'preview-stage';
        this.tabContentEl.append(slotBar, stageEl);

        const initialSlot = (
          this.activePreviewSlot && previews.some((p) => p.slot === this.activePreviewSlot)
            ? this.activePreviewSlot
            : previews[0].slot
        );
        const buttons = [];

        const renderSlot = (prev, btn) => {
          this.activePreviewSlot = prev.slot;
          buttons.forEach((b) => b.classList.remove('active'));
          if (btn) btn.classList.add('active');
          stageEl.textContent = '';

          const hdr = document.createElement('div');
          hdr.className = 'preview-header-row';
          const left = document.createElement('span');
          left.textContent = `${prev.title} · ${prev.media_type}`;
          const right = document.createElement('span');
          right.textContent = prev.truncated ? 'BOUNDED EXCERPT' : (prev.status || 'available').toUpperCase();
          hdr.append(left, right);
          stageEl.appendChild(hdr);

          if (prev.status && prev.status !== 'available') {
            const card = document.createElement('div');
            card.className = 'unavail-card';
            card.setAttribute('data-preview-status', prev.status);
            const badge = document.createElement('div');
            badge.className = 'unavail-badge';
            badge.textContent = `CONTENT ${prev.status.toUpperCase()}`;
            const p = document.createElement('p');
            p.className = 'muted-text';
            p.textContent = prev.reason || `Content is ${prev.status} and was intentionally not embedded.`;
            card.append(badge, p);
            stageEl.appendChild(card);
            return;
          }

          if (prev.media_type === 'application/x-josh-run+json') {
            stageEl.appendChild(renderExecutionRun(prev.body || '{}'));
          } else if (prev.media_type === 'application/json') {
            stageEl.appendChild(renderCollapsibleJson(prev.body || '{}'));
          } else if (prev.media_type === 'text/x-diff') {
            stageEl.appendChild(renderCodeViewer(prev.body || '', { language: 'diff', isDiff: true }));
          } else {
            stageEl.appendChild(renderCodeViewer(prev.body || '', { language: prev.language || 'text', isDiff: false }));
          }
        };

        for (const prev of previews) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'att-pill';
          btn.setAttribute('data-preview-slot', prev.slot);
          btn.textContent = prev.slot;
          btn.addEventListener('click', () => renderSlot(prev, btn));
          buttons.push(btn);
          slotBar.appendChild(btn);
          if (prev.slot === initialSlot) renderSlot(prev, btn);
        }
      }

      const linksSection = this._buildLinksSection(target, layout);
      if (linksSection) this.tabContentEl.appendChild(linksSection);
    }

    _renderDetails(target, descriptor, layout, quantileBin) {
      const unit = descriptor.metric && descriptor.metric.unit ? ` ${descriptor.metric.unit}` : '';
      const metricName = descriptor.metric ? descriptor.metric.name : 'metric';
      const entry = global.RoomViewLayout.getLegendEntry(descriptor, target.kind);
      const rows = [
        ['ID', target.objectId || target.id],
        ['Path', target.path || '—'],
        ['Type', target.nodeKind || 'file'],
        [global.RoomViewLayout.isWorkspace(descriptor) ? 'Kind' : 'Outcome', target.nodeKind === 'directory' ? 'Directory' : `${entry.symbol} ${entry.label}`],
        [metricName, target.metric != null ? `${target.metric}${unit}${quantileBin ? ` (Bin ${quantileBin}/5)` : ''}` : '—'],
        ['Updated', target.updated_at || '—'],
      ];
      if (target.group) rows.push([descriptor.group_label || 'Group', target.group]);
      if (target.stage) rows.push(['Stage', target.stage]);
      for (const [k, v] of Object.entries(target.fields || {})) {
        rows.push([k, v === null ? 'null' : String(v)]);
      }

      const grid = document.createElement('div');
      grid.className = 'kv-grid';
      for (const [k, v] of rows) {
        const dk = document.createElement('div');
        dk.className = 'kv-key';
        dk.textContent = k;
        const dv = document.createElement('div');
        dv.className = 'kv-val';
        dv.textContent = v;
        grid.append(dk, dv);
      }

      this.tabDetailsEl.textContent = '';
      if (target.summary) {
        const sumEl = document.createElement('p');
        sumEl.className = 'drawer-summary-banner';
        sumEl.textContent = target.summary;
        this.tabDetailsEl.appendChild(sumEl);
      }
      this.tabDetailsEl.appendChild(grid);

      const linksSection = this._buildLinksSection(target, layout);
      if (linksSection) this.tabDetailsEl.appendChild(linksSection);

      if ((target.attachments || []).length > 0) {
        const hintBtn = document.createElement('button');
        hintBtn.type = 'button';
        hintBtn.className = 'hud-btn hud-btn-accent';
        hintBtn.style.marginTop = '12px';
        hintBtn.textContent = `Open ${target.attachments.length} attached evidence file${target.attachments.length > 1 ? 's' : ''}`;
        hintBtn.addEventListener('click', () => this.switchTab('evidence'));
        this.tabDetailsEl.appendChild(hintBtn);
      }
    }

    _renderTimeline(target, state) {
      const objId = target.objectId || target.id;
      const steps = (state.steps || []).filter((s) => s.item_id === objId);
      this.tabTimelineEl.textContent = '';
      if (!steps.length) {
        const p = document.createElement('p');
        p.className = 'muted-text';
        p.textContent = 'No lineage steps recorded for this object.';
        this.tabTimelineEl.appendChild(p);
        return;
      }
      const ul = document.createElement('ul');
      ul.className = 'timeline-list';
      for (const step of steps) {
        const li = document.createElement('li');
        const strong = document.createElement('strong');
        strong.textContent = step.name;
        const ts = document.createElement('div');
        ts.className = 'muted-text';
        ts.style.fontFamily = 'var(--font-mono)';
        ts.textContent = step.at;
        li.append(strong, ts);
        ul.appendChild(li);
      }
      this.tabTimelineEl.appendChild(ul);
    }

    _renderEvidence(target, reading) {
      this.tabEvidenceEl.textContent = '';
      const objId = target.objectId || target.id;
      const atts = target.attachments || [];
      if (!atts.length) {
        const p = document.createElement('p');
        p.className = 'muted-text';
        p.textContent = 'No binary evidence attached to this object.';
        this.tabEvidenceEl.appendChild(p);
        return;
      }

      const listEl = document.createElement('div');
      listEl.className = 'att-list';
      const stageEl = document.createElement('div');
      stageEl.className = 'att-stage';
      this.tabEvidenceEl.append(listEl, stageEl);

      const orderScore = (mt) => (mt === 'application/pdf' ? 0 : (mt.startsWith('image/') ? 1 : (mt === 'text/html' ? 2 : 3)));
      const sortedAtts = atts.slice().sort((a, b) => orderScore(a.media_type) - orderScore(b.media_type) || a.name.localeCompare(b.name));
      const images = sortedAtts.filter((a) => a.media_type.startsWith('image/'));
      const buttons = [];

      const initial = sortedAtts.find(att => att.name === reading?.attachment) || sortedAtts[0];
      const selectAtt = (att, btn, position) => {
        buttons.forEach((b) => b.classList.remove('active'));
        if (btn) btn.classList.add('active');
        this._attRequestSeq += 1;
        const reqSeq = this._attRequestSeq;
        this._teardownPdf();
        stageEl.textContent = '';
        if (att.media_type === 'application/pdf') {
          this._openPdf(objId, att, stageEl, reqSeq, position);
        } else if (att.media_type.startsWith('image/')) {
          this._openImages(objId, images.length >= 2 ? images : [att], stageEl, reqSeq);
        } else if (att.media_type === 'text/html') {
          this._openInertHtml(objId, att, stageEl, reqSeq);
        } else {
          this._renderUnavailable(
            stageEl,
            'disallowed-media-type',
            `${att.name} (${att.media_type}, ${att.size} bytes) is listed as metadata only and is not embedded under allowlist rules.`
          );
        }
      };

      sortedAtts.forEach((att) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'att-pill';
        btn.setAttribute('data-attachment-name', att.name);
        btn.textContent = `${att.name} (${Math.max(1, Math.round(att.size / 1024))} KB)`;
        btn.addEventListener('click', () => selectAtt(att, btn));
        buttons.push(btn);
        listEl.appendChild(btn);
        if (att === initial) selectAtt(att, btn, reading);
      });
    }

    _renderUnavailable(container, reasonCode, detailText) {
      container.textContent = '';
      const box = document.createElement('div');
      box.className = 'inert-report';
      const badge = document.createElement('div');
      badge.className = 'inert-badge';
      badge.textContent = `CONTENT UNAVAILABLE (${reasonCode})`;
      const p = document.createElement('p');
      p.className = 'muted-text';
      p.textContent = detailText;
      box.append(badge, p);
      container.appendChild(box);
    }

    _teardownPdf() {
      if (this._pdfObserver) {
        this._pdfObserver.disconnect();
        this._pdfObserver = null;
      }
      if (this._pdfDoc) {
        try { this._pdfDoc.destroy(); } catch (_) {}
        this._pdfDoc = null;
      }
    }

    async _openPdf(itemId, att, container, reqSeq, reading) {
      const startMs = performance.now();
      const status = document.createElement('div');
      status.className = 'muted-text';
      status.textContent = `Loading ${att.name}…`;
      container.appendChild(status);

      try {
        const [pdfjsLib, payload] = await Promise.all([
          this.loadPdfJs(),
          this.data.loadAttachmentBytes(itemId, att.name),
        ]);
        if (reqSeq !== this._attRequestSeq) return;
        if (!payload || !payload.bytes) {
          this._renderUnavailable(
            container,
            'payload-omitted',
            `${att.name} payload is not embedded in this snapshot (for example, exported with --no-attachments or unavailable from provider).`
          );
          return;
        }
        const loadingTask = pdfjsLib.getDocument({
          data: payload.bytes,
          useSystemFonts: true,
          isEvalSupported: false,
        });
        const pdfDoc = await loadingTask.promise;
        if (reqSeq !== this._attRequestSeq) {
          try { pdfDoc.destroy(); } catch (_) {}
          return;
        }
        this._pdfDoc = pdfDoc;
        this._pdfFitMode = reading?.fit || 'width';
        this._pdfScale = reading?.scale || 1;

        container.textContent = '';

        const controls = document.createElement('div');
        controls.className = 'pdf-controls';
        const btnFitW = document.createElement('button');
        btnFitW.type = 'button';
        btnFitW.className = 'hud-btn hud-sm';
        btnFitW.textContent = 'Fit Width';
        const btnFitP = document.createElement('button');
        btnFitP.type = 'button';
        btnFitP.className = 'hud-btn hud-sm';
        btnFitP.textContent = 'Fit Page';
        const btnZoomOut = document.createElement('button');
        btnZoomOut.type = 'button';
        btnZoomOut.className = 'hud-btn hud-sm';
        btnZoomOut.textContent = '−';
        const btnZoomIn = document.createElement('button');
        btnZoomIn.type = 'button';
        btnZoomIn.className = 'hud-btn hud-sm';
        btnZoomIn.textContent = '+';
        const info = document.createElement('span');
        info.className = 'muted-text';
        info.style.marginLeft = 'auto';
        info.textContent = `${pdfDoc.numPages} pages`;
        controls.append(btnFitW, btnFitP, btnZoomOut, btnZoomIn, info);

        const viewportEl = document.createElement('div');
        viewportEl.className = 'pdf-viewport';
        viewportEl.tabIndex = 0;
        viewportEl.setAttribute('aria-label', `PDF document ${att.name}`);
        container.append(controls, viewportEl);

        const slots = [];
        for (let p = 1; p <= pdfDoc.numPages; p += 1) {
          const slot = document.createElement('div');
          slot.className = 'pdf-page-slot';
          slot.dataset.pageNum = String(p);
          slot.style.minHeight = '320px';
          const lbl = document.createElement('div');
          lbl.className = 'pdf-page-label';
          lbl.textContent = `Page ${p} / ${pdfDoc.numPages}`;
          slot.appendChild(lbl);
          viewportEl.appendChild(slot);
          slots.push(slot);
        }

        const renderPageIntoSlot = async (slot) => {
          const pageNum = Number(slot.dataset.pageNum);
          if (slot.dataset.rendered === '1' || !this._pdfDoc) return;
          slot.dataset.rendered = '1';
          const page = await pdfDoc.getPage(pageNum);
          const unscaled = page.getViewport({ scale: 1.0 });
          const availW = Math.max(220, viewportEl.clientWidth - 28);
          const availH = Math.max(260, viewportEl.clientHeight - 28);
          const baseScale = this._pdfFitMode === 'page'
            ? Math.min(availW / unscaled.width, availH / unscaled.height)
            : (availW / unscaled.width);
          const dpr = Math.min(window.devicePixelRatio || 1, 2);
          let targetScale = baseScale * this._pdfScale;
          const maxScaleByDim = Math.min(
            MAX_CANVAS_DIM / (unscaled.width * dpr),
            MAX_CANVAS_DIM / (unscaled.height * dpr)
          );
          targetScale = Math.min(targetScale, maxScaleByDim);

          const vp = page.getViewport({ scale: targetScale * dpr });
          const canvas = document.createElement('canvas');
          canvas.width = Math.max(1, Math.floor(vp.width));
          canvas.height = Math.max(1, Math.floor(vp.height));
          canvas.style.width = `${Math.round(vp.width / dpr)}px`;
          canvas.style.height = `${Math.round(vp.height / dpr)}px`;
          const ctx = canvas.getContext('2d');
          await page.render({ canvasContext: ctx, viewport: vp }).promise;
          if (slot.dataset.rendered !== '1') return;
          slot.insertBefore(canvas, slot.firstChild);
          slot.style.minHeight = `${Math.round(vp.height / dpr) + 24}px`;
          this._pdfPagesRendered += 1;
          if (pageNum === 1) {
            const ms = Math.round((performance.now() - startMs) * 10) / 10;
            info.textContent = `${pdfDoc.numPages} pages · p1 in ${ms}ms`;
            info.dataset.firstPageMs = String(ms);
          }
        };

        const releaseOffscreenSlot = (slot) => {
          const canvas = slot.querySelector('canvas');
          if (canvas) {
            canvas.width = 0;
            canvas.height = 0;
            canvas.remove();
          }
          slot.dataset.rendered = '0';
        };

        const rerenderVisible = () => {
          const ratioY = viewportEl.scrollHeight > 0 ? viewportEl.scrollTop / viewportEl.scrollHeight : 0;
          slots.forEach(releaseOffscreenSlot);
          if (this._pdfObserver) {
            this._pdfObserver.disconnect();
            slots.forEach((s) => this._pdfObserver.observe(s));
          }
          requestAnimationFrame(() => {
            viewportEl.scrollTop = Math.round(ratioY * viewportEl.scrollHeight);
          });
        };

        this._pdfObserver = new IntersectionObserver(
          (entries) => {
            for (const entry of entries) {
              if (entry.isIntersecting) renderPageIntoSlot(entry.target);
              else releaseOffscreenSlot(entry.target);
            }
          },
          { root: viewportEl, rootMargin: '120px 0px' }
        );
        slots.forEach((s) => this._pdfObserver.observe(s));
        const landing = slots[(reading?.page || 1) - 1] || slots[0];
        await renderPageIntoSlot(landing);
        if (reqSeq !== this._attRequestSeq) return;
        if (reading) viewportEl.scrollTop = landing.offsetTop - slots[0].offsetTop;

        btnFitW.addEventListener('click', () => { this._pdfFitMode = 'width'; this._pdfScale = 1.0; rerenderVisible(); });
        btnFitP.addEventListener('click', () => { this._pdfFitMode = 'page'; this._pdfScale = 1.0; rerenderVisible(); });
        btnZoomIn.addEventListener('click', () => { this._pdfScale = Math.min(3.0, this._pdfScale * 1.2); rerenderVisible(); });
        btnZoomOut.addEventListener('click', () => { this._pdfScale = Math.max(0.5, this._pdfScale / 1.2); rerenderVisible(); });

        viewportEl.addEventListener('wheel', (ev) => {
          if (!ev.ctrlKey) return;
          ev.preventDefault();
          this._pdfScale = ev.deltaY < 0
            ? Math.min(3.0, this._pdfScale * 1.15)
            : Math.max(0.5, this._pdfScale / 1.15);
          rerenderVisible();
        }, { passive: false });

        let panActive = false;
        let panX = 0;
        let panY = 0;
        viewportEl.addEventListener('pointerdown', (ev) => {
          panActive = true;
          panX = ev.clientX;
          panY = ev.clientY;
        });
        viewportEl.addEventListener('pointermove', (ev) => {
          if (!panActive) return;
          viewportEl.scrollLeft -= ev.clientX - panX;
          viewportEl.scrollTop -= ev.clientY - panY;
          panX = ev.clientX;
          panY = ev.clientY;
        });
        window.addEventListener('pointerup', () => { panActive = false; });

        viewportEl.addEventListener('keydown', (ev) => {
          if (ev.key === 'PageDown') { ev.preventDefault(); viewportEl.scrollBy(0, viewportEl.clientHeight * 0.85); }
          else if (ev.key === 'PageUp') { ev.preventDefault(); viewportEl.scrollBy(0, -viewportEl.clientHeight * 0.85); }
        });
      } catch (err) {
        status.textContent = `Could not render PDF: ${err && err.message ? err.message : 'unknown error'}`;
      }
    }

    async _openImages(itemId, imageAtts, container, reqSeq) {
      const grid = document.createElement('div');
      grid.className = 'img-compare-grid';
      container.appendChild(grid);

      for (const att of imageAtts) {
        const entry = await this.data.getAttachmentBlobUrl(itemId, att.name);
        if (reqSeq !== this._attRequestSeq) return;
        const card = document.createElement('div');
        card.className = 'img-card';
        const caption = document.createElement('div');
        caption.className = 'muted-text';
        caption.style.marginBottom = '6px';
        caption.textContent = att.name;
        card.appendChild(caption);

        if (!entry) {
          const msg = document.createElement('div');
          msg.className = 'muted-text';
          msg.textContent = 'Image payload unavailable';
          card.appendChild(msg);
        } else {
          const img = document.createElement('img');
          img.src = entry.url;
          img.alt = att.name;
          img.addEventListener('load', () => {
            img.style.maxWidth = `${Math.round(img.naturalWidth * 1.25)}px`;
          });
          img.addEventListener('click', () => {
            this.lightboxCaption.textContent = `${itemId} / ${att.name}`;
            this.lightboxImg.src = entry.url;
            this.lightboxImg.onload = () => {
              this.lightboxImg.style.maxWidth = `${Math.round(this.lightboxImg.naturalWidth * 1.25)}px`;
              this.lightboxImg.style.maxHeight = `${Math.round(this.lightboxImg.naturalHeight * 1.25)}px`;
            };
            this.lightboxEl.hidden = false;
          });
          card.appendChild(img);
        }
        grid.appendChild(card);
      }
    }

    async _openInertHtml(itemId, att, container, reqSeq) {
      const payload = await this.data.loadAttachmentBytes(itemId, att.name);
      if (reqSeq !== this._attRequestSeq) return;
      if (!payload || !payload.bytes) {
        this._renderUnavailable(
          container,
          'payload-omitted',
          `${att.name} HTML report payload is not embedded in this snapshot.`
        );
        return;
      }
      const rawHtml = new TextDecoder('utf-8').decode(payload.bytes);
      const doc = new DOMParser().parseFromString(rawHtml, 'text/html');
      const strippedScripts = doc.querySelectorAll('script, iframe, object, embed').length;

      const wrap = document.createElement('div');
      wrap.className = 'inert-report';

      const badge = document.createElement('div');
      badge.className = 'inert-badge';
      badge.textContent = `INERT PARSED REPORT (scripts blocked: ${strippedScripts})`;
      wrap.appendChild(badge);

      const headings = doc.querySelectorAll('h1, h2, h3');
      if (headings.length) {
        const h = document.createElement('h3');
        h.style.margin = '4px 0 8px';
        h.textContent = headings[0].textContent.trim();
        wrap.appendChild(h);
      }

      doc.querySelectorAll('p').forEach((pEl) => {
        const text = pEl.textContent.trim();
        if (!text) return;
        const p = document.createElement('p');
        p.className = 'muted-text';
        p.textContent = text;
        wrap.appendChild(p);
      });

      doc.querySelectorAll('table').forEach((srcTable) => {
        const table = document.createElement('table');
        table.className = 'inert-table';
        srcTable.querySelectorAll('tr').forEach((srcRow, rowIdx) => {
          const tr = document.createElement('tr');
          const cells = srcRow.querySelectorAll('th, td');
          cells.forEach((srcCell) => {
            const cell = document.createElement(srcCell.tagName.toLowerCase() === 'th' || rowIdx === 0 ? 'th' : 'td');
            cell.textContent = srcCell.textContent.trim();
            tr.appendChild(cell);
          });
          table.appendChild(tr);
        });
        wrap.appendChild(table);
      });

      container.appendChild(wrap);
    }
  }

  global.RoomViewEvidence = { EvidenceDrawer };
})(window);
