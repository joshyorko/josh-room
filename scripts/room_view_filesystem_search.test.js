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
    walk: {generation: 'g1', start: 'root'}, generation: () => 'g1',
    displayed: [{node: file('deep-match')}],
    controller: new AbortController(),
    bar: {hidden: false, children: ['deep-match'], replaceChildren() { this.children = []; }},
    inspect: async (id, generation) => {
      calls.push([id, generation]);
      if (id !== 'root') throw Object.assign(new Error('changed'), {status: 409});
    },
    status() {}, button(label) { this.bar.children.push(label); },
  });
  await assert.rejects(search.validateDisplayed(), /changed/);
  assert.deepEqual(calls, [['root', 'g1'], ['f-deep-match', 'g1']]);
  assert.equal(search.controller.signal.aborted, true);
  assert.equal(search.walk, null);
  assert.deepEqual(search.bar.children, ['Restart search']);
});

test('the search stays bound to its origin while stepping through identically named files', async t => {
  class Element extends EventTarget {
    constructor() { super(); this.children = []; this.attributes = {}; }
    setAttribute(key, value) { this.attributes[key] = value; }
    contains(child) { return child === this || this.children.some(node => node.contains(child)); }
    appendChild(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
    focus() { document.activeElement = this; }
  }
  const elements = {};
  const original = global.document;
  global.document = {
    createElement: () => new Element(),
    getElementById: id => elements[id] ??= new Element(),
  };
  t.after(() => { global.document = original; });
  const nested = dir('nested');
  const matches = [src, nested].map(parent => ({...file('README'), id: parent.id + '-readme', parent_id: parent.id}));
  let currentFolder = src;
  const calls = [], selected = [], highlighted = [];
  const search = new global.RoomViewFilesystemSearch({
    list: async id => {
      calls.push(id);
      if (id === src.id) return page(src, [root], [nested, matches[0]]);
      assert.equal(id, nested.id);
      return page(nested, [root, src], [matches[1]]);
    },
    inspect: async () => {},
    directory: () => currentFolder.id, root: () => root.id, generation: () => 'g1',
    select: node => { selected.push(node.id); currentFolder = node.parent_id === src.id ? src : nested; },
    onMatches: (ids, active) => highlighted.push([ids, active]),
  });
  search.scope.value = 'subtree';
  search.scope.selectedOptions = [{textContent: 'This subtree'}];
  search.input.value = 'readme';
  await search.start();
  assert.deepEqual(search.displayed.map(hit => hit.fullPath), ['/src/README', '/src/nested/README']);
  const controls = () => search.bar.children.find(el => el.className === 'search-controls').children;
  const press = label => {
    const button = controls().find(el => el.textContent === label);
    assert.equal(button.disabled, false);
    button.dispatchEvent(new Event('click'));
  };
  press('Next match');
  press('Next match');
  assert.equal(currentFolder, nested);
  assert.equal(search.walk.start, src.id);
  assert.equal(search.walk.startPath, '/src');
  assert.equal(search.active.ordinal, 2);
  assert.equal(search.expanded, false);
  press('Show results');
  assert.equal(search.bar.children.find(el => el.textContent === '/src/nested/README').attributes['aria-current'], 'true');
  press('Previous match');
  assert.deepEqual(selected, [matches[0].id, matches[1].id, matches[0].id]);
  assert.deepEqual(highlighted.at(-1), [matches.map(node => node.id), matches[0].id]);
  assert.deepEqual(calls, [src.id, nested.id]);
  assert.equal(search.input.value, 'readme');
});

test('a busy listing preserves earlier admitted matches and resumes the same cursor', async () => {
  const entries = [file('a'), file('b')];
  const calls = [];
  const walk = new SearchWalk(async (id, generation, cursor) => {
    calls.push(cursor);
    if (calls.length === 1) return page(root, [], [entries[0]], 'next', 'partial');
    if (calls.length === 2) throw Object.assign(new Error('busy'), {status: 429});
    return page(root, [], [entries[1]]);
  }, root.id, 'g1', false, '/');
  const first = await walk.advance(signal);
  assert.equal(first.more, true);
  assert.deepEqual(first.hits.map(hit => hit.node), [entries[0]]);
  const last = await walk.advance(signal);
  assert.equal(last.more, false);
  assert.deepEqual(last.hits.map(hit => hit.node), [entries[1]]);
  assert.deepEqual(calls, [null, 'next', 'next']);
});
