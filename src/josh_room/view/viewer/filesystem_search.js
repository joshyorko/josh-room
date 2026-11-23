// Live filesystem search uses the same admitted, generation-bound pages as navigation.
(function (global) {
  'use strict';

  class SearchWalk {
    constructor(list, start, generation, recursive, query) {
      this.list = list;
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
        const result = await this.list(this.pending[0], this.generation, this.cursor, signal);
        signal.throwIfAborted();
        if (result.generation !== this.generation) throw new Error('Observation changed');
        const ancestors = [...result.ancestors, result.directory];
        const path = ancestors.map(node => node.label).filter(label => label && label !== '/');
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
    constructor({ list, inspect, select, directory, root, generation }) {
      this.list = list;
      this.inspect = inspect;
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
      this.displayed = null;
      this.bar.replaceChildren();
      this.bar.hidden = true;
    }

    async validateDisplayed() {
      const hits = this.displayed;
      if (!hits?.length || this.validating === hits) return;
      const signal = this.controller.signal;
      this.validating = hits;
      try {
        for (const hit of hits) {
          if (signal.aborted || hits !== this.displayed) return;
          await this.inspect(hit.node.id, this.walk.generation, signal);
        }
      } catch (error) {
        if (signal.aborted || hits !== this.displayed || error.status === 429) return;
        this.invalidate();
        this.bar.hidden = false;
        this.status('Search results changed. Search again for current matches.');
        this.button('Restart search', () => this.start());
        throw error;
      } finally {
        if (this.validating === hits) this.validating = null;
      }
    }

    start() {
      this.invalidate();
      const query = this.input.value.trim();
      if (!query) return;
      this.bar.hidden = false;
      if (!this.generation()) {
        this.status('Waiting for this folder to load.');
        this.button('Retry search', () => this.start());
        return;
      }
      const start = this.scope.value === 'workspace' ? this.root() : this.directory();
      this.walk = new SearchWalk(this.list, start, this.generation(), this.scope.value !== 'directory', query);
      this.controller = new AbortController();
      this.advance();
    }

    button(label, act) {
      const element = document.createElement('button');
      element.type = 'button';
      element.className = 'search-hit';
      element.textContent = label;
      element.addEventListener('click', act);
      this.bar.appendChild(element);
      return element;
    }

    status(label) {
      const element = document.createElement('span');
      element.setAttribute('role', 'status');
      element.textContent = label;
      this.bar.appendChild(element);
    }

    async advance() {
      this.displayed = null;
      const walk = this.walk;
      const signal = this.controller.signal;
      const hadFocus = this.bar.contains(document.activeElement);
      this.bar.replaceChildren();
      this.status('Searching live folder listings…');
      const cancel = this.button('Cancel search', () => {
        this.invalidate();
        this.input.focus();
      });
      if (hadFocus) cancel.focus();
      try {
        const {hits, more} = await walk.advance(signal);
        if (signal.aborted) return;
        this.displayed = hits;
        const moveFocus = this.bar.contains(document.activeElement);
        this.bar.replaceChildren();
        const coverage = more ? 'Partial search' : (walk.skipped ? 'Search ended with incomplete coverage' : 'Search complete for admitted entries');
        this.status(`${coverage}. ${walk.examined} entries observed, ${walk.folders} folder listings finished. ${hits.length} matches on this page.`);
        if (walk.skipped) this.status(`${walk.skipped} folder listings unavailable or beyond the search limit.`);
        for (const hit of hits) {
          this.button(hit.fullPath, () => {
            this.invalidate();
            this.select(hit.node, hit.ancestors);
          });
        }
        if (more) this.button('Continue search', () => this.advance());
        if (moveFocus) (this.bar.querySelector('button') || this.input).focus();
      } catch {
        if (signal.aborted) return;
        this.bar.replaceChildren();
        this.status('Search interrupted. The folder or its observation may have changed.');
        this.button('Restart search', () => this.start());
      }
    }
  }

  global.RoomViewFilesystemSearch = RoomViewFilesystemSearch;
  if (typeof module !== 'undefined' && module.exports) module.exports = { SearchWalk };
})(globalThis);
