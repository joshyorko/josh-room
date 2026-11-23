'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const {SearchWalk} = require('../src/josh_room/view/viewer/filesystem_search');

const file = label => ({id: 'f-' + label, label, node_kind: 'file'});
const dir = label => ({id: 'd-' + label, label, node_kind: 'directory'});
const root = dir('/');
const src = dir('src');
const signal = new AbortController().signal;

function page(directory, ancestors, entries, cursor = null, coverage = 'complete') {
  return {generation: 'g1', directory, ancestors, entries, cursor, coverage};
}

test('direct search resumes incomplete sibling admission and stops before descendants', async () => {
  const calls = [];
  const list = async (id, generation, cursor) => {
    calls.push([id, generation, cursor]);
    return cursor ? page(root, [], [src]) : page(root, [], [], 'next', 'partial');
  };
  const walk = new SearchWalk(list, root.id, 'g1', false, 'src');
  const {hits, more} = await walk.advance(signal);
  assert.deepEqual(calls, [[root.id, 'g1', null], [root.id, 'g1', 'next']]);
  assert.deepEqual(hits.map(hit => hit.fullPath), ['/src']);
  assert.equal(more, false);
});

test('recursive search returns target and ancestry in one hit and bounds each continuation', async () => {
  const folders = Array.from({length: 15}, (_, i) => dir('folder-' + i));
  const calls = [];
  const list = async id => {
    calls.push(id);
    if (id === root.id) return page(root, [], folders);
    const directory = folders.find(node => node.id === id);
    return page(directory, [root], directory === folders.at(-1) ? [file('Test.PY')] : []);
  };
  const walk = new SearchWalk(list, root.id, 'g1', true, 'test.py');
  assert.deepEqual(await walk.advance(signal), {hits: [], more: true});
  assert.equal(calls.length, 8);
  const {hits, more} = await walk.advance(signal);
  assert.deepEqual(hits[0].ancestors, [root, folders.at(-1)]);
  assert.equal(hits[0].fullPath, '/folder-14/Test.PY');
  assert.equal(more, false);
});

test('match overflow pages deliver each result and ignore unavailable folders truthfully', async () => {
  const entries = Array.from({length: 42}, (_, i) => file('hit-' + i));
  const list = async id => id === root.id ? page(root, [], [...entries, src]) : page(src, [root], [], null, 'reserved-boundary');
  const walk = new SearchWalk(list, root.id, 'g1', true, 'hit');
  let more = true;
  const all = [];
  while (more) {
    const batch = await walk.advance(signal);
    assert.ok(batch.hits.length <= 16);
    all.push(...batch.hits.map(hit => hit.node));
    more = batch.more;
  }
  assert.deepEqual(all, entries);
  assert.equal(walk.skipped, 1);
});

test('a changed generation or cancelled in-flight page cannot supply a search hit', async () => {
  const controller = new AbortController();
  const cancel = async () => { controller.abort(); return page(root, [], [src]); };
  await assert.rejects(new SearchWalk(cancel, root.id, 'g1', false, 'src').advance(controller.signal), /abort/i);
  const stale = async () => ({...page(root, [], [src]), generation: 'g2'});
  await assert.rejects(new SearchWalk(stale, root.id, 'g1', false, 'src').advance(signal), /Observation changed/);
});

test('a poll checks displayed deep hits and removes the stale result and continuation', async () => {
  const search = Object.create(global.RoomViewFilesystemSearch.prototype);
  const calls = [];
  Object.assign(search, {
    walk: {generation: 'g1'},
    displayed: [{node: file('deep-match')}],
    controller: new AbortController(),
    bar: {hidden: false, children: ['deep-match'], replaceChildren() { this.children = []; }},
    inspect: async (id, generation) => {
      calls.push([id, generation]);
      throw Object.assign(new Error('changed'), {status: 409});
    },
    status() {}, button(label) { this.bar.children.push(label); },
  });
  await assert.rejects(search.validateDisplayed(), /changed/);
  assert.deepEqual(calls, [['f-deep-match', 'g1']]);
  assert.equal(search.controller.signal.aborted, true);
  assert.equal(search.walk, null);
  assert.deepEqual(search.bar.children, ['Restart search']);
});
