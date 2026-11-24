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
  app.forward.push('node/obsolete');
  await app._openFilesystem(file.id, { kind: 'file', refresh: true });
  assert.equal(app.currentNodeId, 'node/f12');
  assert.equal(app.data.fsGeneration, 'g2');
  assert.deepEqual(app.history, []);
  assert.deepEqual(app.forward, []);
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

test('selection commits the verified neighborhood atomically and sends the relative destination to the camera', async () => {
  let release;
  const app = appFixture(async op => op === 'preview' ? {node: preview} : {node: file});
  const oldBranch = {...other, parent_id: dir.id, fields: {coverage: 'complete'}};
  const currentPage = listing(dir, [file, oldBranch]);
  const branchFile = {...file, id: 'sibling-file', parent_id: oldBranch.id};
  const frames = [];
  const poses = [];
  Object.assign(app, {fsPage: currentPage, viewMode: '3d', sceneContext: [oldBranch, branchFile],
    scene3d: {
      setLayout(layout, options) { frames.push([layout, options]); },
      focusNode(target) { poses.push(target); },
    },
  });
  app.data.applyFilesystemNodes([root, dir, oldBranch, file], 'g');
  app._syncFromData();
  const sync = app._syncFromData;
  app._syncFromData = function(options) {
    sync.call(this);
    this._drawScene(options);
  };
  app.data.sceneNeighborhood = async (page, signal, prior) => {
    assert.deepEqual(prior, [oldBranch.id]);
    await new Promise(resolve => { release = resolve; });
    return [oldBranch, branchFile];
  };
  const selecting = app._openFilesystem(file.id, {kind: file.node_kind});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(frames.length, 0, 'previous verified world stays visible while the new observation is validated');
  assert.equal(app.currentNodeId, 'root');
  release();
  await selecting;
  assert.deepEqual(poses, ['node/f12']);
  assert.equal(frames.length, 1);
  assert.equal(frames[0][1].navigationTargetId, 'node/f12');
  assert.ok(frames[0][0].nodeById.has('node/sibling-file'));
  assert.equal(app.layout.nodeById.has('node/sibling-file'), false);
});

test('page continuation revalidates prior directory IDs without copying unobserved geometry', async () => {
  const folders = [0,1,2].map(i => ({...dir, id: 'old'+i, parent_id: root.id}));
  const calls = [];
  const data = Object.create(window.RoomViewData.RoomViewDataLayer.prototype);
  data.queryFilesystem = async (_, {id, generation}) => {
    calls.push(id);
    if (id === folders[1].id) throw Object.assign(new Error('changed'), {code: 'filesystem-scene-entry-stale'});
    return {directory: folders.find(f => f.id === id), generation, coverage: 'complete', entries: []};
  };
  const result = await data.sceneNeighborhood(listing(root, [file]), new AbortController().signal, folders.map(f => f.id));
  assert.deepEqual(calls, folders.map(f => f.id));
  assert.deepEqual(result.map(f => f.id), [folders[0].id, folders[2].id]);
});

test('transient sample congestion preserves the displayed frame; revoked authority removes every object immediately', async () => {
  const app = appFixture();
  app.viewMode = '3d';
  app.fsPage = listing(dir, [file]);
  const prior = app.sceneContext = [other];
  app.scene3d = {clearSpotlight() {}};
  app.data.applyFilesystemNodes([root, dir, file], 'g');
  app._syncFromData();
  app.currentNodeId = 'node/f12';
  app.evidence.target = file;
  let status = 429;
  app.data.sceneNeighborhood = async () => { throw Object.assign(new Error('sample error'), {status}); };
  await app._sampleScene(app.fsPage);
  assert.equal(app.sceneContext, prior);
  status = 403;
  await app._sampleScene(app.fsPage);
  assert.equal(app.fsPage, null);
  assert.equal(app.currentNodeId, 'root');
  assert.equal(app.evidence.target, null);
  assert.deepEqual(app.sceneContext, []);
  assert.equal(app.data.fsGeneration, null);
  assert.deepEqual(app.data.state.nodes.map(n => n.id), ['root']);
});

test('an in-place sibling edit refreshes only changed rows while the same selected reader and cursor stay active', async () => {
  const old = { ...file, id: 'sibling', metric: 10 };
  const updated = { ...old, id: 'sibling-revision-2', metric: 42 };
  const ops = [];
  const app = appFixture(async (op, params) => {
    ops.push([op, params]);
    if (op === 'metadata') return {generation: 'g', entries: [updated]};
    if (op === 'inspect') return {generation: 'g', node: file};
    throw new Error('unexpected read');
  });
  app.fsPage = listing(dir, [file, old], 'g', 'next-page');
  app.currentNodeId = 'node/f12';
  app.data.applyFilesystemNodes([root, dir, preview, old], 'g');
  app._syncFromData();
  app.evidence.target = preview;
  app.tooltipEl = { hidden: false, textContent: '10 bytes' };
  const changedRows = [];
  let syncs = 0;
  const sync = app._syncFromData.bind(app);
  app._syncFromData = () => { syncs++; sync(); };
  app.filesystemSearch = {
    reconcileMetadata(rows) { changedRows.push([...rows.keys()]); },
    validateDisplayed() {},
  };
  app.scene3d = { focusNode() { assert.fail('poll moved camera'); } };
  await app._pollFilesystem();
  await app._pollFilesystem();
  assert.deepEqual(ops.map(([op, p]) => [op, p.children || p.id]), [
    ['metadata', old.id], ['inspect', file.id], ['metadata', updated.id], ['inspect', file.id],
  ]);
  assert.equal(app.fsPage.cursor, 'next-page');
  assert.equal(app.currentNodeId, 'node/f12');
  assert.equal(app.evidence.target, preview);
  assert.equal(app.data.state.nodes.find(n => n.id === file.id), preview);
  assert.equal(app.layout.allLeavesByObjectId.get(updated.id).metric, 42);
  assert.deepEqual(changedRows, [[old.id]]);
  assert.equal(app.tooltipEl.hidden, true);
  assert.equal(app.tooltipEl.textContent, '');
  assert.equal(syncs, 1);
});

test('a page poll completing after navigation cannot patch or revoke the destination', async () => {
  let release;
  const app = appFixture(async (op) => {
    if (op === 'list') return listing(other, []);
    return new Promise(resolve => { release = resolve; });
  });
  app.fsPage = listing(dir, [file]);
  app.currentNodeId = 'node/dir';
  app.data.applyFilesystemNodes([root, dir, file], 'g');
  app._syncFromData();
  const pending = app._pollFilesystem();
  await app._openFilesystem(other.id, {kind: 'directory'});
  release({generation: 'stale', entries: []});
  await pending;
  assert.equal(app.fsPage.directory.id, other.id);
  assert.equal(app.currentNodeId, 'node/other');
});

test('selected-directory poll updates edited child geometry, grid metadata, and tooltip without redrawing when unchanged', async () => {
  const first = { ...file, id: 'f-a', label: 'alpha.txt', metric: 10, fields: { file_bytes: 10, object_key: 'k-alpha' } };
  const second = { ...file, id: 'f-b', label: 'beta.txt', metric: 20, fields: { file_bytes: 20, object_key: 'k-beta' } };
  const third = { ...file, id: 'f-c', label: 'gamma.txt', metric: 1000, fields: { file_bytes: 1000, object_key: 'k-gamma' } };
  const edited = { ...second, id: 'f-b-v2', metric: 50000, fields: { file_bytes: 50000, object_key: 'k-beta' } };
  let editedNow = false;
  const ops = [];
  const app = appFixture(async (op, params) => {
    ops.push([op, params]);
    if (op === 'metadata') {
      const byId = new Map([
        [first.id, first],
        [second.id, editedNow ? edited : second],
        [edited.id, edited],
        [third.id, third],
      ]);
      const rows = params.children.split(',').map(id => byId.get(id));
      return { generation: 'g', entries: rows };
    }
    if (op === 'inspect') return { generation: 'g', node: dir };
    throw new Error(`unexpected op ${op}`);
  });
  app.viewMode = '3d';
  app.fsPage = listing(dir, [first, second, third], 'g', 'page-2');
  app.currentNodeId = 'node/dir';
  app.data.applyFilesystemNodes([root, dir, first, second, third], 'g');
  app._syncFromData();
  const initialHeight = app.layout.nodeById.get('node/f-b').height;
  app.tooltipEl = { hidden: false, textContent: '20 bytes' };
  let sceneDraws = 0;
  let gridRenders = 0;
  let syncs = 0;
  const baseSync = app._syncFromData.bind(app);
  app._syncFromData = () => { syncs++; baseSync(); app._drawScene({ preserveCamera: true }); app._renderGrid(); };
  app._renderGrid = () => { gridRenders++; };
  app._sampleScene = async () => {};
  app.scene3d = {
    setLayout(layout, options) { sceneDraws++; this.lastLayout = layout; this.lastOptions = options; },
    focusNode() { assert.fail('poll moved camera'); },
  };
  // Unchanged poll does not redraw scene or grid or clear tooltip.
  await app._pollFilesystem();
  assert.equal(syncs, 0);
  assert.equal(sceneDraws, 0);
  assert.equal(gridRenders, 0);
  assert.equal(app.tooltipEl.hidden, false);
  // In-place sibling edit updates metadata, geometry height, grid, clears tooltip, preserves cursor & directory selection.
  editedNow = true;
  await app._pollFilesystem();
  assert.equal(syncs, 1);
  assert.equal(sceneDraws, 1);
  assert.equal(gridRenders, 1);
  assert.equal(app.scene3d.lastOptions.preserveCamera, true);
  assert.equal(app.fsPage.cursor, 'page-2');
  assert.equal(app.currentNodeId, 'node/dir');
  assert.equal(app.tooltipEl.hidden, true);
  assert.equal(app.tooltipEl.textContent, '');
  assert.equal(app.layout.allLeavesByObjectId.get(edited.id).fields.file_bytes, 50000);
  assert.ok(app.layout.nodeById.get('node/f-b-v2').height > initialHeight);
  // Subsequent unchanged poll with the new revision ID triggers zero additional redraws.
  await app._pollFilesystem();
  assert.equal(syncs, 1);
  assert.equal(sceneDraws, 1);
  assert.equal(gridRenders, 1);
});

test('a stale selected file reacquires its new revision via object_key with deferred content, while a replacement falls back to parent', async () => {
  const v1 = { ...file, id: 'v-1', fields: { file_bytes: 7, object_key: 'stable-key' } };
  const v2 = { ...file, id: 'v-2', fields: { file_bytes: 12, object_key: 'stable-key' } };
  const replaced = { ...file, id: 'v-3', fields: { file_bytes: 9, object_key: 'other-key' } };
  const previewV1 = { ...v1, previews: [{ slot: 'source', body: 'initial' }] };
  const previewV2 = { ...v2, previews: [{ slot: 'source', body: 'updated body' }] };
  let phase = 'initial';
  const showCalls = [];
  const app = appFixture(async (op, { id, generation }) => {
    if (phase === 'edited' && generation === 'g1') throw Object.assign(new Error('stale'), { status: 409 });
    if (phase === 'replaced' && generation === 'g2') throw Object.assign(new Error('stale'), { status: 409 });
    const gen = phase === 'initial' ? 'g1' : phase === 'edited' ? 'g2' : 'g3';
    if (op === 'preview') {
      return { generation: gen, node: id === v1.id ? previewV1 : previewV2 };
    }
    if (id === 'root') return listing(root, [dir], gen);
    return listing(dir, [phase === 'initial' ? v1 : phase === 'edited' ? v2 : replaced], gen);
  });
  app.evidence = {
    target: null,
    isOpen() { return !!this.target; },
    captureReadingPosition() {
      return this.target ? { id: this.target.objectId, objectKey: this.target.fields?.object_key, digest: 'd1' } : null;
    },
    async readingUnchanged() { return false; },
    showTarget(target, _d, _s, _l, opts = {}) { this.target = target; showCalls.push({ id: target.objectId, opts }); },
    hide() { this.target = null; },
  };
  await app._openFilesystem(dir.id, { kind: 'directory' });
  await app._openFilesystem(v1.id, { kind: 'file' });
  assert.equal(app.currentNodeId, 'node/v-1');

  // In-place edit: new revision ID, same object_key -> stays selected with deferred content prompt.
  phase = 'edited';
  await app._openFilesystem(v1.id, { kind: 'file', refresh: true });
  assert.equal(app.currentNodeId, 'node/v-2');
  assert.equal(app.data.fsGeneration, 'g2');
  assert.equal(showCalls.at(-1).id, 'v-2');
  assert.equal(showCalls.at(-1).opts.deferContent, true);
  assert.equal(showCalls.at(-1).opts.reading, null);

  // Unlink + recreate: different object_key -> falls back to parent directory and closes reader.
  phase = 'replaced';
  await app._openFilesystem(v2.id, { kind: 'file', refresh: true });
  assert.equal(app.currentNodeId, 'node/dir');
  assert.equal(app.data.fsGeneration, 'g3');
  assert.equal(app.evidence.isOpen(), false);
});
