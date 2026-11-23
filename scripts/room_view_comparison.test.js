'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  find(tag) {
    if (this.tag === tag) return this;
    for (const child of this.children) {
      const found = child.find(tag);
      if (found) return found;
    }
  }
}

const point = { id: 'handle', snapshot: 'older-point', date: '2026-01-01T00:00:00Z' };
const receipt = {
  snapshot: point.snapshot, date: point.date, workspace_snapshot_id: 'a'.repeat(64),
  tree_id: 'b'.repeat(64), saved: { size: 4, sha256: 'c'.repeat(64) },
  live: { size: 6, sha256: 'd'.repeat(64) }, change: 'modified', unified_diff: '+<script>example</script>',
};

function fixture(query) {
  const panel = new Element('div');
  const tab = new Element('button');
  const data = { comparison: true, fsGeneration: 'generation', queryFilesystem: query };
  const context = { document: { createElement: tag => new Element(tag) }, AbortController };
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../src/josh_room/view/viewer/saved_comparison.js'), 'utf8'), context);
  const view = new context.RoomSavedComparison(data, panel, tab);
  return { panel, tab, data, view };
}

const file = { nodeKind: 'file', objectId: 'opaque-file' };
const tick = () => new Promise(done => setImmediate(done));

test('each comparison requires an explicit exact recovery point and renders the receipt as text', async () => {
  const calls = [];
  const { view, panel } = fixture(async (op, args) => {
    calls.push([op, { ...args }]);
    return op === 'points' ? { points: [point] } : receipt;
  });
  view.select(file);
  assert.deepEqual(calls, []);
  view.open();
  await tick();
  const select = panel.find('select');
  const button = panel.find('button');
  assert.equal(button.disabled, true);
  select.value = point.id;
  select.listeners.change();
  await button.listeners.click();
  assert.deepEqual(calls, [
    ['points', { id: file.objectId, generation: 'generation' }],
    ['compare', { id: file.objectId, generation: 'generation', point: point.id }],
  ]);
  assert.equal(panel.find('pre').textContent, receipt.unified_diff);
  assert.equal(panel.find('dl').children.some(child => child.textContent === receipt.saved.sha256), true);
  select.value = '';
  select.listeners.change();
  assert.equal(panel.find('pre'), undefined);
});

test('changing the selected file discards an outstanding comparison and its plaintext', async () => {
  let finish, signal;
  const { panel, view } = fixture((op, _args, requestSignal) => {
    if (op === 'points') return Promise.resolve({points: [point]});
    signal = requestSignal;
    return new Promise(done => { finish = done; });
  });
  view.select(file);
  view.open();
  await tick();
  panel.find('select').value = point.id;
  panel.find('select').listeners.change();
  const pending = panel.find('button').listeners.click();
  view.select({ ...file, objectId: 'other-file' });
  assert.equal(signal.aborted, true);
  finish(receipt);
  await pending;
  assert.equal(panel.children.length, 0);
});

test('an unexpected receipt never replaces the selected recovery point', async () => {
  const { view, panel } = fixture(async op => op === 'points' ? { points: [point] } : { ...receipt, snapshot: 'newest' });
  view.select(file);
  view.open();
  await tick();
  panel.find('select').value = point.id;
  await panel.find('button').listeners.click();
  assert.equal(panel.find('dl'), undefined);
  assert.match(panel.find('p').textContent, /changed/);
});

test('directories and offline/unsupported sources cannot query recovery', () => {
  const { view, tab, data } = fixture(() => assert.fail('unexpected query'));
  view.select({ nodeKind: 'directory', objectId: 'directory' });
  assert.equal(tab.hidden, true);
  view.open();
  data.comparison = false;
  view.select(file);
  assert.equal(tab.hidden, true);
  view.open();
});

test('withheld digest and unavailable text are reported without inventing file equality', async () => {
  const { view, panel } = fixture(async op => op === 'points' ? { points: [point] } : {
    ...receipt, saved: { size: 2 ** 23, sha256: null }, live: { size: 2 ** 23, sha256: null },
    change: 'unknown', unified_diff: null,
  });
  view.select(file);
  view.open();
  await tick();
  panel.find('select').value = point.id;
  await panel.find('button').listeners.click();
  assert.match(panel.find('p').textContent, /unknown/);
  assert.equal(panel.find('dl').children.filter(child => child.textContent === 'Unavailable within the read limit').length, 2);
  assert.equal(panel.find('pre'), undefined);
});
