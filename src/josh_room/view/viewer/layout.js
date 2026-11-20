// Spatial FSN tree layout, 5-quantile height binning, and SpaceTree aggregation.
(function (global) {
  'use strict';

  const BLOCK_WIDTH = 0.65;
  const BLOCK_GAP = 0.325; // exact 0.5x block width (FSN proportion)
  const BLOCK_PITCH = BLOCK_WIDTH + BLOCK_GAP;
  const PEDESTAL_PAD = 0.62;
  const QUANTILE_HEIGHTS = [0.46, 1.02, 1.72, 2.62, 3.85];
  const INDIVIDUAL_MAX = 12;
  const AGGREGATE_THRESHOLD = 16;
  const AGGREGATE_TOP_K = 11;
  const GROUP_COLLAPSE_THRESHOLD = 50;

  function computeQuantileBreaks(items) {
    const vals = items
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

  function degreeOfInterestScore(item) {
    // Failures first, then highest metric, deterministic tie-break by id
    const kindWeight = item.kind === 'fail' ? 1000000 : (item.kind === 'skip' ? 0 : 10000);
    const metricVal = typeof item.metric === 'number' && Number.isFinite(item.metric) ? item.metric : 0;
    return kindWeight + metricVal;
  }

  function sortItemsByInterest(items) {
    return items.slice().sort((a, b) => {
      const diff = degreeOfInterestScore(b) - degreeOfInterestScore(a);
      if (diff !== 0) return diff;
      return a.id.localeCompare(b.id);
    });
  }

  function outcomeCounts(items) {
    let pass = 0;
    let fail = 0;
    let skip = 0;
    for (const item of items) {
      if (item.kind === 'fail') fail += 1;
      else if (item.kind === 'skip') skip += 1;
      else pass += 1;
    }
    return { pass, fail, skip, total: items.length };
  }

  function pedestalDimensionsForCount(count) {
    const n = Math.max(1, count);
    const cols = Math.min(4, Math.ceil(Math.sqrt(n * 1.15)));
    const rows = Math.ceil(n / cols);
    const width = Math.max(2.6, cols * BLOCK_PITCH - BLOCK_GAP + PEDESTAL_PAD * 2);
    const depth = Math.max(2.2, rows * BLOCK_PITCH - BLOCK_GAP + PEDESTAL_PAD * 2);
    const height = Math.min(0.82, 0.28 + Math.log10(n + 1) * 0.28);
    return { cols, rows, width, depth, height };
  }

  /**
   * Build the complete deterministic FSN directory tree layout in constant world units.
   * Geometry size NEVER shrinks as N rises; instead the tree spans further and the
   * overview camera frames out to keep all branches legible.
   */
  function buildFsnLayout(descriptor, state, options) {
    const opts = options || {};
    const expandedGroups = opts.expandedGroups || new Set();
    const expandedPedestals = opts.expandedPedestals || new Set();

    const stages = (descriptor.stages || []).slice().sort((a, b) => a.order - b.order);
    const groups = (state.groups || []).slice().sort((a, b) => (a.order || 0) - (b.order || 0));
    const items = state.items || [];
    const breaks = computeQuantileBreaks(items);

    const itemsByGroup = new Map();
    const itemsByGroupStage = new Map();
    for (const group of groups) itemsByGroup.set(group.id, []);
    for (const item of items) {
      if (!itemsByGroup.has(item.group)) itemsByGroup.set(item.group, []);
      itemsByGroup.get(item.group).push(item);
      const gsKey = `${item.group}::${item.stage}`;
      if (!itemsByGroupStage.has(gsKey)) itemsByGroupStage.set(gsKey, []);
      itemsByGroupStage.get(gsKey).push(item);
    }

    const pedestals = [];
    const blocks = [];
    const summaryBlocks = [];
    const wires = [];
    const nodeById = new Map();
    const itemNodeById = new Map();

    // Level 0: Root Pedestal in near foreground
    const rootNode = {
      id: 'root',
      nodeType: 'root',
      label: '/',
      title: descriptor.title || 'Room View',
      parentId: null,
      x: 0,
      y: 0,
      z: 0,
      width: 6.4,
      depth: 3.4,
      height: 0.48,
      counts: outcomeCounts(items),
      childrenIds: [],
    };
    pedestals.push(rootNode);
    nodeById.set(rootNode.id, rootNode);

    // Pre-compute each group's subtree width so sibling groups never overlap
    const groupPlans = groups.map((group) => {
      const gItems = itemsByGroup.get(group.id) || [];
      const autoCollapsed = gItems.length > GROUP_COLLAPSE_THRESHOLD && !expandedGroups.has(group.id);
      const activeStages = stages.filter((st) => (itemsByGroupStage.get(`${group.id}::${st.id}`) || []).length > 0);
      const stagePlans = activeStages.map((stage) => {
        const sItems = itemsByGroupStage.get(`${group.id}::${stage.id}`) || [];
        const pedId = `stage:${group.id}:${stage.id}`;
        const forceExpanded = expandedPedestals.has(pedId);
        const aggregated = !forceExpanded && sItems.length > AGGREGATE_THRESHOLD;
        const visibleCount = aggregated ? AGGREGATE_TOP_K + 1 : sItems.length;
        const dims = pedestalDimensionsForCount(visibleCount);
        return {
          pedId,
          stage,
          items: sItems,
          aggregated,
          visibleCount,
          dims,
        };
      });

      // Arrange stage pedestals into two spacious depth tiers (3 front, remaining back)
      // so the tree reads like a genuine branching UNIX directory landscape.
      const frontStages = stagePlans.slice(0, Math.ceil(stagePlans.length / 2));
      const backStages = stagePlans.slice(Math.ceil(stagePlans.length / 2));
      const stageGapX = 2.6; // FSN xDirMargin-inspired sideways gap

      const rowWidth = (list) => {
        if (!list.length) return 4.4;
        return list.reduce((acc, s) => acc + s.dims.width, 0) + Math.max(0, list.length - 1) * stageGapX;
      };

      const subtreeWidth = autoCollapsed
        ? Math.max(5.8, activeStages.length * 1.15 + 1.8)
        : Math.max(6.0, rowWidth(frontStages), rowWidth(backStages));

      return {
        group,
        items: gItems,
        autoCollapsed,
        stagePlans,
        frontStages,
        backStages,
        stageGapX,
        subtreeWidth,
      };
    });

    const groupGapX = 4.6; // Generous open space between top-level directory branches
    const totalGroupSpan = groupPlans.reduce((acc, p) => acc + p.subtreeWidth, 0) + Math.max(0, groupPlans.length - 1) * groupGapX;
    let cursorX = -totalGroupSpan / 2;

    const LEVEL1_Z = -13.5; // Deep FSN level separation (~8+ clear ground gap)
    const TIER2A_Z = -27.5;
    const TIER2B_Z = -41.5;

    for (const plan of groupPlans) {
      const gx = cursorX + plan.subtreeWidth / 2;
      cursorX += plan.subtreeWidth + groupGapX;

      const gId = `group:${plan.group.id}`;
      const gWidth = plan.autoCollapsed ? Math.max(5.6, plan.stagePlans.length * 1.12 + 1.4) : 4.8;
      const gDepth = plan.autoCollapsed ? 3.2 : 2.8;
      const gHeight = Math.min(0.68, 0.34 + Math.log10(plan.items.length + 1) * 0.14);

      const groupNode = {
        id: gId,
        nodeType: 'group',
        groupId: plan.group.id,
        label: plan.group.name,
        parentId: rootNode.id,
        x: gx,
        y: 0,
        z: LEVEL1_Z,
        width: gWidth,
        depth: gDepth,
        height: gHeight,
        counts: outcomeCounts(plan.items),
        collapsed: plan.autoCollapsed,
        stageCount: plan.stagePlans.length,
        childrenIds: [],
      };
      pedestals.push(groupNode);
      nodeById.set(groupNode.id, groupNode);
      rootNode.childrenIds.push(groupNode.id);

      // Elbowed parent-child ground wire: root -> group
      wires.push({
        id: `wire:root->${gId}`,
        parentId: rootNode.id,
        childId: gId,
        points: [
          [rootNode.x, 0.055, rootNode.z - rootNode.depth / 2],
          [rootNode.x, 0.055, -6.8],
          [groupNode.x, 0.055, -6.8],
          [groupNode.x, 0.055, groupNode.z + groupNode.depth / 2],
        ],
      });

      if (plan.autoCollapsed) {
        // Group > 50 items collapsed into SpaceTree stage summary towers directly on group pedestal
        const nSt = plan.stagePlans.length;
        const pitch = 1.08;
        const startX = groupNode.x - ((nSt - 1) * pitch) / 2;
        plan.stagePlans.forEach((sp, idx) => {
          const counts = outcomeCounts(sp.items);
          const bx = startX + idx * pitch;
          const bz = groupNode.z;
          const h = Math.min(3.6, 0.75 + Math.log2(counts.total + 1) * 0.45);
          const sumNode = {
            id: `stage-summary:${plan.group.id}:${sp.stage.id}`,
            nodeType: 'stage-summary',
            groupId: plan.group.id,
            stageId: sp.stage.id,
            parentId: groupNode.id,
            label: `${sp.stage.name} (${counts.total})`,
            x: bx,
            y: groupNode.height,
            z: bz,
            width: 0.78,
            depth: 0.78,
            height: h,
            counts,
          };
          summaryBlocks.push(sumNode);
          nodeById.set(sumNode.id, sumNode);
        });
        continue;
      }

      // Place Tier 2A (front stages) and Tier 2B (deeper stages)
      const placeStageRow = (list, zPos, busZ, isBackRow) => {
        if (!list.length) return;
        const span = list.reduce((acc, s) => acc + s.dims.width, 0) + Math.max(0, list.length - 1) * plan.stageGapX;
        let sx = gx - span / 2;

        // Route back-row trunk cleanly down the side/aisle of the group so it never clips a front pedestal
        const aisleX = gx + (plan.frontStages.length % 2 === 0 ? 0 : (span / 2 + 1.4));

        for (const sp of list) {
          const cx = sx + sp.dims.width / 2;
          sx += sp.dims.width + plan.stageGapX;

          const stageNode = {
            id: sp.pedId,
            nodeType: 'stage',
            groupId: plan.group.id,
            stageId: sp.stage.id,
            label: sp.stage.name,
            fullLabel: `${plan.group.name} / ${sp.stage.name}`,
            parentId: groupNode.id,
            x: cx,
            y: 0,
            z: zPos,
            width: sp.dims.width,
            depth: sp.dims.depth,
            height: sp.dims.height,
            counts: outcomeCounts(sp.items),
            aggregated: sp.aggregated,
            hiddenCount: sp.aggregated ? sp.items.length - AGGREGATE_TOP_K : 0,
            childrenIds: [],
          };
          pedestals.push(stageNode);
          nodeById.set(stageNode.id, stageNode);
          groupNode.childrenIds.push(stageNode.id);

          if (!isBackRow) {
            wires.push({
              id: `wire:${gId}->${sp.pedId}`,
              parentId: gId,
              childId: sp.pedId,
              points: [
                [groupNode.x, 0.055, groupNode.z - groupNode.depth / 2],
                [groupNode.x, 0.055, busZ],
                [stageNode.x, 0.055, busZ],
                [stageNode.x, 0.055, stageNode.z + stageNode.depth / 2],
              ],
            });
          } else {
            // Back-row stage wires branch cleanly along aisle to deeper bus
            wires.push({
              id: `wire:${gId}->${sp.pedId}`,
              parentId: gId,
              childId: sp.pedId,
              points: [
                [groupNode.x, 0.055, groupNode.z - groupNode.depth / 2],
                [groupNode.x, 0.055, LEVEL1_Z - 4.4],
                [aisleX, 0.055, LEVEL1_Z - 4.4],
                [aisleX, 0.055, busZ],
                [stageNode.x, 0.055, busZ],
                [stageNode.x, 0.055, stageNode.z + stageNode.depth / 2],
              ],
            });
          }

          // Place upright file blocks on top of this stage pedestal
          const ranked = sortItemsByInterest(sp.items);
          const visibleItems = sp.aggregated ? ranked.slice(0, AGGREGATE_TOP_K) : ranked;
          const hiddenItems = sp.aggregated ? ranked.slice(AGGREGATE_TOP_K) : [];
          const totalSlots = visibleItems.length + (hiddenItems.length > 0 ? 1 : 0);
          const cols = sp.dims.cols;
          const rows = Math.ceil(totalSlots / cols);
          const gridW = cols * BLOCK_PITCH - BLOCK_GAP;
          const gridD = rows * BLOCK_PITCH - BLOCK_GAP;

          visibleItems.forEach((item, idx) => {
            const col = idx % cols;
            const row = Math.floor(idx / cols);
            const bx = stageNode.x - gridW / 2 + BLOCK_WIDTH / 2 + col * BLOCK_PITCH;
            const bz = stageNode.z + gridD / 2 - BLOCK_WIDTH / 2 - row * BLOCK_PITCH;
            const qBin = metricToQuantileBin(item.metric, breaks);
            const bh = QUANTILE_HEIGHTS[qBin];
            const blockNode = {
              id: `item:${item.id}`,
              nodeType: 'item',
              itemId: item.id,
              item,
              groupId: item.group,
              stageId: item.stage,
              parentId: stageNode.id,
              label: item.label,
              kind: item.kind,
              quantileBin: qBin + 1,
              x: bx,
              y: stageNode.height,
              z: bz,
              width: BLOCK_WIDTH,
              depth: BLOCK_WIDTH,
              height: bh,
            };
            blocks.push(blockNode);
            nodeById.set(blockNode.id, blockNode);
            itemNodeById.set(item.id, blockNode);
            stageNode.childrenIds.push(blockNode.id);
          });

          if (hiddenItems.length > 0) {
            const idx = visibleItems.length;
            const col = idx % cols;
            const row = Math.floor(idx / cols);
            const bx = stageNode.x - gridW / 2 + BLOCK_WIDTH / 2 + col * BLOCK_PITCH;
            const bz = stageNode.z + gridD / 2 - BLOCK_WIDTH / 2 - row * BLOCK_PITCH;
            const hCounts = outcomeCounts(hiddenItems);
            const moreNode = {
              id: `more:${plan.group.id}:${sp.stage.id}`,
              nodeType: 'more',
              pedestalId: stageNode.id,
              groupId: plan.group.id,
              stageId: sp.stage.id,
              parentId: stageNode.id,
              label: `+${hiddenItems.length} more`,
              hiddenCount: hiddenItems.length,
              hiddenItemIds: hiddenItems.map((it) => it.id),
              x: bx,
              y: stageNode.height,
              z: bz,
              width: 0.72,
              depth: 0.72,
              height: Math.min(2.8, 0.9 + Math.log2(hiddenItems.length + 1) * 0.35),
              counts: hCounts,
            };
            summaryBlocks.push(moreNode);
            nodeById.set(moreNode.id, moreNode);
            stageNode.childrenIds.push(moreNode.id);
          }
        }
      };

      placeStageRow(plan.frontStages, TIER2A_Z, -20.6, false);
      placeStageRow(plan.backStages, TIER2B_Z, -34.8, true);
    }

    // Compute world bounds for framing overview camera without compressing geometry
    let minX = -6;
    let maxX = 6;
    let minZ = -18;
    let maxZ = 4;
    for (const p of pedestals) {
      minX = Math.min(minX, p.x - p.width / 2);
      maxX = Math.max(maxX, p.x + p.width / 2);
      minZ = Math.min(minZ, p.z - p.depth / 2);
      maxZ = Math.max(maxZ, p.z + p.depth / 2);
    }

    return {
      pedestals,
      blocks,
      summaryBlocks,
      wires,
      nodeById,
      itemNodeById,
      quantileBreaks: breaks,
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
        groupCollapseThreshold: GROUP_COLLAPSE_THRESHOLD,
      },
    };
  }

  global.RoomViewLayout = {
    BLOCK_WIDTH,
    BLOCK_GAP,
    QUANTILE_HEIGHTS,
    computeQuantileBreaks,
    metricToQuantileBin,
    sortItemsByInterest,
    outcomeCounts,
    buildFsnLayout,
  };
})(window);
