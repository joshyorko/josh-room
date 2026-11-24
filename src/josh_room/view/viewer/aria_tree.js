// Synchronized APG role="tree" accessible twin for arbitrary-depth hierarchies.
// Uses collision-free node IDs, declared legend entries, and single-handled key events.
(function (global) {
  'use strict';

  class AccessibleTreeTwin {
    constructor(rootEl, liveEl, callbacks) {
      this.rootEl = rootEl;
      this.liveEl = liveEl;
      this.callbacks = callbacks || {};
      this.expanded = new Set(['root']);
      this.selectedId = 'root';
      this.focusedId = 'root';
      this.flatOrder = [];
      this.nodeElements = new Map();
      this.modelById = new Map();
      this._bindKeyboard();
    }

    announce(message) {
      if (!this.liveEl) return;
      this.liveEl.textContent = '';
      requestAnimationFrame(() => {
        this.liveEl.textContent = message;
      });
    }

    build(descriptor, state, layout) {
      this.descriptor = descriptor;
      this.state = state;
      this.layout = layout;
      const restoreFocus = this.rootEl.contains(document.activeElement);
      this.rootEl.textContent = '';
      this.nodeElements.clear();
      this.modelById.clear();
      this.flatOrder = [];
      if (!layout || !layout.treeRoot) return;

      const buildModel = (dirNode, parentId) => {
        const isRoot = dirNode.id === 'root';
        const label = isRoot
          ? `/ (${descriptor.title || 'Room View'})`
          : global.RoomViewLayout.isWorkspace(descriptor)
            ? `${dirNode.label} (${dirNode.fields?.coverage || 'observed entries only'})`
            : `${dirNode.label} (${dirNode.counts ? dirNode.counts.total : 0})`;
        const model = {
          id: dirNode.id,
          label,
          path: dirNode.path,
          parentId,
          children: [],
          lazy: dirNode.fields?.coverage === 'not-examined' && !dirNode.dirChildren.length && !dirNode.leafChildren.length,
          nodeType: dirNode.nodeType,
        };
        this.modelById.set(model.id, model);

        // Child directories first (arbitrary depth!)
        for (const childDir of dirNode.dirChildren || []) {
          model.children.push(buildModel(childDir, dirNode.id));
        }

        // Direct leaf files/runs/artifacts on this directory
        const leaves = dirNode.leafChildren || [];
        if (leaves.length) {
          const ranked = global.RoomViewLayout.sortItemsByInterest(leaves, descriptor);
          const pedLayout = layout.nodeById.get(dirNode.id);
          const aggregated = pedLayout ? Boolean(pedLayout.aggregated) : (ranked.length > 16);
          const visible = aggregated ? ranked.filter(leaf => layout.itemNodeById.has(leaf.objectId)) : ranked;
          const hidden = aggregated ? ranked.filter(leaf => !layout.itemNodeById.has(leaf.objectId)) : [];

          visible.forEach((leaf, idx) => {
            const entry = global.RoomViewLayout.getLegendEntry(descriptor, leaf.kind);
            const m = {
              id: leaf.id,
              objectId: leaf.objectId,
              label: `${entry.symbol} ${leaf.label}`,
              meta: `${entry.label}${leaf.metric != null ? ` · ${leaf.metric}` : ''}`,
              parentId: dirNode.id,
              children: [],
              nodeType: 'item',
              setsize: leaves.length,
              posinset: idx + 1,
            };
            model.children.push(m);
            this.modelById.set(m.id, m);
          });

          if (hidden.length > 0) {
            const moreId = global.RoomViewLayout.makeMoreNodeId(dirNode.id);
            const moreModel = {
              id: moreId,
              label: `+${hidden.length} more items`,
              parentId: dirNode.id,
              children: hidden.map((leaf, hIdx) => {
                const entry = global.RoomViewLayout.getLegendEntry(descriptor, leaf.kind);
                const hm = {
                  id: leaf.id,
                  objectId: leaf.objectId,
                  label: `${entry.symbol} ${leaf.label}`,
                  meta: `${entry.label}${leaf.metric != null ? ` · ${leaf.metric}` : ''}`,
                  parentId: moreId,
                  children: [],
                  nodeType: 'item',
                  setsize: leaves.length,
                  posinset: visible.length + hIdx + 1,
                };
                this.modelById.set(hm.id, hm);
                return hm;
              }),
              nodeType: 'more',
              pedestalId: dirNode.id,
              setsize: visible.length + 1,
              posinset: visible.length + 1,
            };
            model.children.push(moreModel);
            this.modelById.set(moreId, moreModel);
          }
        }
        return model;
      };

      const rootModel = buildModel(layout.treeRoot, null);

      const renderNode = (model, container, setsize, posinset) => {
        const hasKids = model.lazy || model.children.length > 0;
        const isExpanded = !model.lazy && this.expanded.has(model.id);

        const itemWrap = document.createElement('div');
        itemWrap.setAttribute('role', 'treeitem');
        itemWrap.setAttribute('data-node-id', model.id);
        itemWrap.setAttribute('aria-selected', model.id === this.selectedId ? 'true' : 'false');
        itemWrap.setAttribute('aria-setsize', String(model.setsize || setsize));
        itemWrap.setAttribute('aria-posinset', String(model.posinset || posinset));
        itemWrap.tabIndex = model.id === this.focusedId ? 0 : -1;
        if (hasKids) {
          itemWrap.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
        }

        const row = document.createElement('div');
        row.className = 'tree-node';
        if (model.id === this.selectedId) row.setAttribute('aria-selected', 'true');

        const toggle = document.createElement('span');
        toggle.className = 'tree-toggle';
        toggle.setAttribute('aria-hidden', 'true');
        toggle.textContent = hasKids ? (isExpanded ? '▾' : '▸') : '·';
        row.appendChild(toggle);

        const text = document.createElement('span');
        text.textContent = model.label;
        row.appendChild(text);

        if (model.meta) {
          const meta = document.createElement('span');
          meta.className = 'muted-text';
          meta.style.marginLeft = 'auto';
          meta.textContent = model.meta;
          row.appendChild(meta);
        }

        row.addEventListener('click', (ev) => {
          ev.stopPropagation();
          if (hasKids && ev.target === toggle) {
            this.toggleExpand(model.id);
            return;
          }
          if (model.nodeType === 'more' && this.callbacks.onExpandMore) {
            this.expanded.add(model.id);
            this.callbacks.onExpandMore(model.pedestalId);
            return;
          }
          this.selectAndFocus(model.id, { emit: true });
        });

        itemWrap.appendChild(row);
        this.nodeElements.set(model.id, itemWrap);
        this.flatOrder.push(model.id);

        if (hasKids && isExpanded) {
          const groupEl = document.createElement('div');
          groupEl.className = 'tree-children';
          groupEl.setAttribute('role', 'group');
          model.children.forEach((child, idx) => {
            renderNode(child, groupEl, model.children.length, idx + 1);
          });
          itemWrap.appendChild(groupEl);
        }
        container.appendChild(itemWrap);
      };

      renderNode(rootModel, this.rootEl, 1, 1);
      if (!this.nodeElements.has(this.focusedId)) {
        let fallback = this.modelById.get(this.focusedId);
        while (fallback && !this.nodeElements.has(fallback.id)) fallback = this.modelById.get(fallback.parentId);
        this.focusedId = fallback?.id || 'root';
        this.nodeElements.get(this.focusedId).tabIndex = 0;
      }
      if (restoreFocus) this.nodeElements.get(this.focusedId)?.focus({ preventScroll: true });
    }

    toggleExpand(nodeId) {
      if (this.modelById.get(nodeId)?.lazy) {
        this.callbacks.onSelectNode?.(nodeId);
        return;
      }
      if (this.callbacks.onToggle && this.modelById.get(nodeId)?.nodeType !== 'more') {
        this.callbacks.onToggle(nodeId, !this.expanded.has(nodeId));
        return;
      }
      if (this.expanded.has(nodeId)) this.expanded.delete(nodeId);
      else this.expanded.add(nodeId);
      this.build(this.descriptor, this.state, this.layout);
      const el = this.nodeElements.get(nodeId);
      if (el) el.focus();
    }

    syncFromWorld(nodeId, announcement) {
      if (!nodeId || !this.modelById.has(nodeId)) return;
      let cur = this.modelById.get(nodeId);
      while (cur && cur.parentId) {
        this.expanded.add(cur.parentId);
        cur = this.modelById.get(cur.parentId);
      }
      this.selectedId = nodeId;
      this.focusedId = nodeId;
      if (this.descriptor && this.state && this.layout) {
        this.build(this.descriptor, this.state, this.layout);
      }
      if (announcement) this.announce(announcement);
    }

    selectAndFocus(nodeId, { emit = true, domFocus = this.rootEl.contains(document.activeElement) } = {}) {
      if (!this.modelById.has(nodeId)) return;
      this.selectedId = nodeId;
      this.focusedId = nodeId;
      for (const [id, el] of this.nodeElements.entries()) {
        el.setAttribute('aria-selected', id === nodeId ? 'true' : 'false');
        el.tabIndex = id === nodeId ? 0 : -1;
        const row = el.firstElementChild;
        if (row) row.setAttribute('aria-selected', id === nodeId ? 'true' : 'false');
      }
      const target = this.nodeElements.get(nodeId);
      if (domFocus && target) target.focus();
      if (emit && this.callbacks.onSelectNode) {
        this.callbacks.onSelectNode(nodeId);
      }
    }

    moveRelative(direction) {
      if (!this.flatOrder.length) return;
      const curIdx = Math.max(0, this.flatOrder.indexOf(this.focusedId));
      const curModel = this.modelById.get(this.focusedId);
      if (direction === 'down') {
        const nextId = this.flatOrder[Math.min(this.flatOrder.length - 1, curIdx + 1)];
        this.selectAndFocus(nextId, { emit: true });
      } else if (direction === 'up') {
        const prevId = this.flatOrder[Math.max(0, curIdx - 1)];
        this.selectAndFocus(prevId, { emit: true });
      } else if (direction === 'home') {
        this.selectAndFocus(this.flatOrder[0], { emit: true });
      } else if (direction === 'end') {
        this.selectAndFocus(this.flatOrder[this.flatOrder.length - 1], { emit: true });
      } else if (direction === 'right' && curModel) {
        if (curModel.lazy) return this.toggleExpand(curModel.id);
        if (curModel.children && curModel.children.length) {
          if (!this.expanded.has(curModel.id)) {
            this.toggleExpand(curModel.id);
            return;
          }
          const firstChild = curModel.children[0];
          if (firstChild) this.selectAndFocus(firstChild.id, { emit: true });
        }
      } else if (direction === 'left' && curModel) {
        if (this.expanded.has(curModel.id) && curModel.children.length) {
          this.toggleExpand(curModel.id);
        } else if (curModel.parentId) {
          this.selectAndFocus(curModel.parentId, { emit: true });
        }
      }
    }

    _bindKeyboard() {
      this.rootEl.addEventListener('keydown', (ev) => {
        const handled = new Set(['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'Home', 'End', 'Enter', ' ']);
        if (!handled.has(ev.key)) return;
        ev.preventDefault();
        // Prevent bubbling to global window keyboard handler so each key press executes once!
        ev.stopPropagation();
        if (ev.key === 'ArrowDown') this.moveRelative('down');
        else if (ev.key === 'ArrowUp') this.moveRelative('up');
        else if (ev.key === 'ArrowRight') this.moveRelative('right');
        else if (ev.key === 'ArrowLeft') this.moveRelative('left');
        else if (ev.key === 'Home') this.moveRelative('home');
        else if (ev.key === 'End') this.moveRelative('end');
        else if (ev.key === 'Enter' || ev.key === ' ') {
          const cur = this.modelById.get(this.focusedId);
          if (cur && cur.nodeType === 'more' && this.callbacks.onExpandMore) {
            this.callbacks.onExpandMore(cur.pedestalId);
          } else if (this.callbacks.onSelectNode) {
            this.callbacks.onSelectNode(this.focusedId);
          }
        }
      });
    }
  }

  global.RoomViewAriaTree = { AccessibleTreeTwin };
})(window);
