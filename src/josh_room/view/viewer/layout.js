// Unified arbitrary-depth FSN tree layout, collision-free identifiers, custom legend
// resolution, 5-quantile height binning, and SpaceTree aggregation.
(function (global) {
  'use strict';

  const BLOCK_WIDTH = 0.78;
  const BLOCK_GAP = 0.39; // exact 0.5x block width (FSN proportion)
  const BLOCK_PITCH = BLOCK_WIDTH + BLOCK_GAP;
  const PEDESTAL_PAD = 0.76;
  const QUANTILE_HEIGHTS = [0.62, 1.25, 2.05, 3.05, 4.35];
  const INDIVIDUAL_MAX = 12;
  const AGGREGATE_THRESHOLD = 16;
  const AGGREGATE_TOP_K = 11;
  const SUBTREE_COLLAPSE_THRESHOLD = 50;

  // Collision-free node ID builders (SAFE_LABEL_RE forbids '/' and '%')
  function encodePart(v) {
    return encodeURIComponent(String(v));
  }
  function makeGroupNodeId(groupId) {
    return `group/${encodePart(groupId)}`;
  }
  function makeStageNodeId(groupId, stageId) {
    return `stage/${encodePart(groupId)}/${encodePart(stageId)}`;
  }
  function makeSubtreeSummaryId(parentDirId, childDirId) {
    return `summary/${encodePart(parentDirId)}/${encodePart(childDirId)}`;
  }
  function makeMoreNodeId(pedestalId) {
    return `more/${encodePart(pedestalId)}`;
  }
  function makeItemNodeId(itemId) {
    return `item/${encodePart(itemId)}`;
  }
  function makeCustomNodeId(nodeId) {
    return `node/${encodePart(nodeId)}`;
  }

  function isWorkspace(descriptor) {
    return descriptor?.tool === 'josh-room' && descriptor.group_label === 'Directory';
  }

  function isFailureKind(kind) {
    return kind === 'fail' || kind === 'error' || kind === 'critical';
  }

  function shapeToSymbol(shape) {
    if (shape === 'notch') return '×';
    if (shape === 'hollow') return '○';
    return '●';
  }

  function getLegendEntries(descriptor) {
    const raw = (descriptor && descriptor.legend) || {};
    const entries = [];
    for (const [kind, def] of Object.entries(raw)) {
      const shape = (def && def.shape) || 'solid';
      const label = (def && def.label) || kind;
      const color = (def && def.color) || '#8A93A6';
      const isFailure = isFailureKind(kind);
      entries.push({
        kind,
        label,
        color,
        shape,
        symbol: shapeToSymbol(shape),
        isFailure,
      });
    }
    if (!entries.length) {
      entries.push(
        { kind: 'pass', label: 'Pass', color: '#3FB68B', shape: 'solid', symbol: '●', isFailure: false },
        { kind: 'fail', label: 'Fail', color: '#F0673A', shape: 'notch', symbol: '×', isFailure: true },
        { kind: 'skip', label: 'Skip', color: '#8A93A6', shape: 'hollow', symbol: '○', isFailure: false },
      );
    }
    return entries;
  }

  function getLegendEntry(descriptor, kind) {
    const raw = (descriptor && descriptor.legend) || {};
    const def = raw[kind];
    if (def) {
      const shape = def.shape || 'solid';
      return {
        kind,
        label: def.label || kind,
        color: def.color || '#8A93A6',
        shape,
        symbol: shapeToSymbol(shape),
        isFailure: isFailureKind(kind),
      };
    }
    // Never silently rewrite an unrecognized kind to PASS
    const safeKind = kind || 'unknown';
    const isFailure = isFailureKind(safeKind);
    return {
      kind: safeKind,
      label: safeKind,
      color: isFailure ? '#F0673A' : '#8A93A6',
      shape: isFailure ? 'notch' : 'solid',
      symbol: isFailure ? '×' : '●',
      isFailure,
    };
  }

  function formatOutcomeBadge(descriptor, kind) {
    const entry = getLegendEntry(descriptor, kind);
    return `${entry.symbol} ${entry.label}`;
  }

  function outcomeCounts(items, descriptor) {
    const byKind = {};
    let pass = 0;
    let fail = 0;
    let skip = 0;
    let other = 0;
    for (const item of items || []) {
      const kind = item.kind || 'unknown';
      byKind[kind] = (byKind[kind] || 0) + 1;
      const entry = getLegendEntry(descriptor, kind);
      if (entry.isFailure) fail += 1;
      else if (kind === 'skip') skip += 1;
      else if (kind === 'pass') pass += 1;
      else other += 1;
    }
    return { pass, fail, skip, other, byKind, total: (items || []).length };
  }

  function formatCountsSummary(counts, descriptor) {
    if (!counts) return '0 items';
    const entries = getLegendEntries(descriptor);
    const parts = [];
    for (const entry of entries) {
      const n = (counts.byKind && counts.byKind[entry.kind]) || 0;
      if (n > 0) parts.push(`${n} ${entry.label.toLowerCase()}`);
    }
    if (!parts.length) return `${counts.total} items`;
    return `${counts.total} total · ${parts.slice(0, 4).join(' · ')}`;
  }

  function computeQuantileBreaks(items) {
    const vals = (items || [])
      .map((item) => item.metric)
      .filter((v) => typeof v === 'number' && Number.isFinite(v))
      .sort((a, b) => a - b);
    if (!vals.length) return [0, 0, 0, 0];
    const at = (p) => vals[Math.min(vals.length - 1, Math.floor(p * (vals.length - 1)))];
    return [at(0.2), at(0.4), at(0.6), at(0.8)];
  }

  function metricToQuantileBin(metric, breaks) {
    if (typeof metric !== 'number' || !Number.isFinite(metric)) return 1;
    let bin = 0;
    for (let i = 0; i < breaks.length; i += 1) {
      if (metric > breaks[i]) bin = i + 1;
    }
    return Math.min(4, bin);
  }

  function sortItemsByInterest(items, descriptor) {
    const filesystem = isWorkspace(descriptor);
    return (items || []).slice().sort((a, b) => {
      if (filesystem) return a.label.localeCompare(b.label) || a.id.localeCompare(b.id);
      const priority = (item) => isFailureKind(item.kind) ? 2 : Number(item.kind !== 'skip');
      return priority(b) - priority(a) || (b.metric || 0) - (a.metric || 0) || a.id.localeCompare(b.id);
    });
  }

  function pedestalDimensionsForCount(count, { minWidth = 3.2, minDepth = 2.6 } = {}) {
    if (count <= 0) {
      return { cols: 1, rows: 1, width: minWidth, depth: minDepth, height: 0.42 };
    }
    const cols = Math.min(4, Math.ceil(Math.sqrt(count * 1.25)));
    const rows = Math.ceil(count / cols);
    const width = Math.max(minWidth, cols * BLOCK_PITCH - BLOCK_GAP + PEDESTAL_PAD * 2);
    const depth = Math.max(minDepth, rows * BLOCK_PITCH - BLOCK_GAP + PEDESTAL_PAD * 2);
    const height = Math.min(0.84, 0.36 + Math.log10(count + 1) * 0.24);
    return { cols, rows, width, depth, height };
  }

  /**
   * Normalize either arbitrary-depth `state.nodes` OR legacy `groups -> stages -> items`
   * into a single unified hierarchical tree model.
   */
  function buildUnifiedTree(descriptor, state) {
    const hasCustomNodes = Array.isArray(state.nodes) && state.nodes.length > 0;
    const allTreeNodesById = new Map();
    const allLeavesByObjectId = new Map();
    const allLeaves = [];
    const incomingLinksByObjectId = new Map();

    const recordIncomingLinks = (sourceObjId, sourcePath, sourceLabel, links) => {
      for (const lk of links || []) {
        if (!incomingLinksByObjectId.has(lk.target_id)) {
          incomingLinksByObjectId.set(lk.target_id, []);
        }
        incomingLinksByObjectId.get(lk.target_id).push({
          relation: lk.relation,
          source_id: sourceObjId,
          source_path: sourcePath,
          source_label: sourceLabel,
          label: lk.label || '',
        });
      }
    };

    if (hasCustomNodes) {
      const rawNodes = state.nodes.slice().sort((a, b) => ((a.order ?? 0) - (b.order ?? 0)) || a.id.localeCompare(b.id));
      const rawById = new Map(rawNodes.map((n) => [n.id, n]));
      const rootRaw = rawNodes.find((n) => n.parent_id == null) || rawNodes[0];

      const childrenByParent = new Map();
      for (const n of rawNodes) {
        if (n.parent_id != null) {
          if (!childrenByParent.has(n.parent_id)) childrenByParent.set(n.parent_id, []);
          childrenByParent.get(n.parent_id).push(n);
        }
      }

      const buildRec = (raw, parentTreeId, parentPath, depth) => {
        const isDir = raw.node_kind === 'directory';
        const isRoot = raw.parent_id == null;
        const treeId = isRoot ? 'root' : makeCustomNodeId(raw.id);
        const seg = String(raw.label || '').replace(/^\/+|\/+$/g, '');
        const path = isRoot ? '/' : (parentPath === '/' ? `/${seg}` : `${parentPath}/${seg}`);
        const displayLabel = isRoot ? '/' : (depth === 1 && isDir ? `/${seg}` : raw.label);

        if (isDir) {
          const dirNode = {
            id: treeId,
            objectId: raw.id,
            parentId: parentTreeId,
            nodeType: isRoot ? 'root' : 'directory',
            nodeKind: 'directory',
            label: displayLabel,
            segmentLabel: raw.label,
            path,
            depth,
            summary: raw.summary || null,
            updated_at: raw.updated_at,
            fields: raw.fields || {},
            previews: raw.previews || [],
            attachments: raw.attachments || [],
            links: raw.links || [],
            dirChildren: [],
            leafChildren: [],
            allDescendantLeaves: [],
            counts: null,
          };
          allTreeNodesById.set(treeId, dirNode);
          recordIncomingLinks(raw.id, path, raw.label, dirNode.links);
          const kids = childrenByParent.get(raw.id) || [];
          for (const kid of kids) {
            const built = buildRec(kid, treeId, path, depth + 1);
            if (built.nodeKind === 'directory') {
              dirNode.dirChildren.push(built);
              dirNode.allDescendantLeaves.push(...built.allDescendantLeaves);
            } else {
              dirNode.leafChildren.push(built);
              dirNode.allDescendantLeaves.push(built);
            }
          }
          dirNode.counts = outcomeCounts(dirNode.allDescendantLeaves, descriptor);
          return dirNode;
        }

        const leafNode = {
          id: treeId,
          objectId: raw.id,
          parentId: parentTreeId,
          nodeType: 'item',
          nodeKind: raw.node_kind || 'file',
          label: raw.label,
          path,
          parentPath,
          depth,
          kind: raw.kind || 'unknown',
          metric: raw.metric != null ? raw.metric : null,
          updated_at: raw.updated_at,
          summary: raw.summary || null,
          fields: raw.fields || {},
          attachments: raw.attachments || [],
          previews: raw.previews || [],
          links: raw.links || [],
          group: null,
          stage: null,
        };
        allTreeNodesById.set(treeId, leafNode);
        allLeavesByObjectId.set(raw.id, leafNode);
        allLeaves.push(leafNode);
        recordIncomingLinks(raw.id, path, raw.label, leafNode.links);
        return leafNode;
      };

      const treeRoot = buildRec(rootRaw, null, '/', 0);
      return {
        isUnixTree: true,
        treeRoot,
        allTreeNodesById,
        allLeavesByObjectId,
        allLeaves,
        incomingLinksByObjectId,
        rawById,
      };
    }

    // Legacy 4-level provider mapping: root -> group -> stage -> item (100% collision-free nested Maps!)
    const stages = (descriptor.stages || []).slice().sort((a, b) => a.order - b.order);
    const groups = (state.groups || []).slice().sort((a, b) => (a.order || 0) - (b.order || 0));
    const itemsByGroup = new Map();
    const itemsByGroupStage = new Map();
    for (const g of groups) {
      itemsByGroup.set(g.id, []);
      itemsByGroupStage.set(g.id, new Map());
    }
    for (const item of state.items || []) {
      if (!itemsByGroup.has(item.group)) itemsByGroup.set(item.group, []);
      itemsByGroup.get(item.group).push(item);
      if (!itemsByGroupStage.has(item.group)) itemsByGroupStage.set(item.group, new Map());
      const stageMap = itemsByGroupStage.get(item.group);
      if (!stageMap.has(item.stage)) stageMap.set(item.stage, []);
      stageMap.get(item.stage).push(item);
    }

    const treeRoot = {
      id: 'root',
      objectId: 'root',
      parentId: null,
      nodeType: 'root',
      nodeKind: 'directory',
      label: '/',
      segmentLabel: '/',
      path: '/',
      depth: 0,
      summary: descriptor.title || 'Room View',
      dirChildren: [],
      leafChildren: [],
      allDescendantLeaves: [],
      counts: null,
    };
    allTreeNodesById.set('root', treeRoot);

    for (const group of groups) {
      const gId = makeGroupNodeId(group.id);
      const gPath = `/${group.id}`;
      const gNode = {
        id: gId,
        objectId: group.id,
        groupId: group.id,
        parentId: 'root',
        nodeType: 'group',
        nodeKind: 'directory',
        label: group.name,
        segmentLabel: group.name,
        path: gPath,
        depth: 1,
        dirChildren: [],
        leafChildren: [],
        allDescendantLeaves: [],
        counts: null,
      };
      allTreeNodesById.set(gId, gNode);
      treeRoot.dirChildren.push(gNode);

      const stageMap = itemsByGroupStage.get(group.id) || new Map();
      for (const stage of stages) {
        const sItems = stageMap.get(stage.id) || [];
        if (!sItems.length) continue;
        const sId = makeStageNodeId(group.id, stage.id);
        const sPath = `${gPath}/${stage.id}`;
        const sNode = {
          id: sId,
          objectId: sId,
          groupId: group.id,
          stageId: stage.id,
          parentId: gId,
          nodeType: 'stage',
          nodeKind: 'directory',
          label: stage.name,
          segmentLabel: stage.name,
          fullLabel: `${group.name} / ${stage.name}`,
          path: sPath,
          depth: 2,
          dirChildren: [],
          leafChildren: [],
          allDescendantLeaves: [],
          counts: null,
        };
        allTreeNodesById.set(sId, sNode);
        gNode.dirChildren.push(sNode);

        for (const item of sItems) {
          const itemNodeId = makeItemNodeId(item.id);
          const itemPath = `${sPath}/${item.label}`;
          const leaf = {
            id: itemNodeId,
            objectId: item.id,
            parentId: sId,
            nodeType: 'item',
            nodeKind: 'file',
            label: item.label,
            path: itemPath,
            parentPath: sPath,
            depth: 3,
            group: item.group,
            stage: item.stage,
            kind: item.kind,
            metric: item.metric != null ? item.metric : null,
            updated_at: item.updated_at,
            summary: null,
            fields: item.fields || {},
            attachments: item.attachments || [],
            previews: item.previews || [],
            links: item.links || [],
          };
          allTreeNodesById.set(itemNodeId, leaf);
          allLeavesByObjectId.set(item.id, leaf);
          allLeaves.push(leaf);
          sNode.leafChildren.push(leaf);
          sNode.allDescendantLeaves.push(leaf);
          gNode.allDescendantLeaves.push(leaf);
          treeRoot.allDescendantLeaves.push(leaf);
          recordIncomingLinks(item.id, itemPath, item.label, leaf.links);
        }
        sNode.counts = outcomeCounts(sNode.allDescendantLeaves, descriptor);
      }
      gNode.counts = outcomeCounts(gNode.allDescendantLeaves, descriptor);
    }
    treeRoot.counts = outcomeCounts(treeRoot.allDescendantLeaves, descriptor);

    return {
      isUnixTree: false,
      treeRoot,
      allTreeNodesById,
      allLeavesByObjectId,
      allLeaves,
      incomingLinksByObjectId,
    };
  }

  /**
   * Build the complete deterministic 3D FSN directory tree layout in constant world units.
   * Supports arbitrary directory depth (/, /src/core/codec/vectors, etc.) AND legacy
   * group/stage hierarchies with SpaceTree aggregation and collision-free IDs.
   */
  function buildFsnLayout(descriptor, state, options) {
    const opts = options || {};
    const expandedDirs = opts.expandedDirs || opts.expandedGroups || new Set();
    const expandedPedestals = opts.expandedPedestals || new Set();

    const unified = buildUnifiedTree(descriptor, state);
    const selected = unified.allTreeNodesById.get(opts.selectedNodeId);
    const selectedAncestors = new Set();
    for (let node = selected; node; node = unified.allTreeNodesById.get(node.parentId)) {
      selectedAncestors.add(node.id);
    }
    const breaks = computeQuantileBreaks(unified.allLeaves);

    const pedestals = [];
    const blocks = [];
    const summaryBlocks = [];
    const wires = [];
    const nodeById = new Map();
    const itemNodeById = new Map(); // objectId -> 3D block node (when currently visible)

    const SIBLING_GAP_X = unified.isUnixTree ? 1.85 : 2.8;
    const ROOT_CHILD_GAP_X = unified.isUnixTree ? 2.15 : 4.2;
    const DEPTH_STEP_Z_FIRST = unified.isUnixTree ? 9.4 : 11.8;
    const DEPTH_STEP_Z = unified.isUnixTree ? 10.6 : 13.0;

    // Pass 1: Measure every directory's pedestal dimensions & recursive subtree width
    const measureDir = (dir) => {
      const isRoot = dir.depth === 0;
      const autoCollapsed = (
        !isRoot &&
        dir.dirChildren.length > 0 &&
        dir.allDescendantLeaves.length > SUBTREE_COLLAPSE_THRESHOLD &&
        !selectedAncestors.has(dir.id) &&
        !expandedDirs.has(dir.id) &&
        !(dir.groupId && expandedDirs.has(dir.groupId))
      );

      const forceExpandedLeaves = expandedPedestals.has(dir.id);
      const aggregatedLeaves = !forceExpandedLeaves && dir.leafChildren.length > AGGREGATE_THRESHOLD;
      const visibleLeafCount = aggregatedLeaves ? AGGREGATE_TOP_K + 1 : dir.leafChildren.length;

      let dims;
      if (autoCollapsed) {
        const w = Math.max(5.8, dir.dirChildren.length * 1.18 + 1.6);
        dims = { cols: dir.dirChildren.length, rows: 1, width: w, depth: 3.3, height: 0.54 };
      } else if (isRoot) {
        const minW = Math.max(6.4, visibleLeafCount > 0 ? 6.6 : 6.0);
        dims = pedestalDimensionsForCount(visibleLeafCount, { minWidth: minW, minDepth: 3.6 });
        dims.height = Math.max(0.52, dims.height);
      } else {
        const minW = dir.depth === 1 ? 4.4 : 3.2;
        const minD = dir.depth === 1 ? 2.9 : 2.5;
        dims = pedestalDimensionsForCount(visibleLeafCount, { minWidth: minW, minDepth: minD });
      }

      const childPlans = autoCollapsed ? [] : dir.dirChildren.map((child) => measureDir(child));

      // Split into 2 staggered tiers ONLY for wide legacy groups (>=5 direct subdirs & no grandchildren)
      const useTwoTierRows = (
        !autoCollapsed &&
        !isRoot &&
        childPlans.length >= 5 &&
        childPlans.every((cp) => cp.childPlans.length === 0)
      );
      const gap = isRoot ? ROOT_CHILD_GAP_X : SIBLING_GAP_X;

      let subtreeWidth = dims.width;
      let frontRow = childPlans;
      let backRow = [];
      if (useTwoTierRows) {
        const split = Math.ceil(childPlans.length / 2);
        frontRow = childPlans.slice(0, split);
        backRow = childPlans.slice(split);
        const rowW = (row) => (
          row.reduce((acc, cp) => acc + cp.subtreeWidth, 0) + Math.max(0, row.length - 1) * gap
        );
        subtreeWidth = Math.max(dims.width, rowW(frontRow), rowW(backRow));
      } else if (childPlans.length > 0) {
        const childrenSpan = (
          childPlans.reduce((acc, cp) => acc + cp.subtreeWidth, 0) +
          Math.max(0, childPlans.length - 1) * gap
        );
        subtreeWidth = Math.max(dims.width, childrenSpan);
      }

      return {
        dir,
        autoCollapsed,
        aggregatedLeaves,
        visibleLeafCount,
        dims,
        childPlans,
        useTwoTierRows,
        frontRow,
        backRow,
        gap,
        subtreeWidth,
      };
    };

    const rootPlan = measureDir(unified.treeRoot);

    // Pass 2: Place pedestals, upright file monoliths, summary towers, and orthogonal conduits
    const placeLeafBlocks = (pedNode, plan) => {
      const leaves = plan.dir.leafChildren;
      if (!leaves.length) return;
      const ranked = sortItemsByInterest(leaves, descriptor);
      const visible = plan.aggregatedLeaves ? ranked.slice(0, AGGREGATE_TOP_K) : ranked;
      const hidden = plan.aggregatedLeaves ? ranked.slice(AGGREGATE_TOP_K) : [];
      if (hidden.length && selected?.parentId === plan.dir.id && selected.nodeType === 'item') {
        const index = hidden.indexOf(selected);
        if (index >= 0) {
          hidden[index] = visible[visible.length - 1];
          visible[visible.length - 1] = selected;
        }
      }
      const totalSlots = visible.length + (hidden.length > 0 ? 1 : 0);
      const cols = Math.min(plan.dims.cols, Math.max(1, totalSlots));
      const rows = Math.ceil(totalSlots / cols);
      const gridW = cols * BLOCK_PITCH - BLOCK_GAP;
      const gridD = rows * BLOCK_PITCH - BLOCK_GAP;

      visible.forEach((leaf, idx) => {
        const col = idx % cols;
        const row = Math.floor(idx / cols);
        const bx = pedNode.x - gridW / 2 + BLOCK_WIDTH / 2 + col * BLOCK_PITCH;
        const bz = pedNode.z + gridD / 2 - BLOCK_WIDTH / 2 - row * BLOCK_PITCH;
        const qBin = metricToQuantileBin(leaf.metric, breaks);
        const bh = QUANTILE_HEIGHTS[qBin];
        const legendEntry = getLegendEntry(descriptor, leaf.kind);
        const blockNode = {
          id: leaf.id,
          objectId: leaf.objectId,
          nodeType: 'item',
          nodeKind: leaf.nodeKind,
          itemId: leaf.objectId,
          item: leaf,
          groupId: leaf.group,
          stageId: leaf.stage,
          parentId: pedNode.id,
          label: leaf.label,
          path: leaf.path,
          kind: leaf.kind,
          legendEntry,
          quantileBin: qBin + 1,
          x: bx,
          y: pedNode.height,
          z: bz,
          width: BLOCK_WIDTH,
          depth: BLOCK_WIDTH,
          height: bh,
        };
        blocks.push(blockNode);
        nodeById.set(blockNode.id, blockNode);
        itemNodeById.set(leaf.objectId, blockNode);
        pedNode.childrenIds.push(blockNode.id);
      });

      if (hidden.length > 0) {
        const idx = visible.length;
        const col = idx % cols;
        const row = Math.floor(idx / cols);
        const bx = pedNode.x - gridW / 2 + BLOCK_WIDTH / 2 + col * BLOCK_PITCH;
        const bz = pedNode.z + gridD / 2 - BLOCK_WIDTH / 2 - row * BLOCK_PITCH;
        const hCounts = outcomeCounts(hidden, descriptor);
        const moreNode = {
          id: makeMoreNodeId(pedNode.id),
          nodeType: 'more',
          pedestalId: pedNode.id,
          parentId: pedNode.id,
          label: `+${hidden.length} more`,
          path: `${pedNode.path} (+${hidden.length} more)`,
          hiddenCount: hidden.length,
          hiddenItemIds: hidden.map((it) => it.objectId),
          x: bx,
          y: pedNode.height,
          z: bz,
          width: 0.84,
          depth: 0.84,
          height: Math.min(3.1, 1.05 + Math.log2(hidden.length + 1) * 0.36),
          counts: hCounts,
        };
        summaryBlocks.push(moreNode);
        nodeById.set(moreNode.id, moreNode);
        pedNode.childrenIds.push(moreNode.id);
      }
    };

    const placeDir = (plan, x, z, parentPed) => {
      const d = plan.dir;
      const pedNode = {
        id: d.id,
        objectId: d.objectId,
        nodeType: d.nodeType,
        nodeKind: 'directory',
        groupId: d.groupId || null,
        stageId: d.stageId || null,
        label: d.label,
        segmentLabel: d.segmentLabel,
        path: d.path,
        depth: d.depth,
        summary: d.summary || null,
        parentId: parentPed ? parentPed.id : null,
        x,
        y: 0,
        z,
        width: plan.dims.width,
        depthZ: plan.dims.depth,
        depthDim: plan.dims.depth,
        // Keep `.depth` numeric pedestal Z dimension for scene3d compatibility + `.treeDepth` for level
        depth: plan.dims.depth,
        treeDepth: d.depth,
        coverage: d.fields?.coverage,
        height: plan.dims.height,
        counts: d.counts,
        collapsed: plan.autoCollapsed,
        aggregated: plan.aggregatedLeaves,
        hiddenCount: plan.aggregatedLeaves ? d.leafChildren.length - AGGREGATE_TOP_K : 0,
        childDirCount: d.dirChildren.length,
        directLeafCount: d.leafChildren.length,
        childrenIds: [],
      };
      pedestals.push(pedNode);
      nodeById.set(pedNode.id, pedNode);
      if (parentPed) {
        parentPed.childrenIds.push(pedNode.id);
      }

      if (plan.autoCollapsed) {
        // SpaceTree collapsed child-directory summary towers directly on this pedestal
        const kids = d.dirChildren;
        const pitch = 1.12;
        const startX = pedNode.x - ((kids.length - 1) * pitch) / 2;
        kids.forEach((childDir, idx) => {
          const bx = startX + idx * pitch;
          const bz = pedNode.z;
          const counts = childDir.counts;
          const h = Math.min(3.8, 0.9 + Math.log2(counts.total + 1) * 0.48);
          const sumNode = {
            id: makeSubtreeSummaryId(pedNode.id, childDir.id),
            nodeType: 'stage-summary',
            targetDirId: childDir.id,
            expandDirId: pedNode.id,
            groupId: pedNode.groupId || null,
            stageId: childDir.stageId || null,
            parentId: pedNode.id,
            label: `${childDir.label} (${counts.total})`,
            path: childDir.path,
            x: bx,
            y: pedNode.height,
            z: bz,
            width: 0.86,
            depth: 0.86,
            height: h,
            counts,
          };
          summaryBlocks.push(sumNode);
          nodeById.set(sumNode.id, sumNode);
        });
        return pedNode;
      }

      // Place direct files/runs/artifacts on top of this directory pedestal
      placeLeafBlocks(pedNode, plan);

      if (!plan.childPlans.length) return pedNode;

      const stepZ = d.depth === 0 ? DEPTH_STEP_Z_FIRST : DEPTH_STEP_Z;
      const deepestDeck = (row) => row.reduce((depth, child) => Math.max(depth, child.dims.depth), 0);
      const rowSpacing = (nearDepth, farDepth) => Math.max(stepZ, (nearDepth + farDepth) / 2 + 4.8);
      const frontDepth = deepestDeck(plan.frontRow);
      const frontZ = z - rowSpacing(pedNode.depth, frontDepth);
      const placeChildRow = (rowPlans, rowZ, isBackRow, aisleX) => {
        if (!rowPlans.length) return;
        const rowSpan = (
          rowPlans.reduce((acc, cp) => acc + cp.subtreeWidth, 0) +
          Math.max(0, rowPlans.length - 1) * plan.gap
        );
        let curX = x - rowSpan / 2;
        const busZ = (z - pedNode.depth / 2 + rowZ + deepestDeck(rowPlans) / 2) / 2;

        for (const cp of rowPlans) {
          const cx = curX + cp.subtreeWidth / 2;
          curX += cp.subtreeWidth + plan.gap;
          const childPed = placeDir(cp, cx, rowZ, pedNode);

          if (!isBackRow) {
            wires.push({
              id: `wire/${encodePart(pedNode.id)}/${encodePart(childPed.id)}`,
              parentId: pedNode.id,
              childId: childPed.id,
              points: [
                [pedNode.x, 0.04, pedNode.z - pedNode.depth / 2],
                [pedNode.x, 0.04, busZ],
                [childPed.x, 0.04, busZ],
                [childPed.x, 0.04, childPed.z + childPed.depth / 2],
              ],
            });
          } else {
            const firstTurnZ = z - pedNode.depth / 2 - 3.6;
            const backBusZ = rowZ + childPed.depth / 2 + 3.4;
            wires.push({
              id: `wire/${encodePart(pedNode.id)}/${encodePart(childPed.id)}`,
              parentId: pedNode.id,
              childId: childPed.id,
              points: [
                [pedNode.x, 0.04, pedNode.z - pedNode.depth / 2],
                [pedNode.x, 0.04, firstTurnZ],
                [aisleX, 0.04, firstTurnZ],
                [aisleX, 0.04, backBusZ],
                [childPed.x, 0.04, backBusZ],
                [childPed.x, 0.04, childPed.z + childPed.depth / 2],
              ],
            });
          }
        }
      };

      if (plan.useTwoTierRows) {
        const backZ = frontZ - rowSpacing(frontDepth, deepestDeck(plan.backRow));
        const frontSpan = (
          plan.frontRow.reduce((acc, cp) => acc + cp.subtreeWidth, 0) +
          Math.max(0, plan.frontRow.length - 1) * plan.gap
        );
        const aisleX = x + frontSpan / 2 + 0.9;
        placeChildRow(plan.frontRow, frontZ, false, aisleX);
        placeChildRow(plan.backRow, backZ, true, aisleX);
      } else {
        placeChildRow(plan.childPlans, frontZ, false, x);
      }

      return pedNode;
    };

    placeDir(rootPlan, 0, 0, null);

    let minX = -6;
    let maxX = 6;
    let minZ = -18;
    let maxZ = 4;
    let maxDepth = 0;
    for (const p of pedestals) {
      minX = Math.min(minX, p.x - p.width / 2);
      maxX = Math.max(maxX, p.x + p.width / 2);
      minZ = Math.min(minZ, p.z - p.depth / 2);
      maxZ = Math.max(maxZ, p.z + p.depth / 2);
      maxDepth = Math.max(maxDepth, p.treeDepth || 0);
    }

    return {
      isUnixTree: unified.isUnixTree,
      treeRoot: unified.treeRoot,
      allTreeNodesById: unified.allTreeNodesById,
      allLeavesByObjectId: unified.allLeavesByObjectId,
      allLeaves: unified.allLeaves,
      incomingLinksByObjectId: unified.incomingLinksByObjectId,
      pedestals,
      blocks,
      summaryBlocks,
      wires,
      nodeById,
      itemNodeById,
      quantileBreaks: breaks,
      maxDepth,
      bounds: {
        minX,
        maxX,
        minZ,
        maxZ,
        centerX: (minX + maxX) / 2,
        centerZ: (minZ + maxZ) / 2,
        spanX: maxX - minX,
        spanZ: maxZ - minZ,
      },
      thresholds: {
        individualMax: INDIVIDUAL_MAX,
        aggregateThreshold: AGGREGATE_THRESHOLD,
        aggregateTopK: AGGREGATE_TOP_K,
        groupCollapseThreshold: SUBTREE_COLLAPSE_THRESHOLD,
      },
    };
  }

  global.RoomViewLayout = {
    BLOCK_WIDTH,
    BLOCK_GAP,
    isWorkspace,
    QUANTILE_HEIGHTS,
    makeGroupNodeId,
    makeStageNodeId,
    makeSubtreeSummaryId,
    makeMoreNodeId,
    makeItemNodeId,
    makeCustomNodeId,
    getLegendEntries,
    getLegendEntry,
    formatOutcomeBadge,
    formatCountsSummary,
    computeQuantileBreaks,
    metricToQuantileBin,
    sortItemsByInterest,
    outcomeCounts,
    buildUnifiedTree,
    buildFsnLayout,
  };
})(window);
