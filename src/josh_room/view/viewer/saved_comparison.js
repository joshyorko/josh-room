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
      this.positions = new Map();
    }

    capture() {
      if (!this.intent) return null;
      return {
        ...this.intent,
        top: this.provenance ? this.panel.scrollTop : this.intent.top,
        expanded: this.provenance ? this.provenance.open : this.intent.expanded,
        diffLeft: this.diff ? this.diff.scrollLeft : this.intent.diffLeft,
      };
    }

    select(target, reading = null) {
      const id = this.data.comparison && target && ['file', 'artifact'].includes(target.nodeKind)
        ? target.objectId : null;
      const generation = this.data.fsGeneration;
      this.tab.hidden = !id || !generation;
      if (!this.tab.hidden && this.target?.id === id && this.target.generation === generation) return;
      this.close();
      if (this.generation !== generation) this.positions.clear();
      this.generation = generation;
      if (this.target?.generation === generation) {
        this.positions.delete(this.target.id);
        if (this.intent) this.positions.set(this.target.id, this.intent);
        if (this.positions.size > 16) this.positions.delete(this.positions.keys().next().value);
      }
      this.intent = id && reading?.id === id && reading.generation === generation
        ? reading : this.positions.get(id) || null;
      this.target = this.tab.hidden ? null : { id, generation };
    }

    close() {
      this.intent = this.capture();
      this.provenance = null;
      this.diff = null;
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
      const previous = this.capture();
      this.intent = previous;
      this.diff = null;
      this.provenance = null;
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
        this.intent = null;
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
      const remember = point => point && {
        ...this.target, point: { id: point.id, snapshot: point.snapshot, date: point.date },
        compared: false, top: 0, diffLeft: 0, expanded: false,
      };
      select.addEventListener('change', () => {
        this.pending?.abort();
        this.intent = remember(result.points.find(entry => entry.id === select.value));
        this.provenance = this.diff = null;
        output.replaceChildren();
        status.textContent = '';
        compare.disabled = !select.value;
      });
      const comparePoint = async (restore = null) => {
        const point = result.points.find(entry => entry.id === select.value);
        if (!point) return;
        this.intent = remember(point);
        this.provenance = this.diff = null;
        status.textContent = restore ? 'Checking the saved and live versions…' : 'Comparing the selected file…';
        output.replaceChildren();
        const receipt = await this.request('compare', { point: point.id }, status);
        if (!receipt) return;
        if (receipt.snapshot !== point.snapshot || receipt.date !== point.date) {
          status.textContent = 'The selected recovery point changed. Reload the choices.';
          return;
        }
        const verified = receipt.live.sha256 && (!receipt.saved || receipt.saved.sha256);
        const identity = verified ? JSON.stringify([receipt.workspace_snapshot_id, receipt.tree_id,
          receipt.saved?.sha256 || null, receipt.live.sha256, receipt.change, receipt.path, receipt.diff_status]) : null;
        if (restore && (!identity || identity !== restore.identity)) {
          status.textContent = !identity
            ? 'The available digests are insufficient to confirm the previous result. Compare again for current metadata.'
            : 'The previous comparison could not be verified unchanged. Compare again to read the current result.';
          return;
        }
        this.intent = { ...this.intent, compared: true, identity };
        const descriptions = {
          modified: 'Modified · saved and live files differ.',
          unchanged: 'Unchanged · saved and live SHA-256 digests match.',
          added: 'Added in the live Room · this path was absent at the selected point.',
          unknown: 'Contents unknown · the files have equal sizes and a digest is unavailable.',
        };
        status.textContent = descriptions[receipt.change] || 'Comparison status unavailable.';
        const sides = element('div');
        sides.className = 'comparison-sides';
        const side = (label, date, info) => {
          const column = element('section');
          column.append(element('h3', label), element('p', date),
            element('p', info ? info.size + ' bytes' : 'File absent'));
          sides.append(column);
        };
        side('Saved', receipt.date, receipt.saved);
        side('Live', 'Observed ' + receipt.live_observed_at, receipt.live);
        output.append(sides);
        const provenance = element('details');
        this.provenance = provenance;
        provenance.append(element('summary', 'Recovery point and file digests'));
        const facts = element('dl');
        const field = (name, value) => facts.append(element('dt', name), element('dd', value));
        field('Compared path', receipt.path || 'Withheld by the Room display policy');
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
        provenance.append(facts);
        if (receipt.unified_diff) {
          const diff = element('pre', receipt.unified_diff);
          diff.setAttribute('aria-label', 'Saved to live text diff');
          this.diff = diff;
          output.append(diff);
        } else {
          const textStatus = {
            'size-limit': 'The file exceeds the text comparison size limit.',
            'text-withheld': 'Text comparison is unavailable under the preview and sanitization policy.',
            'preview-disabled': 'Text previews are disabled for this Room view.',
          };
          if (textStatus[receipt.diff_status]) output.append(element('p', textStatus[receipt.diff_status]));
        }
        output.append(provenance);
        const continuation = element('details');
        continuation.append(element('summary', 'Continue in Time Machine'),
          element('p', 'In the VS Code Josh Room sidebar, select this Room and its saved recovery point ' +
            point.snapshot + ' (' + point.date + '). Choose Josh Room: Browse Recovery Point…' +
            (receipt.path ? ' and navigate to ' + receipt.path + '.' : '. The path is withheld in this view.')));
        output.append(continuation);
        if (restore) {
          provenance.open = restore.expanded;
          if (this.diff) this.diff.scrollLeft = restore.diffLeft;
          this.panel.scrollTop = restore.top;
        }
      };
      compare.addEventListener('click', () => comparePoint());
      status.textContent = result.portable ? 'Portable snapshots cannot be compared here.' : '';
      this.panel.replaceChildren(label, compare, status, output, reload);
      if (previous) {
        const pin = result.points.find(entry => entry.id === previous.point.id &&
          entry.snapshot === previous.point.snapshot && entry.date === previous.point.date);
        if (!pin) {
          this.intent = null;
          status.textContent = 'The selected recovery point is no longer available. Choose a point to compare.';
        } else {
          select.value = pin.id;
          compare.disabled = false;
          if (previous.compared) await comparePoint(previous);
        }
      }
    }
  }

  global.RoomSavedComparison = SavedComparison;
})(globalThis);
