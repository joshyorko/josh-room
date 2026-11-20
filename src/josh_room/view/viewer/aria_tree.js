// Synchronized APG role="tree" accessible twin for the aria-hidden 3D WebGL canvas.
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
      // Force polite live-region re-announcement
      requestAnimationFrame(() => {
        this.liveEl.textContent = message;
      });
    }

    build(descriptor, state, layout) {
      this.descriptor = descriptor;
      this.state = state;
      this.layout = layout;
      this.rootEl.textContent = '';
      this.nodeElements.clear();
      this.modelById.clear();
      this.flatOrder = [];

      const stages = (descriptor.stages || []).slice().sort((a, b) => a.order - b.order);
      const groups = (state.groups || []).slice().sort((a, b) => (a.order || 0) - (b.order || 0));
      const itemsByGS = new Map();
      for (const item of (state.items || [])) {
        const key = `${item.group}::${item.stage}`;
        if (!itemsByGS.has(key)) itemsByGS.set(key, []);
        itemsByGS.get(key).push(item);
      }

      const rootModel = {
        id: 'root',
        label: `/ (${descriptor.title})`,
        parentId: null,
        children: [],
        nodeType: 'root',
      };
      this.modelById.set('root', rootModel);

      for (const group of groups) {
        const gId = `group:${group.id}`;
        const gModel = {
          id: gId,
          label: group.name,
          parentId: 'root',
          children: [],
          nodeType: 'group',
          groupId: group.id,
        };
        rootModel.children.push(gModel);
        this.modelById.set(gId, gModel);

        for (const stage of stages) {
          const sItems = itemsByGS.get(`${group.id}::${stage.id}`) || [];
          if (!sItems.length) continue;
          const sId = `stage:${group.id}:${stage.id}`;
          const ranked = global.RoomViewLayout.sortItemsByInterest(sItems);
          const pedLayout = layout.nodeById.get(sId);
          const aggregated = pedLayout ? Boolean(pedLayout.aggregated) : (ranked.length > 16);
          const visible = aggregated ? ranked.slice(0, 11) : ranked;
          const hidden = aggregated ? ranked.slice(11) : [];

          const sModel = {
            id: sId,
            label: `${stage.name} (${sItems.length})`,
            parentId: gId,
            children: [],
            nodeType: 'stage',
            groupId: group.id,
            stageId: stage.id,
            totalItems: sItems.length,
          };
          gModel.children.push(sModel);
          this.modelById.set(sId, sModel);

          visible.forEach((item, idx) => {
            const cue = item.kind === 'fail' ? '×' : (item.kind === 'skip' ? '○' : '●');
            const m = {
              id: `item:${item.id}`,
              itemId: item.id,
              label: `${cue} ${item.label}`,
              meta: `${item.kind}${item.metric != null ? ` · ${item.metric}` : ''}`,
              parentId: sId,
              children: [],
              nodeType: 'item',
              setsize: sItems.length,
              posinset: idx + 1,
            };
            sModel.children.push(m);
            this.modelById.set(m.id, m);
          });

          if (hidden.length > 0) {
            const moreId = `more:${group.id}:${stage.id}`;
            const moreModel = {
              id: moreId,
              label: `+${hidden.length} more items`,
              parentId: sId,
              children: hidden.map((item, hIdx) => {
                const cue = item.kind === 'fail' ? '×' : (item.kind === 'skip' ? '○' : '●');
                const hm = {
                  id: `item:${item.id}`,
                  itemId: item.id,
                  label: `${cue} ${item.label}`,
                  meta: `${item.kind}${item.metric != null ? ` · ${item.metric}` : ''}`,
                  parentId: moreId,
                  children: [],
                  nodeType: 'item',
                  setsize: sItems.length,
                  posinset: visible.length + hIdx + 1,
                };
                this.modelById.set(hm.id, hm);
                return hm;
              }),
              nodeType: 'more',
              pedestalId: sId,
              setsize: visible.length + 1,
              posinset: visible.length + 1,
            };
            sModel.children.push(moreModel);
            this.modelById.set(moreId, moreModel);
          }
        }
      }

      const renderNode = (model, container, setsize, posinset) => {
        const hasKids = model.children && model.children.length > 0;
        const isExpanded = this.expanded.has(model.id);

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
    }

    toggleExpand(nodeId) {
      if (this.expanded.has(nodeId)) this.expanded.delete(nodeId);
      else this.expanded.add(nodeId);
      this.build(this.descriptor, this.state, this.layout);
      const el = this.nodeElements.get(nodeId);
      if (el) el.focus();
    }

    syncFromWorld(nodeId, announcement) {
      if (!nodeId || !this.modelById.has(nodeId)) return;
      // Ensure ancestors are expanded in ARIA tree so node is reachable
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

    selectAndFocus(nodeId, { emit = true, domFocus = false } = {}) {
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
        if (curModel.children && curModel.children.length) {
          if (!this.expanded.has(curModel.id)) {
            this.expanded.add(curModel.id);
            this.build(this.descriptor, this.state, this.layout);
          }
          const firstChild = curModel.children[0];
          if (firstChild) this.selectAndFocus(firstChild.id, { emit: true });
        }
      } else if (direction === 'left' && curModel) {
        if (this.expanded.has(curModel.id) && curModel.children.length) {
          this.expanded.delete(curModel.id);
          this.build(this.descriptor, this.state, this.layout);
          this.selectAndFocus(curModel.id, { emit: true });
        } else if (curModel.parentId) {
          this.selectAndFocus(curModel.parentId, { emit: true });
        }
      }
    }

    _bindKeyboard() {
      this.rootEl.addEventListener('keydown', (ev) => {
        if (ev.key === 'ArrowDown') { ev.preventDefault(); this.moveRelative('down'); }
        else if (ev.key === 'ArrowUp') { ev.preventDefault(); this.moveRelative('up'); }
        else if (ev.key === 'ArrowRight') { ev.preventDefault(); this.moveRelative('right'); }
        else if (ev.key === 'ArrowLeft') { ev.preventDefault(); this.moveRelative('left'); }
        else if (ev.key === 'Home') { ev.preventDefault(); this.moveRelative('home'); }
        else if (ev.key === 'End') { ev.preventDefault(); this.moveRelative('end'); }
        else if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
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
