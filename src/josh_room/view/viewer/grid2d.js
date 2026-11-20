// Accessible 2D Grid Fallback & Printable Triage Table.
(function (global) {
  'use strict';

  class GridFallbackView {
    constructor(contentEl, summaryEl, printTableEl, callbacks) {
      this.contentEl = contentEl;
      this.summaryEl = summaryEl;
      this.printTableEl = printTableEl;
      this.callbacks = callbacks || {};
      this.filterKind = 'all';
      this.searchQuery = '';
      this._bindFilterChips();
    }

    _bindFilterChips() {
      document.querySelectorAll('.filter-chip').forEach((btn) => {
        btn.addEventListener('click', () => {
          document.querySelectorAll('.filter-chip').forEach((b) => b.classList.remove('active'));
          btn.classList.add('active');
          this.filterKind = btn.getAttribute('data-filter') || 'all';
          if (this.descriptor && this.state) {
            this.render(this.descriptor, this.state);
          }
        });
      });
    }

    setSearch(query) {
      this.searchQuery = (query || '').trim().toLowerCase();
      if (this.descriptor && this.state) {
        this.render(this.descriptor, this.state);
      }
    }

    render(descriptor, state) {
      this.descriptor = descriptor;
      this.state = state;
      const stages = (descriptor.stages || []).slice().sort((a, b) => a.order - b.order);
      const groups = (state.groups || []).slice().sort((a, b) => (a.order || 0) - (b.order || 0));
      const allItems = state.items || [];
      const metricMax = Math.max(1, ...allItems.map((it) => (typeof it.metric === 'number' ? it.metric : 0)));
      const unit = descriptor.metric && descriptor.metric.unit ? ` ${descriptor.metric.unit}` : '';

      const matches = allItems.filter((item) => {
        if (this.filterKind !== 'all' && item.kind !== this.filterKind) return false;
        if (!this.searchQuery) return true;
        const hay = `${item.id} ${item.label} ${item.group} ${item.stage} ${item.kind}`.toLowerCase();
        return hay.includes(this.searchQuery);
      });

      const counts = global.RoomViewLayout.outcomeCounts(matches);
      this.summaryEl.textContent = `${counts.total} shown · ${counts.pass} pass · ${counts.fail} fail · ${counts.skip} skip`;

      this.contentEl.textContent = '';
      for (const group of groups) {
        const gItems = matches.filter((it) => it.group === group.id);
        if (!gItems.length) continue;

        const section = document.createElement('section');
        section.className = 'grid-group-section';

        const header = document.createElement('div');
        header.className = 'grid-group-header';
        const h3 = document.createElement('h3');
        h3.textContent = `${group.name} (${gItems.length})`;
        const gCounts = global.RoomViewLayout.outcomeCounts(gItems);
        const stat = document.createElement('span');
        stat.className = 'muted-text';
        stat.textContent = `Pass ${gCounts.pass} · Fail ${gCounts.fail} · Skip ${gCounts.skip}`;
        header.append(h3, stat);
        section.appendChild(header);

        for (const stage of stages) {
          const sItems = gItems.filter((it) => it.stage === stage.id);
          if (!sItems.length) continue;

          const row = document.createElement('div');
          row.className = 'grid-stage-row';
          const stTitle = document.createElement('div');
          stTitle.className = 'grid-stage-title';
          stTitle.textContent = `${stage.name} (${sItems.length})`;
          row.appendChild(stTitle);

          const tiles = document.createElement('div');
          tiles.className = 'grid-tiles';

          for (const item of sItems) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = `grid-tile kind-tile-${item.kind}`;
            btn.setAttribute('data-item-id', item.id);

            const cue = item.kind === 'fail' ? '× FAIL' : (item.kind === 'skip' ? '○ SKIP' : '● PASS');
            const head = document.createElement('div');
            head.className = 'grid-tile-head';
            const lbl = document.createElement('span');
            lbl.textContent = item.label;
            const kindSpan = document.createElement('span');
            kindSpan.textContent = cue;
            head.append(lbl, kindSpan);

            const meta = document.createElement('div');
            meta.className = 'grid-tile-meta';
            const attCount = (item.attachments || []).length;
            meta.textContent = `${item.id}${item.metric != null ? ` · ${item.metric}${unit}` : ''}${attCount ? ` · ${attCount} doc${attCount > 1 ? 's' : ''}` : ''}`;

            const bar = document.createElement('div');
            bar.className = 'grid-metric-bar';
            const fill = document.createElement('div');
            fill.className = 'grid-metric-fill';
            const pct = item.metric != null ? Math.min(100, Math.max(4, (item.metric / metricMax) * 100)) : 8;
            fill.style.width = `${pct}%`;
            bar.appendChild(fill);

            btn.append(head, meta, bar);
            btn.addEventListener('click', () => {
              if (this.callbacks.onSelectItem) this.callbacks.onSelectItem(item.id);
            });
            tiles.appendChild(btn);
          }

          row.appendChild(tiles);
          section.appendChild(row);
        }
        this.contentEl.appendChild(section);
      }

      this._renderPrintTable(descriptor, state);
    }

    _renderPrintTable(descriptor, state) {
      if (!this.printTableEl) return;
      const unit = descriptor.metric && descriptor.metric.unit ? ` (${descriptor.metric.unit})` : '';
      const table = document.createElement('table');
      const thead = document.createElement('thead');
      const hr = document.createElement('tr');
      for (const col of ['ID', descriptor.group_label || 'Group', 'Stage', 'Outcome', 'Label', `Metric${unit}`, 'Attachments']) {
        const th = document.createElement('th');
        th.textContent = col;
        hr.appendChild(th);
      }
      thead.appendChild(hr);
      table.appendChild(thead);

      const tbody = document.createElement('tbody');
      const sorted = global.RoomViewLayout.sortItemsByInterest(state.items || []);
      for (const item of sorted) {
        const tr = document.createElement('tr');
        const cue = item.kind === 'fail' ? '× fail' : (item.kind === 'skip' ? '○ skip' : '● pass');
        const cells = [
          item.id,
          item.group,
          item.stage,
          cue,
          item.label,
          item.metric != null ? String(item.metric) : '—',
          String((item.attachments || []).length),
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
