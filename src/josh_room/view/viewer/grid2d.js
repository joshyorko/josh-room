// Accessible 2D Grid Fallback & Printable Triage Table.
// Honors arbitrary-depth hierarchies AND provider-declared custom legends.
(function (global) {
  'use strict';

  class GridFallbackView {
    constructor(contentEl, summaryEl, printTableEl, callbacks) {
      this.contentEl = contentEl;
      this.summaryEl = summaryEl;
      this.printTableEl = printTableEl;
      this.filtersEl = document.getElementById('grid-filters');
      this.callbacks = callbacks || {};
      this.filterKind = 'all';
      this.searchQuery = '';
      this._renderedLegendKey = '';
    }

    _syncFilterChips(descriptor) {
      if (!this.filtersEl) return;
      const entries = global.RoomViewLayout.getLegendEntries(descriptor);
      const sig = entries.map((e) => `${e.kind}:${e.label}:${e.color}:${e.shape}`).join('|');
      if (sig === this._renderedLegendKey) return;
      this._renderedLegendKey = sig;
      if (this.filterKind !== 'all' && !entries.some((e) => e.kind === this.filterKind)) {
        this.filterKind = 'all';
      }
      this.filtersEl.textContent = '';

      const makeChip = (filterValue, text, colorHex) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `filter-chip${this.filterKind === filterValue ? ' active' : ''}`;
        btn.setAttribute('data-filter', filterValue);
        if (colorHex) {
          const dot = document.createElement('span');
          dot.style.color = colorHex;
          dot.style.marginRight = '4px';
          dot.textContent = text.slice(0, 1);
          btn.append(dot, document.createTextNode(text.slice(1).trim()));
        } else {
          btn.textContent = text;
        }
        btn.addEventListener('click', () => {
          this.filterKind = filterValue;
          this.filtersEl.querySelectorAll('.filter-chip').forEach((b) => {
            b.classList.toggle('active', b.getAttribute('data-filter') === filterValue);
          });
          if (this.descriptor && this.state && this.layout) {
            this.render(this.descriptor, this.state, this.layout);
          }
        });
        return btn;
      };

      this.filtersEl.appendChild(makeChip('all', 'All', null));
      for (const entry of entries) {
        this.filtersEl.appendChild(makeChip(entry.kind, `${entry.symbol} ${entry.label}`, entry.color));
      }
    }

    setSearch(query) {
      this.searchQuery = (query || '').trim().toLowerCase();
      if (this.descriptor && this.state && this.layout) {
        this.render(this.descriptor, this.state, this.layout);
      }
    }

    render(descriptor, state, layout) {
      this.descriptor = descriptor;
      this.state = state;
      this.layout = layout;
      this._syncFilterChips(descriptor);

      const allLeaves = (layout && layout.allLeaves) || [];
      const metricMax = Math.max(1, ...allLeaves.map((it) => (typeof it.metric === 'number' ? it.metric : 0)));
      const unit = descriptor.metric && descriptor.metric.unit ? ` ${descriptor.metric.unit}` : '';

      const matches = allLeaves.filter((leaf) => {
        if (this.filterKind !== 'all' && leaf.kind !== this.filterKind) return false;
        if (!this.searchQuery) return true;
        const entry = global.RoomViewLayout.getLegendEntry(descriptor, leaf.kind);
        const hay = `${leaf.objectId} ${leaf.label} ${leaf.path} ${leaf.kind} ${entry.label} ${leaf.summary || ''}`.toLowerCase();
        return hay.includes(this.searchQuery);
      });

      const counts = global.RoomViewLayout.outcomeCounts(matches, descriptor);
      this.summaryEl.textContent = global.RoomViewLayout.formatCountsSummary(counts, descriptor);

      // Group matching leaves by their parent directory path (preserving tree order)
      const byParentPath = new Map();
      for (const leaf of matches) {
        const key = leaf.parentPath || '/';
        if (!byParentPath.has(key)) byParentPath.set(key, []);
        byParentPath.get(key).push(leaf);
      }

      this.contentEl.textContent = '';
      for (const [dirPath, leaves] of byParentPath.entries()) {
        const section = document.createElement('section');
        section.className = 'grid-group-section';

        const header = document.createElement('div');
        header.className = 'grid-group-header';
        const h3 = document.createElement('h3');
        h3.textContent = `${dirPath} (${leaves.length})`;
        const dirCounts = global.RoomViewLayout.outcomeCounts(leaves, descriptor);
        const stat = document.createElement('span');
        stat.className = 'muted-text';
        stat.textContent = global.RoomViewLayout.formatCountsSummary(dirCounts, descriptor);
        header.append(h3, stat);
        section.appendChild(header);

        const tiles = document.createElement('div');
        tiles.className = 'grid-tiles';

        for (const leaf of leaves) {
          const entry = global.RoomViewLayout.getLegendEntry(descriptor, leaf.kind);
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'grid-tile';
          btn.style.borderLeftColor = entry.color;
          btn.style.borderLeftStyle = entry.shape === 'hollow' ? 'dashed' : 'solid';
          btn.setAttribute('data-item-id', leaf.objectId);
          btn.setAttribute('data-kind', leaf.kind);

          const head = document.createElement('div');
          head.className = 'grid-tile-head';
          const lbl = document.createElement('span');
          lbl.textContent = leaf.label;
          const kindSpan = document.createElement('span');
          kindSpan.style.color = entry.color;
          kindSpan.textContent = `${entry.symbol} ${entry.label}`;
          head.append(lbl, kindSpan);

          const meta = document.createElement('div');
          meta.className = 'grid-tile-meta';
          const attCount = (leaf.attachments || []).length;
          const prevCount = (leaf.previews || []).length;
          const linkCount = (leaf.links || []).length;
          const bits = [];
          if (leaf.metric != null) bits.push(`${leaf.metric}${unit}`);
          if (prevCount) bits.push(`${prevCount} preview${prevCount > 1 ? 's' : ''}`);
          if (attCount) bits.push(`${attCount} doc${attCount > 1 ? 's' : ''}`);
          if (linkCount) bits.push(`${linkCount} link${linkCount > 1 ? 's' : ''}`);
          meta.textContent = bits.length ? bits.join(' · ') : leaf.objectId;

          const bar = document.createElement('div');
          bar.className = 'grid-metric-bar';
          const fill = document.createElement('div');
          fill.className = 'grid-metric-fill';
          fill.style.background = entry.color;
          const pct = leaf.metric != null ? Math.min(100, Math.max(4, (leaf.metric / metricMax) * 100)) : 8;
          fill.style.width = `${pct}%`;
          bar.appendChild(fill);

          btn.append(head, meta, bar);
          btn.addEventListener('click', () => {
            if (this.callbacks.onSelectItem) this.callbacks.onSelectItem(leaf.objectId);
          });
          tiles.appendChild(btn);
        }

        section.appendChild(tiles);
        this.contentEl.appendChild(section);
      }

      this._renderPrintTable(descriptor, allLeaves);
    }

    _renderPrintTable(descriptor, allLeaves) {
      if (!this.printTableEl) return;
      const unit = descriptor.metric && descriptor.metric.unit ? ` (${descriptor.metric.unit})` : '';
      const table = document.createElement('table');
      const thead = document.createElement('thead');
      const hr = document.createElement('tr');
      for (const col of ['Path', 'Type', 'Outcome', `Metric${unit}`, 'Previews', 'Attachments']) {
        const th = document.createElement('th');
        th.textContent = col;
        hr.appendChild(th);
      }
      thead.appendChild(hr);
      table.appendChild(thead);

      const tbody = document.createElement('tbody');
      const sorted = global.RoomViewLayout.sortItemsByInterest(allLeaves, descriptor);
      for (const leaf of sorted) {
        const tr = document.createElement('tr');
        const entry = global.RoomViewLayout.getLegendEntry(descriptor, leaf.kind);
        const cells = [
          leaf.path,
          leaf.nodeKind || 'file',
          `${entry.symbol} ${entry.label}`,
          leaf.metric != null ? String(leaf.metric) : '—',
          String((leaf.previews || []).length),
          String((leaf.attachments || []).length),
        ];
        for (const text of cells) {
          const td = document.createElement('td');
          td.textContent = text;
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      this.printTableEl.textContent = '';
      this.printTableEl.appendChild(table);
    }
  }

  global.RoomViewGrid = { GridFallbackView };
})(window);
