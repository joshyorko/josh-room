// Live filesystem search uses the same admitted, generation-bound pages as navigation.
(function (global) {
  'use strict';

  class SearchWalk {
    constructor(list, start, generation, recursive, query) {
      this.list = list;
      this.start = start;
      this.startPath = null;
      this.pending = [start];
      this.generation = generation;
      this.recursive = recursive;
      this.query = query.toLowerCase();
      this.cursor = null;
      this.folders = 0;
      this.examined = 0;
      this.skipped = 0;
      this.omitted = 0;
      this.directoryOmitted = 0;
      this.matches = [];
    }

    async advance(signal) {
      const hits = this.matches.splice(0, 16);
      for (let pages = 0; this.pending.length && pages < 8 && hits.length < 16; pages++) {
        let result;
        const reopening = this.reopenIndex != null;
        const directory = reopening ? this.reopenChain[this.reopenIndex] : this.pending[0];
        try {
          result = await this.list(directory, this.generation, this.cursor, signal);
        } catch (error) {
          if (error.status === 429 && !signal.aborted) break;
          throw error;
        }
        signal.throwIfAborted();
        if (result.generation !== this.generation) throw new Error('Observation changed');
        if (reopening) {
          const next = this.reopenChain[this.reopenIndex + 1];
          if (result.entries.some(node => node.id === next && node.node_kind === 'directory')) {
            this.cursor = null;
            if (++this.reopenIndex === this.reopenChain.length - 1) this.reopenIndex = null;
          } else if (result.cursor) this.cursor = result.cursor;
          else throw Object.assign(new Error('Original search folder unavailable'), { scopeLost: true });
          continue;
        }
        const ancestors = [...result.ancestors, result.directory];
        const path = ancestors.map(node => node.label).filter(label => label && label !== '/');
        if (this.startPath === null) {
          this.startPath = '/' + path.join('/');
          this.startChain = ancestors.map(node => node.id);
        }
        const omitted = result.omitted || 0;
        this.omitted += omitted - this.directoryOmitted;
        this.directoryOmitted = result.cursor ? omitted : 0;
        const matches = [];
        this.cursor = result.cursor;
        if (!this.cursor) {
          this.pending.shift();
          this.folders++;
          if (result.coverage !== 'complete') this.skipped++;
        }
        for (const node of result.entries) {
          this.examined++;
          if (this.recursive && node.node_kind === 'directory') {
            if (this.pending.length < 2048) this.pending.push(node.id);
            else this.skipped++;
          }
          const fullPath = '/' + [...path, node.label].join('/');
          if (fullPath.toLowerCase().includes(this.query)) matches.push({ node, ancestors, fullPath });
        }
        hits.push(...matches.splice(0, 16 - hits.length));
        this.matches.push(...matches);
      }
      return { hits, more: Boolean(this.pending.length || this.matches.length) };
    }
  }

  class RoomViewFilesystemSearch {
    constructor({ list, inspect, select, directory, root, generation, onMatches }) {
      this.list = list;
      this.inspect = inspect;
      this.onMatches = onMatches;
      this.select = select;
      this.directory = directory;
      this.root = root;
      this.generation = generation;
      this.input = document.getElementById('search-input');
      this.bar = document.getElementById('search-results-bar');
      this.scope = document.getElementById('fs-search-scope');
      this.scope.hidden = false;
      this.input.placeholder = 'Search by name or path';
      this.input.addEventListener('input', () => this.start());
      this.scope.addEventListener('change', () => this.start());
      this.input.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
          event.preventDefault();
          this.input.value = '';
          this.invalidate();
        }
      });
    }

    invalidate() {
      this.controller?.abort();
      this.walk = null;
      this.displayed = [];
      this.active = null;
      this.lastOrdinal = 0;
      this.offset = 0;
      this.more = false;
      this.busy = false;
      this.expanded = true;
      this.onMatches?.([], null);
      this.bar.replaceChildren();
      this.bar.hidden = true;
    }

    observationLost() {
      if (!this.walk) return;
      const chain = this.walk.startChain || this.walk.reopenChain;
      this.invalidate();
      this.bar.hidden = false;
      this.status('Search results changed. Recheck the original scope to search again.');
      this.button('Restart search', () => this.start(chain?.at(-1), chain));
    }

    async validateDisplayed() {
      const hits = this.displayed;
      if (!this.walk || this.validating === hits) return;
      const signal = this.controller.signal;
      this.validating = hits;
      try {
        if (this.generation() !== this.walk.generation) throw new Error('Observation changed');
        const ids = new Set([this.walk.start, ...hits.map(hit => hit.node.id)]);
        if (this.active) ids.add(this.active.node.id);
        for (const id of ids) {
          if (signal.aborted || hits !== this.displayed) return;
          await this.inspect(id, this.walk.generation, signal);
        }
      } catch (error) {
        if (signal.aborted || hits !== this.displayed || error.status === 429) return;
        this.observationLost();
        throw error;
      } finally {
        if (this.validating === hits) this.validating = null;
      }
    }

    start(boundStart, chain) {
      this.invalidate();
      const query = this.input.value.trim();
      if (!query) return;
      this.bar.hidden = false;
      if (!this.generation()) {
        this.status('Waiting for this folder to load.');
        this.button('Retry search', () => this.start(boundStart, chain));
        return;
      }
      const start = boundStart ?? (this.scope.value === 'workspace' ? this.root() : this.directory());
      this.scopeTitle = this.scope.selectedOptions[0].textContent;
      this.walk = new SearchWalk(this.list, start, this.generation(), this.scope.value !== 'directory', query);
      if (chain?.length > 1) {
        this.walk.reopenChain = chain;
        this.walk.reopenIndex = 0;
      }
      this.controller = new AbortController();
      return this.advance();
    }

    button(label, act, parent = this.bar) {
      const element = document.createElement('button');
      element.type = 'button';
      element.className = 'search-hit';
      element.textContent = label;
      element.addEventListener('click', act);
      parent.appendChild(element);
      return element;
    }

    status(label) {
      const element = document.createElement('span');
      element.setAttribute('role', 'status');
      element.textContent = label;
      this.bar.appendChild(element);
    }

    syncSelection(id) {
      this.selectedId = id;
      if (!this.walk || !this.updateActive()) return;
      if (this.busy) this.publishMatches();
      else this.render();
    }

    updateActive() {
      const hit = this.displayed.find(hit => hit.node.id === this.selectedId)
        || (this.active?.node.id === this.selectedId ? this.active : null);
      if (hit === this.active) return false;
      this.active = hit;
      if (hit) this.lastOrdinal = hit.ordinal;
      return true;
    }

    publishMatches() {
      const hits = this.active && !this.displayed.includes(this.active)
        ? [this.active, ...this.displayed] : this.displayed;
      this.onMatches?.(hits.map(hit => hit.node.id), this.active?.node.id ?? null,
        hits.map(hit => [hit.node.id, hit.ancestors.map(node => node.id)]));
    }

    jump(index) {
      const hit = this.displayed[index];
      if (!hit || this.busy) return;
      this.expanded = false;
      this.render();
      return this.select(hit.node, hit.ancestors);
    }

    render() {
      const walk = this.walk;
      const active = this.active;
      const index = this.displayed.findIndex(hit => hit.ordinal === this.lastOrdinal);
      const focused = this.bar.contains(document.activeElement) ? document.activeElement.textContent : null;
      this.bar.replaceChildren();
      const total = this.offset + this.displayed.length;
      const coverage = this.more ? 'Partial search' : (walk.skipped ? 'Incomplete coverage' : 'Search complete for admitted entries');
      const scope = walk.startPath ? `${this.scopeTitle} ${walk.startPath}` : this.scopeTitle;
      this.status(`${scope} · ${coverage} · ${total} matches · ${walk.examined} admitted entries · ${walk.omitted} omitted`);
      if (walk.skipped) this.status(`${walk.skipped} folders unavailable or beyond the search limit.`);
      if (active) this.status(`Match ${active.ordinal}: ${active.fullPath}`);
      const controls = document.createElement('div');
      controls.className = 'search-controls';
      this.bar.appendChild(controls);
      const action = (label, fn, disabled = false) => {
        const button = this.button(label, fn, controls);
        button.disabled = disabled;
        return button;
      };
      const prev = action('Previous match', () => this.jump(index - 1), index <= 0);
      const nextLabel = this.lastOrdinal && index < 0 && this.displayed.length
        ? `Jump to retained match ${this.displayed[0].ordinal}` : 'Next match';
      const next = action(nextLabel, () => {
        if (index + 1 < this.displayed.length) this.jump(index + 1);
        else this.advance(true);
      }, index + 1 >= this.displayed.length && !this.more);
      if (this.displayed.length) {
        const toggle = action(this.expanded ? 'Hide results' : 'Show results', () => {
          this.expanded = !this.expanded;
          this.render();
        });
        toggle.setAttribute('aria-expanded', String(this.expanded));
      }
      if (this.more) action('Continue search', () => this.advance());
      action('Clear search', () => {
        this.input.value = '';
        this.invalidate();
        this.input.focus();
      });
      if (this.expanded) {
        if (this.offset) this.status(`Showing the latest ${this.displayed.length} matches. Refine the query to revisit earlier results.`);
        for (const [index, hit] of this.displayed.entries()) {
          const button = this.button(hit.fullPath, () => this.jump(index));
          if (hit === active) button.setAttribute('aria-current', 'true');
        }
      }
      this.publishMatches();
      if (focused) {
        const same = Array.from(controls.children).find(button => !button.disabled && button.textContent === focused);
        (same || (!next.disabled ? next : !prev.disabled ? prev : controls.lastChild)).focus();
      }
    }

    async advance(jumpToNext = false) {
      if (this.busy) return;
      this.busy = true;
      const walk = this.walk;
      const signal = this.controller.signal;
      const hadFocus = this.bar.contains(document.activeElement);
      this.bar.replaceChildren();
      this.status('Searching live folder listings…');
      const cancel = this.button('Cancel search', () => {
        this.input.value = '';
        this.invalidate();
        this.input.focus();
      });
      if (hadFocus) cancel.focus();
      try {
        const {hits, more} = await walk.advance(signal);
        if (signal.aborted) return;
        this.more = more;
        // Keep a bounded previous-match window while allowing discovery to continue.
        const priorCount = this.offset + this.displayed.length;
        hits.forEach((hit, index) => { hit.ordinal = priorCount + index + 1; });
        const merged = [...this.displayed, ...hits];
        const removed = Math.max(0, merged.length - 64);
        this.displayed = merged.slice(removed);
        this.offset += removed;
        this.updateActive();
        this.busy = false;
        if (jumpToNext && hits.length) return this.jump(this.displayed.length - hits.length);
        this.render();
      } catch (error) {
        if (signal.aborted) return;
        if (error.scopeLost) {
          this.invalidate();
          this.bar.hidden = false;
          this.status('The original search folder is unavailable. Choose a new search scope.');
          this.button('Search from here', () => this.start());
        } else this.observationLost();
      }
    }
  }

  global.RoomViewFilesystemSearch = RoomViewFilesystemSearch;
  if (typeof module !== 'undefined' && module.exports) module.exports = { SearchWalk };
})(globalThis);
