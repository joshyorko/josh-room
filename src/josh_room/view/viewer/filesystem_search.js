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
      this.matches = [];
    }

    async advance(signal) {
      const hits = this.matches.splice(0, 16);
      for (let pages = 0; this.pending.length && pages < 8 && hits.length < 16; pages++) {
        let result;
        try {
          result = await this.list(this.pending[0], this.generation, this.cursor, signal);
        } catch (error) {
          if (error.status === 429 && !signal.aborted) break;
          throw error;
        }
        signal.throwIfAborted();
        if (result.generation !== this.generation) throw new Error('Observation changed');
        const ancestors = [...result.ancestors, result.directory];
        const path = ancestors.map(node => node.label).filter(label => label && label !== '/');
        if (this.startPath === null) this.startPath = '/' + path.join('/');
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
      this.input.addEventListener('input', () => this.start(this.walk?.start));
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
      const start = this.walk.start;
      this.invalidate();
      this.bar.hidden = false;
      this.status('Search results changed. Recheck the original scope to search again.');
      this.button('Restart search', () => this.start(start));
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

    start(boundStart) {
      this.invalidate();
      const query = this.input.value.trim();
      if (!query) return;
      this.bar.hidden = false;
      if (!this.generation()) {
        this.status('Waiting for this folder to load.');
        this.button('Retry search', () => this.start(boundStart));
        return;
      }
      const start = boundStart ?? (this.scope.value === 'workspace' ? this.root() : this.directory());
      this.scopeTitle = this.scope.selectedOptions[0].textContent;
      this.walk = new SearchWalk(this.list, start, this.generation(), this.scope.value !== 'directory', query);
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

    jump(index) {
      const hit = this.displayed[index];
      if (!hit || this.busy) return;
      this.active = hit;
      this.expanded = false;
      this.render();
      return this.select(hit.node, hit.ancestors);
    }

    render() {
      const walk = this.walk;
      const active = this.active;
      const index = active ? this.displayed.indexOf(active) : -1;
      const focused = this.bar.contains(document.activeElement) ? document.activeElement.textContent : null;
      this.bar.replaceChildren();
      const total = this.offset + this.displayed.length;
      const coverage = this.more ? 'Partial search' : (walk.skipped ? 'Incomplete coverage' : 'Search complete');
      const scope = walk.startPath ? `${this.scopeTitle} ${walk.startPath}` : this.scopeTitle;
      this.status(`${scope} · ${coverage} · ${total} matches · ${walk.examined} entries observed`);
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
      const next = action('Next match', () => {
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
      const ids = this.displayed.map(hit => hit.node.id);
      if (active && index < 0) ids.unshift(active.node.id);
      this.onMatches?.(ids, active?.node.id ?? null);
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
        this.busy = false;
        if (jumpToNext && hits.length) return this.jump(this.displayed.length - hits.length);
        this.render();
      } catch {
        if (!signal.aborted) this.observationLost();
      }
    }
  }

  global.RoomViewFilesystemSearch = RoomViewFilesystemSearch;
  if (typeof module !== 'undefined' && module.exports) module.exports = { SearchWalk };
})(globalThis);
