// Main shared Room View controller (used identically by live localhost and offline single-file HTML).
(function (global) {
  'use strict';

  class RoomViewApp {
    constructor({ THREE, dataLayer, loadPdfJs }) {
      this.THREE = THREE;
      this.data = dataLayer;
      this.loadPdfJs = loadPdfJs;

      const params = new URLSearchParams(window.location.search);
      this.theme = params.get('theme') === 'grass' ? 'grass' : 'night';
      this.spotlightMode = params.get('spotlight') === 'baseline' ? 'baseline' : 'v2';
      this.viewMode = '3d';
      this.reducedMotion = Boolean(
        params.get('reduced_motion') === '1' ||
        (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
      );

      this.expandedGroups = new Set();
      this.expandedPedestals = new Set();
      this.history = [];
      this.currentNodeId = 'root';
      this.failureIndex = -1;
      this.webglActive = false;
      this.scene3d = null;

      this.tooltipEl = document.getElementById('hover-tooltip');
      this.gridViewEl = document.getElementById('grid-view');
      this.sqlViewEl = document.getElementById('sql-view');
      this.treePanelEl = document.getElementById('tree-panel');
      this.searchInputEl = document.getElementById('search-input');
      this.searchBarEl = document.getElementById('search-results-bar');

      this.evidence = new global.RoomViewEvidence.EvidenceDrawer(dataLayer, loadPdfJs);
      this.sqlConsole = new global.RoomViewSql.SqlConsoleView(dataLayer);
      this.gridView = new global.RoomViewGrid.GridFallbackView(
        document.getElementById('grid-content'),
        document.getElementById('grid-summary-stats'),
        document.getElementById('print-grid-table'),
        { onSelectItem: (itemId) => this.selectItemById(itemId, { openInspector: true }) }
      );
      this.ariaTree = new global.RoomViewAriaTree.AccessibleTreeTwin(
        document.getElementById('aria-tree-root'),
        document.getElementById('aria-live-region'),
        {
          onSelectNode: (nodeId) => this.handleNodeSelect(nodeId, { fromTree: true }),
          onExpandMore: (pedId) => this.expandPedestal(pedId),
        }
      );

      this._init3DOrFallback();
      this._bindHud();
      this._bindKeyboardMatrix();
      this._applyTheme(this.theme);
      this._syncFromData({ isInitial: true });
      this._exposeDebugHandle();

      if (this.data.mode === 'live') {
        this._startLivePolling();
      }
    }

    _init3DOrFallback() {
      const descriptorWantsGrid = this.data.descriptor && this.data.descriptor.view === 'grid';
      const canvas = document.getElementById('fsn-canvas');
      if (!this.THREE || !canvas) {
        this._activateWebglFallback();
        return;
      }
      try {
        this.scene3d = new global.RoomViewScene3D.FsnScene3D(this.THREE, canvas, {
          onSelectNode: (nodeId, opts) => this.handleNodeSelect(nodeId, opts || {}),
          onHover: (node, x, y) => this._updateHoverTooltip(node, x, y),
          onFirstFrame: () => this._updateTelemetry(),
        });
        this.scene3d.setReducedMotion(this.reducedMotion);
        this.scene3d.setSpotlightMode(this.spotlightMode);
        this.webglActive = true;
        if (descriptorWantsGrid) {
          this.setViewMode('grid');
        }
      } catch (_) {
        this.scene3d = null;
        this._activateWebglFallback();
      }
    }

    _activateWebglFallback() {
      this.webglActive = false;
      const notice = document.getElementById('webgl-fallback-notice');
      if (notice) notice.hidden = false;
      const btn3d = document.getElementById('btn-mode-3d');
      if (btn3d) btn3d.disabled = true;
      this.setViewMode('grid');
    }

    _syncFromData({ isInitial = false } = {}) {
      const desc = this.data.descriptor;
      const state = this.data.state;
      const sens = this.data.sensitivity;

      document.title = `${desc.title || 'Josh Room View'} — Room View`;

      // Mode & sensitivity badges/banners
      const modeBadge = document.getElementById('badge-mode');
      modeBadge.textContent = this.data.mode === 'live' ? 'LIVE' : 'OFFLINE';

      const sensBadge = document.getElementById('badge-sensitivity');
      sensBadge.textContent = (sens.level || 'synthetic').toUpperCase();
      sensBadge.classList.toggle('sensitive', Boolean(sens.requires_acknowledgment));

      const sensBanner = document.getElementById('sensitivity-banner');
      if (sens.requires_acknowledgment) {
        sensBanner.textContent = sens.banner_text || `Contains ${sens.level} data; keep on approved storage`;
        sensBanner.hidden = false;
      } else {
        sensBanner.hidden = true;
      }

      const staleBanner = document.getElementById('stale-banner');
      if (state.stale) {
        staleBanner.textContent = `STALE DATA (${state.stale.reason})${state.stale.last_ok ? ` — last fresh poll: ${state.stale.last_ok}` : ''}`;
        staleBanner.hidden = false;
      } else {
        staleBanner.hidden = true;
      }

      const liveExportBtn = document.getElementById('btn-export-live');
      liveExportBtn.hidden = this.data.mode !== 'live';

      const metricLabel = document.getElementById('legend-metric');
      if (desc.metric) {
        metricLabel.textContent = `Height: ${desc.metric.name} (5 quantile bins${desc.metric.unit ? `, ${desc.metric.unit}` : ''})`;
      }

      this._rebuildLayout({ runEstablishingShot: isInitial });
      this.gridView.render(desc, state);
      this.sqlConsole.renderPresets(desc);
      this._updateBreadcrumbs();
      this._updateTelemetry();
    }

    _rebuildLayout({ runEstablishingShot = false } = {}) {
      this.layout = global.RoomViewLayout.buildFsnLayout(this.data.descriptor, this.data.state, {
        expandedGroups: this.expandedGroups,
        expandedPedestals: this.expandedPedestals,
      });
      if (this.scene3d) {
        this.scene3d.setLayout(this.layout, { runEstablishingShot });
      }
      this.ariaTree.build(this.data.descriptor, this.data.state, this.layout);
    }

    _applyTheme(themeKey) {
      this.theme = themeKey === 'grass' ? 'grass' : 'night';
      document.documentElement.setAttribute('data-theme', this.theme);
      document.getElementById('btn-theme-night').classList.toggle('active', this.theme === 'night');
      document.getElementById('btn-theme-grass').classList.toggle('active', this.theme === 'grass');
      if (this.scene3d) this.scene3d.setTheme(this.theme);
    }

    setViewMode(mode) {
      if (mode === '3d' && !this.webglActive) mode = 'grid';
      this.viewMode = mode;
      this.gridViewEl.hidden = mode !== 'grid';
      this.sqlViewEl.hidden = mode !== 'sql';
      document.getElementById('btn-mode-3d').classList.toggle('active', mode === '3d');
      document.getElementById('btn-mode-grid').classList.toggle('active', mode === 'grid');
      document.getElementById('btn-mode-sql').classList.toggle('active', mode === 'sql');
      if (mode === '3d' && this.scene3d) {
        this.scene3d.requestRender();
      }
    }

    expandPedestal(pedestalId) {
      this.expandedPedestals.add(pedestalId);
      this._rebuildLayout({ runEstablishingShot: false });
      this.handleNodeSelect(pedestalId, { pushHistory: false });
    }

    expandGroup(groupId) {
      this.expandedGroups.add(groupId);
      this._rebuildLayout({ runEstablishingShot: false });
    }

    handleNodeSelect(nodeId, { shiftKey = false, fromTree = false, pushHistory = true, instant = false } = {}) {
      if (!this.layout) return;
      // Handle "+N more" and collapsed stage summaries cleanly
      const node = this.layout.nodeById.get(nodeId);
      if (!node) {
        // Item may currently be inside a collapsed group (>50) or aggregated >16 pedestal!
        if (nodeId.startsWith('item:')) {
          const rawItemId = nodeId.slice(5);
          this.selectItemById(rawItemId, { openInspector: true, instant });
        }
        return;
      }

      if (node.nodeType === 'more') {
        this.expandPedestal(node.pedestalId);
        return;
      }

      if (node.nodeType === 'stage-summary') {
        this.expandGroup(node.groupId);
        this.handleNodeSelect(`stage:${node.groupId}:${node.stageId}`, { instant });
        return;
      }

      if (node.nodeType === 'group' && node.collapsed) {
        this.expandGroup(node.groupId);
      }

      if (pushHistory && this.currentNodeId && this.currentNodeId !== nodeId) {
        this.history.push(this.currentNodeId);
        if (this.history.length > 40) this.history.shift();
      }
      this.currentNodeId = nodeId;
      document.getElementById('btn-back').disabled = this.history.length === 0;

      const freshNode = this.layout.nodeById.get(nodeId) || node;
      if (this.scene3d) {
        this.scene3d.focusNode(nodeId, { moveCamera: !shiftKey, instant });
      }

      if (freshNode.nodeType === 'item') {
        this.evidence.showItem(freshNode.item, this.data.descriptor, this.data.state, freshNode.quantileBin);
        const announcement = `Focused ${freshNode.groupId} / ${freshNode.stageId} / ${freshNode.label}, ${freshNode.kind}${freshNode.item.metric != null ? `, ${freshNode.item.metric}` : ''}`;
        if (!fromTree) this.ariaTree.syncFromWorld(nodeId, announcement);
        else this.ariaTree.announce(announcement);
      } else {
        this.evidence.hide();
        const announcement = `Focused directory ${freshNode.label}, ${freshNode.counts ? freshNode.counts.total : 0} items`;
        if (!fromTree) this.ariaTree.syncFromWorld(nodeId, announcement);
        else this.ariaTree.announce(announcement);
      }
      this._updateBreadcrumbs();
      this._updateTelemetry();
    }

    selectItemById(itemId, { openInspector = true, instant = false } = {}) {
      const item = (this.data.state.items || []).find((it) => it.id === itemId);
      if (!item) return false;
      // Ensure group (>50) and pedestal (>16) are expanded if item was aggregated
      let rebuilt = false;
      const gNode = this.layout.nodeById.get(`group:${item.group}`);
      if (gNode && gNode.collapsed) {
        this.expandedGroups.add(item.group);
        rebuilt = true;
      }
      if (!this.layout.itemNodeById.has(item.id)) {
        this.expandedPedestals.add(`stage:${item.group}:${item.stage}`);
        rebuilt = true;
      }
      if (rebuilt) {
        this._rebuildLayout({ runEstablishingShot: false });
      }
      this.handleNodeSelect(`item:${item.id}`, { instant });
      if (!openInspector) this.evidence.hide();
      return true;
    }

    ascendToParent() {
      if (this.evidence.isOpen()) {
        this.evidence.hide();
        const cur = this.layout && this.layout.nodeById.get(this.currentNodeId);
        if (cur && cur.parentId) {
          this.handleNodeSelect(cur.parentId);
          return;
        }
      }
      const cur = this.layout && this.layout.nodeById.get(this.currentNodeId);
      if (cur && cur.parentId) {
        this.handleNodeSelect(cur.parentId);
      } else {
        this.frameAll();
      }
    }

    frameAll({ instant = false } = {}) {
      this.evidence.hide();
      this.currentNodeId = 'root';
      if (this.scene3d) this.scene3d.frameOverview({ instant });
      this.ariaTree.syncFromWorld('root', 'Framed complete filesystem overview');
      this._updateBreadcrumbs();
    }

    stepFailure(delta) {
      const failures = (this.data.state.items || []).filter((it) => it.kind === 'fail');
      if (!failures.length) return;
      this.failureIndex = (this.failureIndex + delta + failures.length) % failures.length;
      this.selectItemById(failures[this.failureIndex].id, { openInspector: true });
    }

    _updateBreadcrumbs() {
      const nav = document.getElementById('breadcrumbs');
      nav.textContent = '';
      if (!this.layout) return;
      const chain = [];
      let cur = this.layout.nodeById.get(this.currentNodeId);
      while (cur) {
        chain.unshift(cur);
        cur = cur.parentId ? this.layout.nodeById.get(cur.parentId) : null;
      }
      if (!chain.length && this.layout.nodeById.has('root')) {
        chain.push(this.layout.nodeById.get('root'));
      }
      chain.forEach((node, idx) => {
        if (idx > 0) {
          const sep = document.createElement('span');
          sep.className = 'crumb-sep';
          sep.textContent = '/';
          nav.appendChild(sep);
        }
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'crumb-btn';
        btn.textContent = node.nodeType === 'root' ? 'root' : node.label;
        btn.addEventListener('click', () => this.handleNodeSelect(node.id));
        nav.appendChild(btn);
      });
    }

    _updateTelemetry() {
      const el = document.getElementById('hud-telemetry');
      const items = (this.data.state && this.data.state.items) || [];
      const counts = global.RoomViewLayout.outcomeCounts(items);
      const spotLabel = this.spotlightMode === 'v2' ? 'Spot v2' : 'Spot baseline';
      const draws = this.scene3d ? ` · ${this.scene3d.stats.drawCalls} draws` : ' · 2D';
      el.textContent = `${counts.total} items (${counts.fail} fail) · ${spotLabel}${draws}`;
    }

    _updateHoverTooltip(node, x, y) {
      if (!node || this.viewMode !== '3d') {
        this.tooltipEl.hidden = true;
        return;
      }
      const unit = this.data.descriptor.metric && this.data.descriptor.metric.unit ? ` ${this.data.descriptor.metric.unit}` : '';
      this.tooltipEl.textContent = '';
      const tEl = document.createElement('div');
      tEl.className = 'tt-title';
      tEl.textContent = node.label;
      const mEl = document.createElement('div');
      mEl.className = 'tt-meta';
      if (node.nodeType === 'item') {
        const rawMetric = node.item.metric != null ? `${node.item.metric}${unit} (Bin ${node.quantileBin}/5)` : 'n/a';
        mEl.textContent = `${node.kind.toUpperCase()} · ${rawMetric} · /${node.groupId}/${node.stageId}`;
      } else if (node.counts) {
        mEl.textContent = `${node.counts.total} items · ${node.counts.pass} pass · ${node.counts.fail} fail · ${node.counts.skip} skip`;
      }
      this.tooltipEl.append(tEl, mEl);
      this.tooltipEl.style.left = `${Math.min(window.innerWidth - 290, x + 14)}px`;
      this.tooltipEl.style.top = `${Math.min(window.innerHeight - 90, y + 14)}px`;
      this.tooltipEl.hidden = false;
    }

    _runSearch(query) {
      this.gridView.setSearch(query);
      const q = (query || '').trim().toLowerCase();
      if (!q) {
        this.searchBarEl.hidden = true;
        this.searchBarEl.textContent = '';
        return;
      }
      const hits = (this.data.state.items || [])
        .filter((it) => `${it.id} ${it.label} ${it.group} ${it.stage} ${it.kind}`.toLowerCase().includes(q))
        .slice(0, 14);
      this.searchBarEl.textContent = '';
      if (!hits.length) {
        const div = document.createElement('div');
        div.className = 'muted-text';
        div.style.padding = '6px 8px';
        div.textContent = `No matches for "${query}"`;
        this.searchBarEl.appendChild(div);
      } else {
        for (const item of hits) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'search-hit';
          const left = document.createElement('span');
          left.textContent = `${item.kind === 'fail' ? '×' : (item.kind === 'skip' ? '○' : '●')} ${item.label} (${item.id})`;
          const right = document.createElement('span');
          right.className = 'muted-text';
          right.textContent = `${item.group}/${item.stage}`;
          btn.append(left, right);
          btn.addEventListener('click', () => {
            this.searchBarEl.hidden = true;
            this.selectItemById(item.id, { openInspector: true });
          });
          this.searchBarEl.appendChild(btn);
        }
      }
      this.searchBarEl.hidden = false;
    }

    async _openLiveExportModal() {
      const modal = document.getElementById('export-modal');
      const body = document.getElementById('export-modal-body');
      const confirmBtn = document.getElementById('btn-export-confirm');
      body.textContent = 'Computing snapshot size accounting…';
      modal.hidden = false;
      confirmBtn.disabled = true;

      try {
        const preview = await this.data.fetchLiveExportPreview();
        const acc = preview.accounting;
        const sens = preview.sensitivity;
        const mb = (b) => `${(b / (1024 * 1024)).toFixed(2)} MiB`;

        body.textContent = '';
        const list = document.createElement('div');
        list.className = 'kv-grid';
        const rows = [
          ['Sensitivity', sens.level.toUpperCase()],
          ['Items', String(acc.item_count)],
          ['Attachments', `${acc.attachment_count} (${acc.unique_attachment_blobs} unique)`],
          ['Template', mb(acc.template_bytes)],
          ['SQLite DB', `${Math.round(acc.db_raw_bytes / 1024)} KiB`],
          ['Final HTML', mb(acc.final_html_bytes)],
        ];
        for (const [k, v] of rows) {
          const dk = document.createElement('div');
          dk.className = 'kv-key';
          dk.textContent = k;
          const dv = document.createElement('div');
          dv.className = 'kv-val';
          dv.textContent = v;
          list.append(dk, dv);
        }
        body.appendChild(list);

        let ackChecked = !sens.requires_acknowledgment;
        if (sens.requires_acknowledgment) {
          const warn = document.createElement('p');
          warn.style.color = '#ffb39a';
          warn.style.fontWeight = '600';
          warn.textContent = sens.banner_text;
          const label = document.createElement('label');
          label.style.display = 'flex';
          label.style.gap = '8px';
          label.style.alignItems = 'center';
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.addEventListener('change', () => {
            ackChecked = cb.checked;
            confirmBtn.disabled = !ackChecked;
          });
          label.append(cb, document.createTextNode('I acknowledge this snapshot contains sensitive data and will keep it on approved storage.'));
          body.append(warn, label);
        }
        confirmBtn.disabled = !ackChecked;
        confirmBtn.onclick = () => {
          const url = this.data.liveExportUrl({ ack: ackChecked });
          window.location.assign(url);
          modal.hidden = true;
        };
      } catch (err) {
        body.textContent = `Could not prepare export preview: ${err.message}`;
      }
    }

    _preparePrint() {
      document.getElementById('print-title').textContent = this.data.descriptor.title || 'Josh Room View Snapshot';
      document.getElementById('print-meta').textContent = `Taken: ${this.data.state.taken_at} · Sensitivity: ${this.data.sensitivity.level}`;
      const img = document.getElementById('print-map-img');
      if (this.scene3d) {
        img.src = this.scene3d.captureTopDownDataUrl();
        img.hidden = false;
      } else {
        img.hidden = true;
      }
    }

    _bindHud() {
      document.getElementById('btn-mode-3d').addEventListener('click', () => this.setViewMode('3d'));
      document.getElementById('btn-mode-grid').addEventListener('click', () => this.setViewMode('grid'));
      document.getElementById('btn-mode-sql').addEventListener('click', () => this.setViewMode('sql'));
      document.getElementById('btn-theme-night').addEventListener('click', () => this._applyTheme('night'));
      document.getElementById('btn-theme-grass').addEventListener('click', () => this._applyTheme('grass'));
      document.getElementById('btn-frame-all').addEventListener('click', () => this.frameAll());
      document.getElementById('btn-back').addEventListener('click', () => {
        if (!this.history.length) return;
        const prev = this.history.pop();
        this.handleNodeSelect(prev, { pushHistory: false });
      });
      document.getElementById('btn-inspector-close').addEventListener('click', () => this.ascendToParent());
      document.getElementById('btn-prev-fail').addEventListener('click', () => this.stepFailure(-1));
      document.getElementById('btn-next-fail').addEventListener('click', () => this.stepFailure(1));

      const treeToggle = document.getElementById('btn-tree-toggle');
      const setTreeOpen = (open) => {
        this.treePanelEl.setAttribute('data-open', open ? 'true' : 'false');
        treeToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      };
      treeToggle.addEventListener('click', () => setTreeOpen(this.treePanelEl.getAttribute('data-open') !== 'true'));
      document.getElementById('btn-tree-close').addEventListener('click', () => setTreeOpen(false));

      const spotBtn = document.getElementById('btn-spotlight-ab');
      const syncSpotBtn = () => { spotBtn.textContent = this.spotlightMode === 'v2' ? 'Spot: v2' : 'Spot: base'; };
      syncSpotBtn();
      spotBtn.addEventListener('click', () => {
        this.spotlightMode = this.spotlightMode === 'v2' ? 'baseline' : 'v2';
        syncSpotBtn();
        if (this.scene3d) this.scene3d.setSpotlightMode(this.spotlightMode);
        this._updateTelemetry();
      });

      const motionBtn = document.getElementById('btn-motion');
      const syncMotionBtn = () => { motionBtn.textContent = this.reducedMotion ? 'Motion: Off' : 'Motion: On'; };
      syncMotionBtn();
      motionBtn.addEventListener('click', () => {
        this.reducedMotion = !this.reducedMotion;
        syncMotionBtn();
        if (this.scene3d) this.scene3d.setReducedMotion(this.reducedMotion);
      });

      const helpModal = document.getElementById('help-modal');
      document.getElementById('btn-help').addEventListener('click', () => { helpModal.hidden = !helpModal.hidden; });
      document.getElementById('btn-help-close').addEventListener('click', () => { helpModal.hidden = true; });
      helpModal.addEventListener('click', (ev) => { if (ev.target === helpModal) helpModal.hidden = true; });

      document.getElementById('btn-export-live').addEventListener('click', () => this._openLiveExportModal());
      const exportModal = document.getElementById('export-modal');
      document.getElementById('btn-export-close').addEventListener('click', () => { exportModal.hidden = true; });
      document.getElementById('btn-export-cancel').addEventListener('click', () => { exportModal.hidden = true; });

      document.getElementById('btn-print').addEventListener('click', () => {
        this._preparePrint();
        window.print();
      });
      window.addEventListener('beforeprint', () => this._preparePrint());

      this.searchInputEl.addEventListener('input', () => this._runSearch(this.searchInputEl.value));
      this.searchInputEl.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') {
          this.searchInputEl.value = '';
          this._runSearch('');
          this.searchInputEl.blur();
        }
      });
    }

    _bindKeyboardMatrix() {
      window.addEventListener('keydown', (ev) => {
        const tag = (document.activeElement && document.activeElement.tagName) || '';
        const inInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
        if (inInput) return;

        // Cancel establishing shot on any key press
        if (this.scene3d) this.scene3d.cancelIntroAndTour();

        if (ev.key === '/') {
          ev.preventDefault();
          this.searchInputEl.focus();
          this.searchInputEl.select();
        } else if (ev.key === '?') {
          ev.preventDefault();
          const m = document.getElementById('help-modal');
          m.hidden = !m.hidden;
        } else if (ev.key === 'Escape') {
          ev.preventDefault();
          const help = document.getElementById('help-modal');
          const exp = document.getElementById('export-modal');
          const lb = document.getElementById('lightbox-modal');
          if (!help.hidden) { help.hidden = true; return; }
          if (!exp.hidden) { exp.hidden = true; return; }
          if (!lb.hidden) { lb.hidden = true; return; }
          this.ascendToParent();
        } else if (ev.key === 'f' || ev.key === 'F') {
          ev.preventDefault();
          this.frameAll();
        } else if (ev.key === 'g' || ev.key === 'G') {
          ev.preventDefault();
          this.setViewMode(this.viewMode === 'grid' ? '3d' : 'grid');
        } else if (ev.key === 'm' || ev.key === 'M') {
          ev.preventDefault();
          document.getElementById('btn-motion').click();
        } else if (ev.key === '[') {
          ev.preventDefault();
          this.stepFailure(-1);
        } else if (ev.key === ']') {
          ev.preventDefault();
          this.stepFailure(1);
        } else if (ev.key === 'Backspace') {
          ev.preventDefault();
          document.getElementById('btn-back').click();
        } else if (ev.key === 'ArrowDown') {
          ev.preventDefault();
          this.ariaTree.moveRelative('down');
        } else if (ev.key === 'ArrowUp') {
          ev.preventDefault();
          this.ariaTree.moveRelative('up');
        } else if (ev.key === 'ArrowRight') {
          ev.preventDefault();
          this.ariaTree.moveRelative('right');
        } else if (ev.key === 'ArrowLeft') {
          ev.preventDefault();
          this.ariaTree.moveRelative('left');
        } else if (ev.key === 'Home') {
          ev.preventDefault();
          this.ariaTree.moveRelative('home');
        } else if (ev.key === 'End') {
          ev.preventDefault();
          this.ariaTree.moveRelative('end');
        } else if (ev.key === 'Enter') {
          if (document.activeElement && document.activeElement.tagName === 'BUTTON') return;
          ev.preventDefault();
          this.handleNodeSelect(this.ariaTree.focusedId);
        }
      });
    }

    _startLivePolling() {
      const intervalMs = Math.max(1000, (this.data.liveConfig && this.data.liveConfig.pollIntervalMs) || 5000);
      setInterval(async () => {
        try {
          await this.data.refreshLive();
          this._syncFromData({ isInitial: false });
        } catch (_) {
          const staleBanner = document.getElementById('stale-banner');
          staleBanner.textContent = 'STALE DATA (poll-connection-error) — retrying automatically';
          staleBanner.hidden = false;
        }
      }, intervalMs);
    }

    _exposeDebugHandle() {
      const app = this;
      global.__JOSH_ROOM_VIEW_DEBUG__ = {
        get mode() { return app.data.mode; },
        get viewMode() { return app.viewMode; },
        get theme() { return app.theme; },
        get spotlightMode() { return app.spotlightMode; },
        get webglActive() { return app.webglActive; },
        get reducedMotion() { return app.reducedMotion; },
        get currentNodeId() { return app.currentNodeId; },
        get itemCount() { return (app.data.state.items || []).length; },
        get renderCount() { return app.scene3d ? app.scene3d.stats.renderCount : 0; },
        get shadowUpdateCount() { return app.scene3d ? app.scene3d.stats.shadowUpdateCount : 0; },
        get drawCalls() { return app.scene3d ? app.scene3d.stats.drawCalls : 0; },
        get firstFrameMs() { return app.scene3d ? app.scene3d.stats.firstFrameMs : null; },
        get lastFirstClickMs() { return app.scene3d ? app.scene3d.stats.lastFirstClickMs : null; },
        get cameraY() { return app.scene3d ? app.scene3d.camera.position.y : null; },
        get layoutSummary() {
          if (!app.layout) return null;
          return {
            pedestals: app.layout.pedestals.length,
            blocks: app.layout.blocks.length,
            summaryBlocks: app.layout.summaryBlocks.length,
            wires: app.layout.wires.length,
          };
        },
        setTheme: (t) => app._applyTheme(t),
        setViewMode: (m) => app.setViewMode(m),
        setSpotlightMode: (m) => {
          app.spotlightMode = m;
          if (app.scene3d) app.scene3d.setSpotlightMode(m);
          document.getElementById('btn-spotlight-ab').textContent = m === 'v2' ? 'Spot: v2' : 'Spot: base';
          app._updateTelemetry();
        },
        setReducedMotion: (v) => {
          app.reducedMotion = Boolean(v);
          if (app.scene3d) app.scene3d.setReducedMotion(app.reducedMotion);
        },
        focusNode: (id, instant = true) => {
          app.handleNodeSelect(id, { instant });
          if (app.scene3d) app.scene3d.settleAnimationsImmediately();
        },
        selectItem: (id, openInspector = true, instant = true) => {
          app.selectItemById(id, { openInspector, instant });
          if (app.scene3d) app.scene3d.settleAnimationsImmediately();
        },
        frameAll: (instant = true) => {
          app.frameAll({ instant });
          if (app.scene3d) app.scene3d.settleAnimationsImmediately();
        },
        settle: () => {
          if (app.scene3d) app.scene3d.settleAnimationsImmediately();
        },
        runSql: (q) => app.sqlConsole.execute(q),
        openEvidenceTab: () => app.evidence.switchTab('evidence'),
        hideInspector: () => app.evidence.hide(),
      };
    }
  }

  global.RoomViewAppClass = RoomViewApp;
})(window);
