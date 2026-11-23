'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

global.window = {};
require('../src/josh_room/view/viewer/layout.js');
require('../src/josh_room/view/viewer/scene3d.js');
const { buildFsnLayout, outcomeCounts } = window.RoomViewLayout;
const workspace = { tool: 'josh-room', group_label: 'Directory' };

function branch(n) {
  return [
    { id: 'root', node_kind: 'directory', label: 'Root' },
    { id: 'dir', parent_id: 'root', node_kind: 'directory', label: 'Dir' },
    ...Array.from({ length: n }, (_, index) => {
      const id = 'f' + String(index).padStart(2, '0');
      return { id, parent_id: 'dir', node_kind: 'file', label: id, kind: 'source' };
    }),
  ];
}

test('the selected block remains in view as its directory crosses the aggregation threshold', () => {
  for (const count of [16, 17]) {
    const layout = buildFsnLayout(workspace, { nodes: branch(count) }, { selectedNodeId: 'node/f12' });
    assert.ok(layout.nodeById.has('node/f12'), count + ' files');
    if (count > 16) assert.equal(layout.blocks.length, 11);
  }
});

test('expanded platforms leave aisles between every pair of decks', () => {
  const nodes = branch(63);
  nodes.push({ id: 'child', parent_id: 'dir', node_kind: 'directory', label: 'Child' });
  const expanded = new Set(['node/dir']);
  const layout = buildFsnLayout(workspace, { nodes }, { expandedDirs: expanded, expandedPedestals: expanded });
  for (const [index, a] of layout.pedestals.entries()) {
    for (const b of layout.pedestals.slice(index + 1)) {
      const dx = Math.abs(a.x - b.x) - (a.width + b.width) / 2;
      const dz = Math.abs(a.z - b.z) - (a.depth + b.depth) / 2;
      assert.ok(Math.max(dx, dz) >= 1, a.id + '/' + b.id + ' clearance ' + Math.max(dx, dz));
    }
  }
});

test('a selected block moving between slots carries its spotlight on layout refresh', () => {
  const makeLayout = (metric) => buildFsnLayout({}, { nodes: branch(2).map((node) => ({
    ...node, metric: node.id === 'f01' ? metric : 1,
  })) });
  const scene = Object.create(window.RoomViewScene3D.FsnScene3D.prototype);
  Object.assign(scene, {
    spotState: { dimL: 1, spotL: 1 }, stats: {}, manualCamera: true, selectedNodeId: 'node/f01',
    _blockLodState: new Map(), THREE: { Vector3: class {
      constructor(x, y, z) { Object.assign(this, { x, y, z }); }
    } }, camPos: { add(delta) { this.dx = delta.x; } }, camTarget: { add() {} },
    camera: { position: { copy() {} }, lookAt() {}, updateProjectionMatrix() {} },
    _clearWorldGroup() {}, _buildWorldGeometry() {}, _computeOverviewCamera() {},
    _updateBlockMatrices() {}, _positionSpotlightOnState() {}, requestRender() {},
  });
  scene.layout = makeLayout(0);
  const before = scene.layout.nodeById.get(scene.selectedNodeId);
  scene._startSpotlightOnBlock(before);
  scene.setLayout(makeLayout(2), { preserveCamera: true });
  const after = scene.layout.nodeById.get(scene.selectedNodeId);
  assert.notEqual(before.x, after.x);
  assert.equal(scene.spotState.targetX, after.x);
  assert.equal(scene.camPos.dx, after.x - before.x);
  assert.equal(scene.spotState.blockTop, after.y + after.height);
});

test('custom shapes do not turn ordinary content into skipped or failed outcomes', () => {
  const descriptor = { legend: {
    artifact: { shape: 'notch' }, redacted: { shape: 'hollow' }, error: { shape: 'solid' },
  } };
  const counts = outcomeCounts(['artifact', 'redacted', 'error'].map(kind => ({ kind })), descriptor);
  assert.deepEqual([counts.fail, counts.skip, counts.other], [1, 0, 2]);
});

test('selecting a deep file reveals every hidden ancestor in one layout', () => {
  const nodes = branch(52);
  nodes.push({ id: 'deep', parent_id: 'dir', node_kind: 'directory', label: 'Deep' },
    { id: 'last', parent_id: 'deep', node_kind: 'directory', label: 'Last' });
  nodes.filter(node => node.node_kind === 'file').forEach(node => { node.parent_id = 'last'; });
  const selected = 'node/f43';
  const layout = buildFsnLayout(workspace, { nodes }, { selectedNodeId: selected });
  assert.ok(layout.nodeById.has(selected));
  const scene = Object.create(window.RoomViewScene3D.FsnScene3D.prototype);
  scene.layout = layout;
  assert.deepEqual(scene._directoryWaypoints('root', 'node/last').map(n => n.objectId), ['dir', 'deep']);
  assert.deepEqual(scene._directoryWaypoints('node/last', 'root').map(n => n.objectId), ['deep', 'dir']);
});

test('file size changes height without reordering the directory', () => {
  const nodes = branch(2);
  const slots = () => {
    const blocks = buildFsnLayout(workspace, { nodes }).blocks;
    return blocks.map(b => ({ x: b.x, height: b.height }));
  };
  nodes.at(-1).metric = 10;
  nodes.at(-2).metric = 0;
  const before = slots();
  nodes.at(-2).metric = 1000000;
  const after = slots();
  assert.deepEqual(after.map(b => b.x), before.map(b => b.x));
  assert.notEqual(after[0].height, before[0].height);
});

test('staggered rows of differently sized expanded directories have disjoint footprints', () => {
  const nodes = branch(0);
  const expanded = new Set(['node/dir']);
  for (let d = 0; d < 8; d++) {
    const folder = 'child' + d;
    nodes.push({ id: folder, parent_id: 'dir', node_kind: 'directory', label: folder });
    expanded.add('node/' + folder);
    for (let n = 0; n < (d + 1) * 16; n++) {
      const id = folder + '-' + n;
      nodes.push({ id, parent_id: folder, label: id, node_kind: 'file' });
    }
  }
  const layout = buildFsnLayout(workspace, { nodes }, { expandedDirs: expanded, expandedPedestals: expanded });
  for (const [index, a] of layout.pedestals.entries()) {
    for (const b of layout.pedestals.slice(index + 1)) {
      const dx = Math.abs(a.x - b.x) - (a.width + b.width) / 2;
      const dz = Math.abs(a.z - b.z) - (a.depth + b.depth) / 2;
      assert.ok(Math.max(dx, dz) >= 1, a.id + '/' + b.id);
    }
  }
});

test('explicit collapse hides files and subdirectories while preserving a selected descendant', () => {
  const nodes = branch(2);
  nodes.push({ id: 'inner', parent_id: 'dir', node_kind: 'directory', label: 'Inner' });
  const collapsedDirs = new Set(['node/dir']);
  const layout = selectedNodeId => buildFsnLayout(workspace, { nodes }, { collapsedDirs, selectedNodeId });
  const collapsed = layout('node/dir');
  assert.equal(collapsed.nodeById.get('node/dir').collapsed, true);
  assert.deepEqual(collapsed.nodeById.get('node/dir').childrenIds, []);
  assert.equal(collapsed.nodeById.has('node/inner'), false);
  assert.equal(collapsed.allTreeNodesById.has('node/inner'), true);
  assert.equal(layout('node/f01').nodeById.has('node/f01'), true);
});
