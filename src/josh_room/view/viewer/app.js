// Main shared Room View controller (used identically by live localhost and offline single-file HTML).
// Implements arbitrary-depth NAVIGATE, typed read-only INSPECT, clickable relationship jumps,
// custom legend rendering, live-poll inspector reconciliation, and single-handled keyboard events.
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

      this.expandedDirs = new Set();
      this.collapsedDirs = new Set();
      this.expandedPedestals = new Set();
      this.history = [];
      this.forward = [];
      this.fsPage = null;
      this.fsRequest = null;
      this.fsBusy = false;
      this.sceneContext = [];
      this.sceneRequest = null;
      this.fsCoverage = this.data.filesystem ? 'loading' : null;
      if (this.data.filesystem) this.data.applyFilesystemNodes([this._placeholderRoot()], null);
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

      this.evidence = new global.RoomViewEvidence.EvidenceDrawer(dataLayer, loadPdfJs, {
        onJumpToObject: (objectId) => this.selectItemById(objectId, { openInspector: true }),
      });
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
          onToggle: (nodeId, expand) => {
            if (expand) this.expandDirectory(nodeId);
            else {
              this._remember(nodeId);
              this.currentNodeId = nodeId;
              this.expandedDirs.delete(nodeId);
              this.collapsedDirs.add(nodeId);
              this.evidence.hide();
              this.scene3d?.clearSpotlight();
              this._rebuildLayout({ preserveCamera: true });
              this.ariaTree.syncFromWorld(nodeId);
              this._updateBreadcrumbs();
              this._renderGrid();
              this._updateNavigationButtons();
            }
          },
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
        if (this.data.filesystem) this._openFilesystem('root', { pushHistory: false });
        this._startLivePolling();
      }
    }

    _placeholderRoot() {
      return { id: 'root', node_kind: 'directory', parent_id: null, label: '/' };
    }

    _treeId(node) {
      return node.parent_id == null ? 'root' : global.RoomViewLayout.makeCustomNodeId(node.id);
    }

    _directoryId() {
      const current = this.layout?.allTreeNodesById.get(this.currentNodeId);
      return current?.nodeType === 'item' ? current.parentId : current?.id || 'root';
    }

    _renderGrid() {
      this.filesystemSearch?.syncSelection(this.layout.allTreeNodesById.get(this.currentNodeId)?.objectId);
      const directoryId = this._directoryId();
      const pageMatches = this.fsPage && this._treeId(this.fsPage.directory) === directoryId;
      this.gridView.render(this.data.descriptor, this.data.state, this.layout, {
        directoryId,
        selectedId: this.currentNodeId,
        coverage: pageMatches ? this.fsPage.coverage : undefined,
        coverageLabel: pageMatches || !this.fsPage ? this.fsCoverage : undefined,
      });
    }

    _updateNavigationButtons() {
      document.getElementById('btn-back').disabled = !this.history.length;
      document.getElementById('btn-forward').disabled = !this.forward.length;
      document.getElementById('btn-up').disabled = this.currentNodeId === 'root';
      const badge = document.getElementById('fs-coverage');
      const more = document.getElementById('fs-continue');
      document.getElementById('fs-details').hidden = !this.fsPage || this.currentNodeId !== this._treeId(this.fsPage.directory);
      badge.hidden = !this.fsCoverage;
      if (this.fsCoverage) badge.textContent = this.fsCoverage;
      more.hidden = !this.data.filesystem || !(this.fsRetry || this.fsResume || this.fsPage?.cursor);
      more.disabled = this.fsBusy;
      more.textContent = this.fsRetry ? 'Retry' : 'Continue';
    }

    _remember(nodeId) {
      if (this.currentNodeId === nodeId) return;
      this.history.push(this.currentNodeId);
      if (this.history.length > 40) this.history.shift();
      this.forward.length = 0;
    }

    async _openFilesystem(id, { pushHistory = true, cursor = null, kind = null,
      refresh = false, instant = false, moveCamera = true, reading = null, reconcile = true, frame = false, invalidation = null } = {}) {
      this.fsRequest?.abort();
      this.pagePoll?.abort();
      this.sceneRequest?.abort();
      this.sceneRequest = null;
      this.fsResume = null;
      const previousPage = this.fsPage;
      const previousNode = this.layout?.allTreeNodesById.get(this.currentNodeId);
      const wasReading = this.evidence.isOpen();
      const previousChain = previousPage
        ? [...previousPage.ancestors, previousPage.directory, ...(previousNode?.nodeType === 'item'
          ? [{ id: previousNode.objectId, node_kind: previousNode.nodeKind }] : [])] : [];
      const request = new AbortController();
      this.fsRequest = request;
      this.fsBusy = true;
      this.fsRetry = false;
      const generation = this.data.fsGeneration;
      const query = (name, params) => this.data.queryFilesystem(name, {
        ...(generation ? { generation } : {}), ...params,
      }, request.signal);
      if (!refresh) {
        this.fsCoverage = kind === 'directory' || id === 'root' ? 'Loading directory…' : 'Loading selection…';
        this._updateNavigationButtons();
      }
      try {
        if (invalidation) throw invalidation;
        let page = this.fsPage;
        if (!kind && id !== 'root') {
          const inspected = await query('inspect', { id });
          kind = inspected.node.node_kind;
        }
        const isFile = Boolean(kind && kind !== 'directory');
        let file = null;
        if (isFile) {
          const preview = await query('preview', { id });
          file = preview.node;
          if (page?.directory.id !== file.parent_id) {
            page = await query('list', { id: file.parent_id });
          }
        } else {
          page = await query('list', { id, ...(cursor ? { cursor } : {}) });
          if (cursor && previousNode?.nodeType === 'item' && previousPage?.directory.id === id) {
            file = (await query('preview', { id: previousNode.objectId })).node;
          }
        }
        if (request.signal.aborted) return;
        if (generation && page.generation !== generation) throw Object.assign(new Error('Stale directory'), { status: 409 });
        // Keep the previous observation intact until the destination and its
        // independently verified neighborhood can replace it in one layout.
        const stageScene = this.scene3d && this.viewMode === '3d' && previousPage;
        let destinationContext = [];
        if (stageScene) {
          try {
            destinationContext = await this.data.sceneNeighborhood(page, request.signal,
              this._neighborCandidates(page));
          } catch (error) {
            if (request.signal.aborted || ![413, 429, 503].includes(error.status)) throw error;
          }
          if (request.signal.aborted) return;
          // Navigation performs its authoritative query first. Revalidate before
          // publishing the selected content if the later samples took time.
          await query('inspect', { id: file?.id || page.directory.id, generation: page.generation });
          if (request.signal.aborted) return;
        }
        const selected = file || page.directory;
        const selectedId = this._treeId(selected);
        const previousId = this.currentNodeId;
        if (pushHistory) this._remember(selectedId);
        const nodes = new Map();
        for (const node of [...page.ancestors, page.directory, ...page.entries, selected]) {
          nodes.set(node.id, node);
        }
        const contextChanged = JSON.stringify(this.sceneContext || []) !== JSON.stringify(destinationContext);
        this.sceneContext = destinationContext;
        this.fsPage = page;
        const dir = nodes.get(page.directory.id);
        nodes.set(dir.id, { ...dir, fields: { ...dir.fields, coverage: page.coverage,
          entries_on_page: page.entries.length, examined: page.examined, omitted: page.omitted } });
        this.fsCoverage = `${page.coverage} · ${page.entries.length} shown · ${page.examined} examined · ${page.omitted} omitted`;
        this.currentNodeId = selectedId;
        this.data.applyFilesystemNodes(Array.from(nodes.values()), page.generation);
        this._syncFromData({ contextChanged, navigationTargetId: previousId !== selectedId ? selectedId : null });
        const target = this.layout.allTreeNodesById.get(selectedId);
        const block = this.layout.nodeById.get(selectedId);
        if (!refresh) {
          if (frame) this.scene3d?.frameOverview({ instant });
          else this.scene3d?.focusNode(selectedId, { instant, moveCamera });
          this.ariaTree.syncFromWorld(selectedId, target?.path || '/');
        }
        if (target && ((!refresh && file && (!cursor || wasReading)) || (refresh && wasReading))) {
          const restoring = reading && target.objectId === reading.id;
          const unchanged = restoring && await this.evidence.readingUnchanged(target, reading);
          if (request.signal.aborted) return;
          this.evidence.showTarget(target, this.data.descriptor, this.data.state, this.layout, {
            quantileBin: block?.quantileBin, preserveTab: previousId === selectedId,
            reading: unchanged ? reading : null, deferContent: restoring && !unchanged,
          });
        } else if (!refresh) this.evidence.hide();
        if (!request.signal.aborted && !stageScene) this._sampleScene(page);
      } catch (error) {
        if (request.signal.aborted) return;
        if (error.status === 429) {
          this.fsCoverage = 'Directory request busy. Retry to continue.';
          this.fsResume = () => this._openFilesystem(id, { pushHistory, cursor, kind, refresh, instant, moveCamera, frame });
          return;
        }
        const position = this.evidence.captureReadingPosition?.();
        if ([403, 409].includes(error.status)) {
          this.history.length = 0;
          this.forward.length = 0;
        }
        this.filesystemSearch?.observationLost();
        this.evidence.hide();
        this.scene3d?.clearSpotlight();
        this._clearSceneContext(false);
        this.fsPage = null;
        this.fsCoverage = error.status === 409
          ? 'Workspace changed. Reopen the root to refresh.'
          : `Directory unavailable (${error.message}).`;
        this.fsRetry = true;
        this.data.applyFilesystemNodes([this._placeholderRoot()], null);
        this.currentNodeId = 'root';
        this._syncFromData();
        if (reconcile && (error.status === 409 || error.status === 404) && previousChain.length) {
          try {
            await this._restoreFilesystem(previousChain, request.signal, wasReading, position);
          } catch (_) {
            if (!request.signal.aborted) {
              this.fsRetry = true;
              this.fsCoverage = 'Workspace unavailable. Retry from the root.';
            }
          }
        }
      } finally {
        if (!request.signal.aborted) {
          this.fsBusy = false;
          this._updateNavigationButtons();
        }
      }
    }

    async _restoreFilesystem(chain, signal, wasReading, reading) {
      let index = 1;
      let directory = 'root';
      let generation;
      let cursor;
      const resume = async () => {
        this.fsResume = null;
        this.fsBusy = true;
        try {
          for (let batch = 0; batch < 8; batch++) {
            const page = await this.data.queryFilesystem('list', {
              id: directory, ...(generation ? { generation } : {}), ...(cursor ? { cursor } : {}),
            }, signal);
            if (signal.aborted) return;
            generation = page.generation;
            cursor = page.cursor;
            const next = page.entries.find(node => node.id === chain[index]?.id);
            if (next?.node_kind === 'directory') {
              directory = next.id;
              index++;
              cursor = null;
              continue;
            }
            if (next || index === chain.length || !cursor) {
              this.data.fsGeneration = generation;
              this.fsPage = page;
              this.fsRetry = false;
              await this._openFilesystem(next?.id || directory, {
                kind: next?.node_kind || 'directory', pushHistory: false, instant: true, reading, reconcile: false,
              });
              if (!wasReading) this.evidence.hide();
              return;
            }
          }
          this.fsResume = resume;
          this.fsRetry = false;
          this.fsCoverage = 'Rechecking selected path. Continue to resume.';
        } catch (error) {
          if (signal.aborted) return;
          this.fsCoverage = `Workspace unavailable (${error.message}). Retry from root.`;
          this.fsRetry = true;
        } finally {
          if (!signal.aborted) {
            this.fsBusy = false;
            this._updateNavigationButtons();
          }
        }
      };
      return resume();
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
        if (descriptorWantsGrid) this.setViewMode('grid');
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

    _renderBottomLegend(descriptor) {
      const strip = document.getElementById('legend-strip');
      if (!strip) return;
      strip.textContent = '';
      const entries = global.RoomViewLayout.getLegendEntries(descriptor);
      for (const entry of entries) {
        const span = document.createElement('span');
        span.className = 'legend-item';
        const sw = document.createElement('span');
        sw.className = 'swatch';
        sw.setAttribute('aria-hidden', 'true');
        if (entry.shape === 'hollow') {
          sw.style.border = `2px solid ${entry.color}`;
          sw.style.background = 'transparent';
        } else {
          sw.style.background = entry.color;
          if (entry.shape === 'notch') {
            sw.style.color = '#fff';
            sw.textContent = '×';
          }
        }
        span.append(sw, document.createTextNode(entry.label));
        strip.appendChild(span);
      }
      const sep = document.createElement('span');
      sep.className = 'legend-sep';
      sep.setAttribute('aria-hidden', 'true');
      sep.textContent = '|';
      const metricSpan = document.createElement('span');
      metricSpan.id = 'legend-metric';
      metricSpan.className = 'legend-metric';
      if (descriptor && descriptor.metric) {
        metricSpan.textContent = `Height: ${descriptor.metric.name} (5 quantile bins${descriptor.metric.unit ? `, ${descriptor.metric.unit}` : ''})`;
      } else {
        metricSpan.textContent = 'Height: 5 quantile bins';
      }
      strip.append(sep, metricSpan);
      const key = document.getElementById('menu-map-key');
      if (key) key.replaceChildren(...Array.from(strip.children, child => {
        const entry = child.cloneNode(true);
        entry.removeAttribute('id');
        return entry;
      }));
    }

    _syncFromData({ isInitial = false, contextChanged = false, navigationTargetId = null } = {}) {
      const desc = this.data.descriptor;
      const state = this.data.state;
      const sens = this.data.sensitivity;

      document.title = `${desc.title || 'Josh Room View'} — Room View`;

      document.getElementById('room-title').textContent = desc.title || 'Room View';
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

      this._renderBottomLegend(desc);
      const signature = JSON.stringify([desc, state.nodes, state.items, state.groups, this.currentNodeId]);
      if (this.layoutSignature !== signature) {
        const previous = this.layout;
        this._rebuildLayout({ runEstablishingShot: isInitial, preserveCamera: !isInitial, navigationTargetId });
        while (this.currentNodeId !== 'root' && !this.layout.allTreeNodesById.has(this.currentNodeId)) {
          this.currentNodeId = previous?.allTreeNodesById.get(this.currentNodeId)?.parentId || 'root';
        }
        this.layoutSignature = signature;
      } else if (contextChanged) this._drawScene({ preserveCamera: true });
      this._renderGrid();
      this._updateNavigationButtons();
      if (!this.data.filesystem && this.searchInputEl.value) this._runSearch(this.searchInputEl.value);
      this.sqlConsole.renderPresets(desc);

      // Reconcile open inspector drawer against refreshed state after live poll!
      if (!this.data.filesystem && !isInitial && this.evidence.isOpen() && this.evidence.currentObjectId) {
        const objId = this.evidence.currentObjectId;
        const refreshedLeaf = this.layout.allLeavesByObjectId.get(objId);
        if (refreshedLeaf) {
          const blockNode = this.layout.itemNodeById.get(objId);
          const qBin = blockNode
            ? blockNode.quantileBin
            : global.RoomViewLayout.metricToQuantileBin(refreshedLeaf.metric, this.layout.quantileBreaks) + 1;
          this.evidence.showTarget(refreshedLeaf, desc, state, this.layout, {
            quantileBin: qBin,
            preserveTab: true,
          });
        } else {
          this.evidence.hide();
          if (this.scene3d) this.scene3d.clearSpotlight();
          if (!this.layout.nodeById.has(this.currentNodeId)) {
            this.currentNodeId = 'root';
          }
        }
      }

      this._updateBreadcrumbs();
      this._updateTelemetry();
    }

    _neighborCandidates(page) {
      if (page.directory.id !== this.fsPage?.directory.id || page.generation !== this.fsPage?.generation) return [];
      // Across pages only IDs are hints. Every retained folder must be sampled
      // again before any of its metadata appears in the destination.
      return (this.sceneContext || []).filter(n => n.node_kind === 'directory' &&
        n.parent_id === page.directory.id && n.fields?.coverage !== 'not-examined').map(n => n.id).slice(0, 3);
    }

    _clearSceneContext(redraw = true) {
      this.sceneRequest?.abort();
      this.sceneRequest = null;
      if (!this.sceneContext?.length) return;
      this.sceneContext = [];
      if (this.tooltipEl) {
        this.tooltipEl.hidden = true;
        this.tooltipEl.textContent = '';
      }
      if (redraw) this._drawScene({ preserveCamera: true });
    }

    async _sampleScene(page) {
      if (!this.scene3d || this.viewMode !== '3d' || this.sceneRequest || !page) return;
      const request = new AbortController();
      this.sceneRequest = request;
      try {
        const nodes = await this.data.sceneNeighborhood(page, request.signal, this._neighborCandidates(page));
        if (request.signal.aborted || this.fsPage !== page) return;
        if (JSON.stringify(nodes) !== JSON.stringify(this.sceneContext)) {
          this.sceneContext = nodes;
          this._drawScene({ preserveCamera: true });
        }
      } catch (error) {
        if (request.signal.aborted || this.fsPage !== page) return;
        // Congestion does not change the authority behind the current frame.
        if (error.status === 429) return;
        if (error.status === 409 || error.status === 403) {
          const selected = this.layout.allTreeNodesById.get(this.currentNodeId);
          await this._openFilesystem(selected?.objectId || page.directory.id, {
            pushHistory: false, refresh: true, invalidation: error,
          });
        } else this._clearSceneContext();
      } finally {
        if (this.sceneRequest === request) this.sceneRequest = null;
      }
    }

    _layoutOptions() {
      return { selectedNodeId: this.currentNodeId, expandedDirs: this.expandedDirs,
        collapsedDirs: this.collapsedDirs, expandedPedestals: this.expandedPedestals };
    }

    _drawScene(options) {
      if (!this.scene3d || !this.layout) return;
      let layout = this.layout;
      if (this.sceneContext?.length) {
        const nodes = new Map(this.sceneContext.map(node => [node.id, node]));
        for (const node of this.data.state.nodes) {
          const sample = nodes.get(node.id);
          if (sample?.fields.coverage && node.fields?.coverage === 'not-examined') {
            nodes.set(node.id, { ...node, fields: { ...node.fields, coverage: sample.fields.coverage } });
          } else nodes.set(node.id, node);
        }
        layout = global.RoomViewLayout.buildFsnLayout(this.data.descriptor,
          { ...this.data.state, nodes: [...nodes.values()] }, this._layoutOptions());
      }
      this.scene3d.setLayout(layout, { descriptor: this.data.descriptor, ...options });
    }

    _rebuildLayout({ runEstablishingShot = false, preserveCamera = false, navigationTargetId = null } = {}) {
      this.layout = global.RoomViewLayout.buildFsnLayout(this.data.descriptor, this.data.state, this._layoutOptions());
      this._drawScene({ runEstablishingShot, preserveCamera, navigationTargetId });
      this.ariaTree.expanded = new Set(this.layout.pedestals.filter(node => !node.collapsed).map(node => node.id));
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
        if (this.fsPage) this._sampleScene(this.fsPage);
      } else this._clearSceneContext();
    }

    expandPedestal(pedestalId) {
      this.expandedPedestals.add(pedestalId);
      this._rebuildLayout({ runEstablishingShot: false, preserveCamera: true });
      this.ariaTree.syncFromWorld(this.currentNodeId);
    }

    expandDirectory(dirId) {
      this.collapsedDirs.delete(dirId);
      this.expandedDirs.add(dirId);
      this._rebuildLayout({ runEstablishingShot: false, preserveCamera: true });
    }

    _ensureAncestorsExpanded(treeNodeId) {
      if (!this.layout || !this.layout.allTreeNodesById) return false;
      let rebuilt = false;
      let cur = this.layout.allTreeNodesById.get(treeNodeId);
      while (cur && cur.parentId) {
        const parentId = cur.parentId;
        const parentPed = this.layout.nodeById.get(parentId);
        if (!parentPed || parentPed.collapsed) {
          this.collapsedDirs.delete(parentId);
          this.expandedDirs.add(parentId);
          rebuilt = true;
        }
        cur = this.layout.allTreeNodesById.get(parentId);
      }
      if (rebuilt) {
        this._rebuildLayout({ runEstablishingShot: false, preserveCamera: true });
      }
      return rebuilt;
    }

    handleNodeSelect(nodeId, { shiftKey = false, fromTree = false, pushHistory = true, instant = false } = {}) {
      if (!this.layout) return;
      const logicalNode = this.layout.allTreeNodesById.get(nodeId);
      if (this.data.filesystem && (logicalNode || nodeId === 'root' || nodeId.startsWith('node/'))) {
        const id = logicalNode?.objectId || (nodeId === 'root' ? 'root' : decodeURIComponent(nodeId.slice(5)));
        return this._openFilesystem(id, { kind: logicalNode?.nodeKind, pushHistory, instant, moveCamera: !shiftKey });
      }
      let node = this.layout.nodeById.get(nodeId);
      if (!node) {
        // Node may live inside a collapsed subtree (>50) or aggregated >16 pedestal!
        const treeNode = this.layout.allTreeNodesById && this.layout.allTreeNodesById.get(nodeId);
        if (treeNode) {
          if (treeNode.nodeType === 'item') {
            this.selectItemById(treeNode.objectId, { openInspector: true, instant });
            return;
          }
          this._ensureAncestorsExpanded(nodeId);
          node = this.layout.nodeById.get(nodeId);
        }
        if (!node) return;
      }

      if (node.nodeType === 'more') {
        this.expandPedestal(node.pedestalId);
        return;
      }

      if (node.nodeType === 'stage-summary') {
        this.expandDirectory(node.expandDirId);
        this.handleNodeSelect(node.targetDirId, { instant });
        return;
      }

      if ((node.nodeType === 'directory' || node.nodeType === 'group') && node.collapsed) {
        this.expandDirectory(node.id);
      }

      if (pushHistory && this.currentNodeId && this.currentNodeId !== nodeId) {
        this._remember(nodeId);
      }
      this.currentNodeId = nodeId;
      this._updateNavigationButtons();
      this._renderGrid();

      const freshNode = this.layout.nodeById.get(nodeId) || node;
      if (this.scene3d) {
        this.scene3d.focusNode(nodeId, { moveCamera: !shiftKey, instant });
      }

      if (freshNode.nodeType === 'item') {
        const entry = global.RoomViewLayout.getLegendEntry(this.data.descriptor, freshNode.kind);
        this.evidence.showTarget(freshNode.item, this.data.descriptor, this.data.state, this.layout, {
          quantileBin: freshNode.quantileBin,
          preserveTab: false,
        });
        const announcement = `Focused ${freshNode.path}, ${entry.label}${freshNode.item.metric != null ? `, ${freshNode.item.metric}` : ''}`;
        if (!fromTree) this.ariaTree.syncFromWorld(nodeId, announcement);
        else this.ariaTree.announce(announcement);
      } else {
        this.evidence.hide();
        const announcement = `Entered directory ${freshNode.path || freshNode.label}, ${freshNode.counts ? freshNode.counts.total : 0} items`;
        if (!fromTree) this.ariaTree.syncFromWorld(nodeId, announcement);
        else this.ariaTree.announce(announcement);
      }
      this._updateBreadcrumbs();
      this._updateTelemetry();
    }

    selectItemById(objectId, { openInspector = true, instant = false } = {}) {
      if (!this.layout) return false;
      const leaf = this.layout.allLeavesByObjectId.get(objectId);
      if (!leaf) {
        // Also support jumping to a directory by objectId or treeId
        if (this.layout.allTreeNodesById.has(objectId)) {
          this.handleNodeSelect(objectId, { instant });
          return true;
        }
        for (const tn of this.layout.allTreeNodesById.values()) {
          if (tn.objectId === objectId) {
            this.handleNodeSelect(tn.id, { instant });
            return true;
          }
        }
        return false;
      }

      let rebuilt = this._ensureAncestorsExpanded(leaf.id);
      if (!this.layout.itemNodeById.has(objectId)) {
        this.expandedPedestals.add(leaf.parentId);
        rebuilt = true;
      }
      if (rebuilt) {
        this._rebuildLayout({ runEstablishingShot: false, preserveCamera: true });
      }
      this.handleNodeSelect(leaf.id, { instant });
      if (!openInspector) this.evidence.hide();
      return true;
    }

    ascendToParent() {
      const cur = this.layout?.allTreeNodesById.get(this.currentNodeId);
      if (cur && cur.parentId) {
        this.handleNodeSelect(cur.parentId);
      } else {
        this.frameAll();
      }
    }

    frameAll({ instant = false } = {}) {
      if (this.data.filesystem) return this._openFilesystem('root', { instant, frame: true });
      this._remember('root');
      this.evidence.hide();
      this.currentNodeId = 'root';
      this._updateNavigationButtons();
      this._renderGrid();
      if (this.scene3d) this.scene3d.frameOverview({ instant });
      this.ariaTree.syncFromWorld('root', 'Framed complete filesystem overview');
      this._updateBreadcrumbs();
    }

    stepFailure(delta) {
      const allLeaves = (this.layout && this.layout.allLeaves) || [];
      const failures = allLeaves.filter((leaf) => global.RoomViewLayout.getLegendEntry(this.data.descriptor, leaf.kind).isFailure);
      if (global.RoomViewLayout.isWorkspace(this.data.descriptor) || !failures.length) return;
      if (this.failureIndex < 0 && delta < 0) this.failureIndex = 0;
      this.failureIndex = (this.failureIndex + delta + failures.length) % failures.length;
      this.selectItemById(failures[this.failureIndex].objectId, { openInspector: true });
    }

    _updateBreadcrumbs() {
      const nav = document.getElementById('breadcrumbs');
      nav.textContent = '';
      if (!this.layout) return;
      const chain = [];
      let cur = this.layout.allTreeNodesById.get(this.currentNodeId) || this.layout.nodeById.get(this.currentNodeId);
      while (cur) {
        chain.unshift(cur);
        cur = cur.parentId ? (this.layout.allTreeNodesById.get(cur.parentId) || this.layout.nodeById.get(cur.parentId)) : null;
      }
      if (!chain.length && this.layout.nodeById.has('root')) {
        chain.push(this.layout.nodeById.get('root'));
      }
      chain.forEach((node, idx) => {
        if (idx > 0 && !(idx === 1 && chain[0].nodeType === 'root')) {
          const sep = document.createElement('span');
          sep.className = 'crumb-sep';
          sep.textContent = '/';
          nav.appendChild(sep);
        }
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'crumb-btn';
        const seg = node.nodeType === 'root' ? '/' : (node.segmentLabel || node.label).replace(/^\/+/, '');
        btn.textContent = seg;
        btn.title = node.path || seg;
        if (idx === chain.length - 1) btn.setAttribute('aria-current', 'location');
        btn.addEventListener('click', () => this.handleNodeSelect(node.id));
        nav.appendChild(btn);
      });
    }

    _updateTelemetry() {
      const el = document.getElementById('hud-telemetry');
      const allLeaves = (this.layout && this.layout.allLeaves) || [];
      const counts = global.RoomViewLayout.outcomeCounts(allLeaves, this.data.descriptor);
      const spotLabel = this.spotlightMode === 'v2' ? 'Spot v2' : 'Spot base';
      const draws = this.scene3d ? ` · ${this.scene3d.stats.drawCalls} draws` : ' · 2D';
      const dirInfo = this.layout ? `${this.layout.pedestals.length} dirs · ` : '';
      if (global.RoomViewLayout.isWorkspace(this.data.descriptor)) {
        el.textContent = `${dirInfo}${counts.total} observed files`;
        return;
      }
      el.textContent = `${dirInfo}${counts.total} objects (${counts.fail} fail) · ${spotLabel}${draws}`;
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
      tEl.textContent = node.path || node.label;
      const mEl = document.createElement('div');
      mEl.className = 'tt-meta';
      if (node.nodeType === 'item') {
        const entry = global.RoomViewLayout.getLegendEntry(this.data.descriptor, node.kind);
        const rawMetric = node.item.metric != null ? `${node.item.metric}${unit} (Bin ${node.quantileBin}/5)` : 'n/a';
        mEl.textContent = `${entry.symbol} ${entry.label} · ${rawMetric}`;
      } else if (this.data.filesystem && node.nodeKind === 'directory') {
        const coverage = node.coverage || 'not-examined';
        mEl.textContent = `Directory · ${coverage} · open for current listing`;
        if (this.scene3d?.searchBranches?.has(node.objectId)) mEl.textContent += ' · contains an observed search match';
      } else if (node.counts) {
        mEl.textContent = global.RoomViewLayout.formatCountsSummary(node.counts, this.data.descriptor);
      }
      this.tooltipEl.append(tEl, mEl);
      this.tooltipEl.style.left = `${Math.min(window.innerWidth - 320, x + 14)}px`;
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
      const allNodes = this.layout ? Array.from(this.layout.allTreeNodesById.values()) : [];
      const hits = allNodes
        .filter((n) => {
          const entry = n.kind ? global.RoomViewLayout.getLegendEntry(this.data.descriptor, n.kind) : null;
          const hay = `${n.objectId || n.id} ${n.label} ${n.path || ''} ${n.kind || ''} ${entry ? entry.label : ''} ${n.summary || ''}`.toLowerCase();
          return hay.includes(q);
        })
        .slice(0, 16);

      this.searchBarEl.textContent = '';
      if (!hits.length) {
        const div = document.createElement('div');
        div.className = 'muted-text';
        div.style.padding = '6px 8px';
        div.textContent = `No matches for "${query}"`;
        this.searchBarEl.appendChild(div);
      } else {
        for (const hit of hits) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'search-hit';
          const left = document.createElement('span');
          if (hit.nodeType === 'item') {
            const entry = global.RoomViewLayout.getLegendEntry(this.data.descriptor, hit.kind);
            left.textContent = `${entry.symbol} ${hit.label}`;
          } else {
            left.textContent = `▸ ${hit.path || hit.label}`;
          }
          const right = document.createElement('span');
          right.className = 'muted-text';
          right.style.fontFamily = 'var(--font-mono)';
          right.textContent = hit.nodeType === 'item' ? (hit.parentPath || '/') : 'dir';
          btn.append(left, right);
          btn.addEventListener('click', () => {
            this.searchBarEl.hidden = true;
            if (hit.nodeType === 'item') {
              this.selectItemById(hit.objectId, { openInspector: true });
            } else {
              this.handleNodeSelect(hit.id);
            }
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
          ['Items / Nodes', `${acc.item_count} items / ${acc.node_count || 0} nodes`],
          ['Previews / Links', `${acc.preview_count || 0} previews / ${acc.link_count || 0} links`],
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
      document.getElementById('btn-forward').addEventListener('click', () => {
        if (!this.forward.length) return;
        this.history.push(this.currentNodeId);
        this.handleNodeSelect(this.forward.pop(), { pushHistory: false });
      });
      document.getElementById('btn-up').addEventListener('click', () => this.ascendToParent());
      document.getElementById('fs-details').addEventListener('click', () => {
        const target = this.layout.allTreeNodesById.get(this.currentNodeId);
        if (target) this.evidence.showTarget(target, this.data.descriptor, this.data.state, this.layout);
      });
      document.getElementById('fs-continue').addEventListener('click', () => {
        if (this.fsResume) this.fsResume();
        else if (this.fsRetry) this._openFilesystem('root', { pushHistory: false });
        else if (this.fsPage?.cursor) this._openFilesystem(this.fsPage.directory.id, {
          cursor: this.fsPage.cursor, kind: 'directory', pushHistory: false,
        });
      });
      document.getElementById('btn-back').addEventListener('click', () => {
        if (!this.history.length) return;
        const prev = this.history.pop();
        this.forward.push(this.currentNodeId);
        this.handleNodeSelect(prev, { pushHistory: false });
      });
      document.getElementById('btn-inspector-close').addEventListener('click', () => this.evidence.dismiss({ close: true }));
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

      if (this.data.filesystem) {
        this.filesystemSearch = new global.RoomViewFilesystemSearch({
          list: (id, generation, cursor, signal) => this.data.queryFilesystem('list', {
            id, ...(generation ? { generation } : {}), ...(cursor ? { cursor } : {}),
          }, signal),
          inspect: (id, generation, signal) => this.data.queryFilesystem('inspect', { id, generation }, signal),
          select: hit => this._openFilesystem(hit.id, { kind: hit.node_kind }),
          onMatches: (ids, active, routes) => this.scene3d?.setSearchMatches(ids, active, routes),
          directory: () => this.fsPage?.directory.id || 'root',
          root: () => 'root',
          generation: () => this.data.fsGeneration,
        });
        return;
      }
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
        // Never double-handle events already handled by AccessibleTreeTwin or sub-widgets
        if (ev.defaultPrevented) return;
        const tag = (document.activeElement && document.activeElement.tagName) || '';
        const inInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
        if (inInput) return;

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
          if (this.evidence.isOpen()) { this.evidence.dismiss(); return; }
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

    async _pollFilesystem() {
      if (this.fsBusy || !this.fsPage || this.pagePoll) return;
      const request = new AbortController();
      this.pagePoll = request;
      const page = this.fsPage;
      const selected = this.layout.allTreeNodesById.get(this.currentNodeId);
      const current = () => !request.signal.aborted && page === this.fsPage && !this.fsBusy;
      try {
        const siblings = page.entries.filter(node => node.id !== selected?.objectId);
        const changed = new Map();
        // The selected object keeps its original content binding. All other
        // visible rows are reobserved through their issued direct-child IDs.
        for (let offset = 0; offset < siblings.length; offset += 48) {
          const batch = siblings.slice(offset, offset + 48);
          const fresh = await this.data.queryFilesystem('metadata', {
            id: page.directory.id, generation: page.generation, children: batch.map(n => n.id).join(','),
          }, request.signal);
          if (!current()) return;
          if (fresh.generation !== page.generation || fresh.entries.length !== batch.length ||
              fresh.entries.some((n, i) => n.parent_id !== page.directory.id || n.node_kind !== batch[i].node_kind ||
                (n.node_kind === 'directory' && n.id !== batch[i].id))) {
            throw Object.assign(new Error('Directory observation changed'), { status: 409 });
          }
          fresh.entries.forEach((n, i) => {
            if (JSON.stringify(n) !== JSON.stringify(batch[i])) changed.set(batch[i].id, n);
          });
        }
        await this.data.queryFilesystem('inspect', {
          id: selected?.objectId || page.directory.id, generation: page.generation,
        }, request.signal);
        if (!current()) return;
        if (changed.size) {
          page.entries = page.entries.map(n => changed.get(n.id) || n);
          const nodes = this.data.state.nodes.map(n => changed.get(n.id) || n);
          this.data.applyFilesystemNodes(nodes, page.generation);
          this.filesystemSearch?.reconcileMetadata(changed);
          if (this.tooltipEl) {
            this.tooltipEl.hidden = true;
            this.tooltipEl.textContent = '';
          }
          this._syncFromData();
        }
        await this.filesystemSearch?.validateDisplayed();
        if (current()) await this._sampleScene(page);
      } catch (error) {
        if (!current() || error.status === 429) return;
        return this._openFilesystem(selected?.objectId || page.directory.id, {
          kind: selected?.nodeKind, refresh: true, pushHistory: false, invalidation: error,
        });
      } finally {
        if (this.pagePoll === request) this.pagePoll = null;
      }
    }

    _startLivePolling() {
      const intervalMs = Math.max(1000, (this.data.liveConfig && this.data.liveConfig.pollIntervalMs) || 5000);
      const poll = async () => {
        if (this.data.filesystem) return this._pollFilesystem();
        try {
          await this.data.refreshLive();
          this._syncFromData({ isInitial: false });
        } catch (_) {
          const staleBanner = document.getElementById('stale-banner');
          staleBanner.textContent = 'STALE DATA (poll-connection-error) — retrying automatically';
          staleBanner.hidden = false;
        }
      };
      this.refresh = poll;
      setInterval(poll, intervalMs);
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
        get itemCount() { return app.layout ? app.layout.allLeaves.length : 0; },
        get renderCount() { return app.scene3d ? app.scene3d.stats.renderCount : 0; },
        get shadowUpdateCount() { return app.scene3d ? app.scene3d.stats.shadowUpdateCount : 0; },
        get drawCalls() { return app.scene3d ? app.scene3d.stats.drawCalls : 0; },
        get firstFrameMs() { return app.scene3d ? app.scene3d.stats.firstFrameMs : null; },
        get lastFirstClickMs() { return app.scene3d ? app.scene3d.stats.lastFirstClickMs : null; },
        get cameraY() { return app.scene3d ? app.scene3d.camera.position.y : null; },
        get layoutSummary() {
          if (!app.layout) return null;
          return {
            isUnixTree: app.layout.isUnixTree,
            maxDepth: app.layout.maxDepth,
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
        refreshNow: async () => {
          await app.refresh?.();
        },
        runSql: (q) => app.sqlConsole.execute(q),
        openTab: (tab) => app.evidence.switchTab(tab),
        openEvidenceTab: () => app.evidence.switchTab('evidence'),
        hideInspector: () => app.evidence.hide(),
      };
    }
  }

  global.RoomViewAppClass = RoomViewApp;
})(window);
