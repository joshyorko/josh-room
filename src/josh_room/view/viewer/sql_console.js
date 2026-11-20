// Read-Only SQL Console over the embedded room-view-v1 SQLite database.
(function (global) {
  'use strict';

  class SqlConsoleView {
    constructor(dataLayer) {
      this.data = dataLayer;
      this.presetsEl = document.getElementById('sql-presets');
      this.inputEl = document.getElementById('sql-input');
      this.statusEl = document.getElementById('sql-status');
      this.resultsEl = document.getElementById('sql-results');
      this.runBtn = document.getElementById('btn-sql-run');
      this.downloadBtn = document.getElementById('btn-download-sqlite');
      this._bind();
    }

    _bind() {
      this.runBtn.addEventListener('click', () => this.execute());
      this.inputEl.addEventListener('keydown', (ev) => {
        if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') {
          ev.preventDefault();
          this.execute();
        }
      });
      this.downloadBtn.addEventListener('click', () => {
        const blob = this.data.getSqliteDownloadBlob();
        if (!blob) return;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'room-view-snapshot.sqlite';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
    }

    renderPresets(descriptor) {
      this.presetsEl.textContent = '';
      const presets = (descriptor && descriptor.sql_presets) || [];
      presets.forEach((preset, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'hud-btn';
        btn.textContent = preset.name;
        btn.title = preset.description || preset.sql;
        btn.setAttribute('data-preset-id', preset.id);
        btn.addEventListener('click', () => {
          this.inputEl.value = preset.sql;
          this.execute();
        });
        this.presetsEl.appendChild(btn);
        if (idx === 0 && !this.inputEl.dataset.userEdited) {
          this.inputEl.value = preset.sql;
        }
      });
    }

    execute(overrideSql) {
      if (typeof overrideSql === 'string') {
        this.inputEl.value = overrideSql;
      }
      this.resultsEl.textContent = '';
      try {
        const { results, elapsedMs } = this.data.runReadOnlySql(this.inputEl.value);
        if (!results || !results.length) {
          this.statusEl.textContent = `0 rows (${elapsedMs} ms)`;
          const p = document.createElement('p');
          p.className = 'muted-text';
          p.textContent = 'Query returned no rows.';
          this.resultsEl.appendChild(p);
          return { ok: true, rowCount: 0, elapsedMs };
        }
        const first = results[0];
        this.statusEl.textContent = `${first.values.length} row${first.values.length === 1 ? '' : 's'} (${elapsedMs} ms)`;

        const table = document.createElement('table');
        table.className = 'sql-table';
        const thead = document.createElement('thead');
        const hr = document.createElement('tr');
        for (const col of first.columns) {
          const th = document.createElement('th');
          th.textContent = col;
          hr.appendChild(th);
        }
        thead.appendChild(hr);
        table.appendChild(thead);

        const tbody = document.createElement('tbody');
        const cap = Math.min(500, first.values.length);
        for (let r = 0; r < cap; r += 1) {
          const tr = document.createElement('tr');
          for (const val of first.values[r]) {
            const td = document.createElement('td');
            td.textContent = val === null ? 'NULL' : (val instanceof Uint8Array ? `<BLOB ${val.byteLength} B>` : String(val));
            tr.appendChild(td);
          }
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        this.resultsEl.appendChild(table);
        return { ok: true, rowCount: first.values.length, columns: first.columns, elapsedMs };
      } catch (err) {
        this.statusEl.textContent = err && err.message ? err.message : 'SQL error';
        return { ok: false, error: this.statusEl.textContent };
      }
    }
  }

  global.RoomViewSql = { SqlConsoleView };
})(window);
