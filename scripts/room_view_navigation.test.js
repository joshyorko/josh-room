'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
global.window = {};
require('../src/josh_room/view/viewer/layout.js');
require('../src/josh_room/view/viewer/app.js');

const root = { id: 'root', parent_id: null, node_kind: 'directory', label: '/' };
const dir = { id: 'dir', parent_id: 'root', node_kind: 'directory', label: 'src' };
const other = { ...dir, id: 'other', label: 'tests' };
const file = { id: 'f12', parent_id: 'dir', node_kind: 'file', label: 'readme', kind: 'doc' };
const preview = { ...file, previews: [{ slot: 'source', body: 'fixture' }] };
function listing(directory, entries, generation = 'g', cursor = null) {
  return { ancestors: directory === root ? [] : [root], directory, entries, generation,
    cursor, examined: entries.length, omitted: 0, coverage: cursor ? 'partial' : 'complete' };
}
function appFixture(queryFilesystem) {
  const app = Object.create(window.RoomViewAppClass.prototype);
  Object.assign(app, {
    history: [], forward: [], currentNodeId: 'root',
    data: {
      descriptor: { tool: 'josh-room', group_label: 'Directory' }, state: { nodes: [root] },
      queryFilesystem, applyFilesystemNodes(nodes, generation) {
        this.state = { nodes };
        this.fsGeneration = generation;
      },
    },
    ariaTree: { syncFromWorld() {} }, _updateNavigationButtons() {},
    evidence: { isOpen() { return !!this.target; }, showTarget(target) { this.target = target; }, hide() { this.target = null; } },
    _syncFromData() {
      this.layout = window.RoomViewLayout.buildFsnLayout(this.data.descriptor, this.data.state,
        { selectedNodeId: this.currentNodeId });
    },
  });
  app._syncFromData();
  return app;
}

test('an old directory response cannot replace a newer location', async () => {
  let unblock;
  const app = appFixture(async (_, { id }) => {
    if (id === dir.id) await new Promise(resolve => { unblock = resolve; });
    return listing(id === dir.id ? dir : other, []);
  });
  const pending = app._openFilesystem(dir.id, { kind: 'directory' });
  await app._openFilesystem(other.id, { kind: 'directory' });
  unblock();
  await pending;
  assert.equal(app.currentNodeId, 'node/other');
  assert.deepEqual(app.history, ['root']);
});

test('continuation keeps a selected file visible while replacing the page', async () => {
  const app = appFixture(async (op, { cursor }) => op === 'preview'
    ? { generation: 'g', node: preview } : listing(dir, cursor ? [] : [file], 'g', cursor ? null : 'next'));
  await app._openFilesystem(dir.id, { kind: 'directory' });
  await app._openFilesystem(file.id, { kind: 'file' });
  await app._openFilesystem(dir.id, { kind: 'directory', cursor: 'next', pushHistory: false });
  assert.equal(app.currentNodeId, 'node/f12');
  assert.equal(app.layout.nodeById.get(app.currentNodeId).item.objectId, file.id);
  assert.equal(app.evidence.target.previews[0].body, 'fixture');
  assert.deepEqual(app.history, ['root', 'node/dir']);
});

test('a stale file handle rechecks ancestors and selects only the same admitted identity', async () => {
  let stale = false;
  let removed = false;
  const app = appFixture(async (op, { id, generation }) => {
    if (stale && generation === 'g') throw Object.assign(new Error('stale'), { status: 409 });
    const gen = stale ? 'g2' : 'g';
    if (op === 'preview') return { node: preview, generation: gen };
    return id === 'root' ? listing(root, [dir], gen) : listing(dir, removed ? [] : [file], gen);
  });
  await app._openFilesystem(dir.id, { kind: 'directory' });
  await app._openFilesystem(file.id, { kind: 'file' });
  stale = true;
  await app._openFilesystem(file.id, { kind: 'file', refresh: true });
  assert.equal(app.currentNodeId, 'node/f12');
  assert.equal(app.data.fsGeneration, 'g2');
  removed = true;
  app.data.fsGeneration = 'g';
  await app._openFilesystem(file.id, { kind: 'file', refresh: true });
  assert.equal(app.currentNodeId, 'node/dir');
  assert.equal(app.layout.allLeavesByObjectId.size, 0);
});

require('../src/josh_room/view/viewer/data_layer.js');

test('seven real directory IDs acquire a bounded landscape independently of the current listing', async () => {
  const folders = Array.from({length: 7}, (_, n) => ({ ...dir, id: 'dir' + n, label: 'folder' + n, fields: {coverage: 'not-examined'} }));
  const deeper = { ...dir, id: 'deep', parent_id: 'dir3' };
  const leaf = { ...file, id: 'context-file', parent_id: deeper.id, previews: [{body: 'private body'}] };
  const app = appFixture();
  Object.assign(app, { viewMode: '3d', sceneContext: [], scene3d: {setLayout(layout) { this.layout = layout; }} });
  const data = app.data;
  const calls = [];
  data.sceneNeighborhood = window.RoomViewData.RoomViewDataLayer.prototype.sceneNeighborhood;
  data.queryFilesystem = async (op, {id, sample, cursor, generation}) => {
    calls.push([op, id, sample, cursor, generation]);
    const directory = [deeper, ...folders].find(d => d.id === id);
    return {directory, generation: 'current', coverage: id === 'dir0' ? 'partial' : 'complete',
      entries: id === deeper.id ? [leaf] : id === 'dir3' ? [deeper] : [] };
  };
  const page = listing(root, folders, 'current');
  app.fsPage = page;
  data.applyFilesystemNodes([root, ...folders], 'current');
  app._syncFromData();
  await app._sampleScene(page);
  assert.deepEqual(calls.map(c => c[1]), ['dir3', 'dir0', 'dir6', 'deep']);
  assert.ok(calls.every(([op, , sample, cursor, gen]) =>
    op === 'list' && sample === '1' && !cursor && gen === 'current'));
  assert.equal(data.state.nodes.length, 8);
  assert.equal(app.layout.allLeavesByObjectId.size, 0);
  assert.equal(app.scene3d.layout.allLeavesByObjectId.get(leaf.id).previews.length, 0);
  assert.equal(app.scene3d.layout.nodeById.get('node/dir0').coverage, 'partial');
  assert.equal(app.scene3d.layout.nodeById.get('node/dir1').coverage, 'not-examined');
});

test('one unavailable scene branch preserves verified neighbors while generation and binding errors discard the pass', async () => {
  const folders = [0, 1, 2].map(n => ({ ...dir, id: 'folder-' + n }));
  const page = listing(root, folders, 'current');
  let code = 'filesystem-scene-entry-stale';
  const data = Object.create(window.RoomViewData.RoomViewDataLayer.prototype);
  data.queryFilesystem = async (_, { id }) => {
    if (id === folders[1].id && code) throw Object.assign(new Error(code), { code });
    return { directory: folders.find(n => n.id === id), entries: [], generation: page.generation, coverage: 'complete' };
  };
  const run = () => data.sceneNeighborhood(page, new AbortController().signal);
  for (code of ['filesystem-scene-entry-stale', 'filesystem-unknown']) {
    assert.deepEqual((await run()).map(n => n.id), ['folder-0', 'folder-2']);
  }
  for (code of ['filesystem-stale', 'workspace-binding-changed']) {
    await assert.rejects(run, { code });
  }
  code = null;
  assert.deepEqual((await run()).map(n => n.id), ['folder-1', 'folder-0', 'folder-2']);
});

test('late geometry from an abandoned sample cannot reappear after navigation', async () => {
  const app = appFixture();
  const page = listing(root, [dir]);
  let release;
  Object.assign(app, {viewMode: '3d', fsPage: page, sceneContext: [], scene3d: {}});
  app.data.sceneNeighborhood = () => new Promise(resolve => { release = resolve; });
  const pending = app._sampleScene(page);
  app._clearSceneContext();
  app.fsPage = listing(other, []);
  release([file]);
  await pending;
  assert.deepEqual(app.sceneContext, []);
  assert.equal(app.currentNodeId, 'root');
});
