(function (global) {
  'use strict';

  function element(tag, text) {
    const el = document.createElement(tag);
    if (text != null) el.textContent = text;
    return el;
  }

  class SavedComparison {
    constructor(data, panel, tab) {
      this.data = data;
      this.panel = panel;
      this.tab = tab;
      this.target = null;
      this.pending = null;
      this.active = false;
    }

    select(target) {
      const id = this.data.comparison && target && ['file', 'artifact'].includes(target.nodeKind)
        ? target.objectId : null;
      const generation = this.data.fsGeneration;
      this.tab.hidden = !id || !generation;
      if (!this.tab.hidden && this.target?.id === id && this.target.generation === generation) return;
      this.close();
      this.target = this.tab.hidden ? null : { id, generation };
    }

    close() {
      this.pending?.abort();
      this.pending = null;
      this.active = false;
      this.panel.replaceChildren();
      this.panel.hidden = true;
    }

    open() {
      if (!this.target || this.active) return;
      this.active = true;
      this.panel.hidden = false;
      this.loadPoints();
    }

    async request(operation, extra, status) {
      this.pending?.abort();
      const pending = new AbortController();
      this.pending = pending;
      try {
        const value = await this.data.queryFilesystem(operation, { ...this.target, ...extra }, pending.signal);
        if (!pending.signal.aborted) {
          if (this.target?.generation === this.data.fsGeneration) return value;
          status.textContent = 'The Room changed. Reopen the file before comparing.';
        }
      } catch (err) {
        if (!pending.signal.aborted) {
          const reasons = {
            'comparison-unsupported': 'This recovery format does not support selective comparison.',
            'comparison-unavailable': 'Could not read this recovery point. Check the Room connection and try again.',
            'comparison-stale': 'The selected recovery point changed. Reload the choices.',
            'comparison-busy': 'Another comparison is running. Try again.',
          };
          status.textContent = reasons[err.code] || ({
            403: 'Access to this comparison was denied.',
            404: 'The selected file or recovery point is unavailable.',
            409: 'The Room or selection changed. Reopen the file and choose a recovery point.',
            413: 'This file exceeds the comparison limit.',
            422: 'Comparison is unavailable for this file or recovery format.',
            429: 'Another query is running. Try again.',
          })[err.status] || 'Could not read the recovery point. Check the Room connection and try again.';
        }
      } finally {
        if (this.pending === pending) this.pending = null;
      }
      return null;
    }

    async loadPoints() {
      const status = element('p', 'Reading available recovery points…');
      status.setAttribute('role', 'status');
      const reload = element('button', 'Reload recovery points');
      reload.type = 'button';
      reload.className = 'hud-btn';
      reload.addEventListener('click', () => this.loadPoints());
      this.panel.replaceChildren(status, reload);
      const result = await this.request('points', {}, status);
      if (!result) return;
      if (!result.points.length) {
        status.textContent = result.portable ? 'Only portable snapshots are available. They do not support selective comparison.'
          : 'No saved recovery points are available for this Room.';
        return;
      }
      const label = element('label', 'Saved recovery point');
      const select = element('select');
      select.className = 'hud-btn saved-point-select';
      const prompt = element('option', 'Choose a recovery point…');
      prompt.value = '';
      select.append(prompt);
      for (const point of result.points) {
        const option = element('option', point.date + ' · ' + point.snapshot);
        option.value = point.id;
        select.append(option);
      }
      label.append(select);
      const compare = element('button', 'Compare with live file');
      compare.type = 'button';
      compare.className = 'hud-btn';
      compare.disabled = true;
      const output = element('div');
      output.className = 'saved-comparison-result';
      select.addEventListener('change', () => {
        this.pending?.abort();
        output.replaceChildren();
        status.textContent = '';
        compare.disabled = !select.value;
      });
      compare.addEventListener('click', async () => {
        const point = result.points.find(entry => entry.id === select.value);
        if (!point) return;
        status.textContent = 'Comparing the selected file…';
        output.replaceChildren();
        const receipt = await this.request('compare', { point: point.id }, status);
        if (!receipt) return;
        if (receipt.snapshot !== point.snapshot || receipt.date !== point.date) {
          status.textContent = 'The selected recovery point changed. Reload the choices.';
          return;
        }
        status.textContent = 'Comparison at time of request · ' + receipt.change;
        const facts = element('dl');
        const field = (name, value) => facts.append(element('dt', name), element('dd', value));
        field('Saved recovery point', receipt.date + ' · ' + receipt.snapshot);
        field('Workspace snapshot', receipt.workspace_snapshot_id);
        field('Saved tree', receipt.tree_id);
        if (receipt.saved) {
          field('Saved size', receipt.saved.size + ' bytes');
          field('Saved SHA-256', receipt.saved.sha256 || 'Unavailable within the read limit');
        } else {
          field('Saved file', 'Absent at this point');
        }
        field('Live size', receipt.live.size + ' bytes');
        field('Live SHA-256', receipt.live.sha256 || 'Unavailable within the read limit');
        output.append(facts);
        if (receipt.unified_diff) {
          const diff = element('pre', receipt.unified_diff);
          diff.setAttribute('aria-label', 'Saved to live text diff');
          output.append(diff);
        }
      });
      status.textContent = result.portable ? 'Portable snapshots cannot be compared here.' : '';
      this.panel.replaceChildren(label, compare, status, output, reload);
    }
  }

  global.RoomSavedComparison = SavedComparison;
})(globalThis);
