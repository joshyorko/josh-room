'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

global.window = {};
require('../src/josh_room/view/viewer/layout.js');
require('../src/josh_room/view/viewer/grid2d.js');

class Element extends EventTarget {
  constructor(tag) {
    super();
    this.tag = tag;
    this.children = [];
    this.attributes = new Map();
  }

  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text || ''; }
  contains(element) { return this === element || this.children.some(child => child.contains(element)); }
  setAttribute(key, value) { this.attributes.set(key, value); }
  getAttribute(key) { return this.attributes.get(key); }
  appendChild(child) { this.children.push(child); }
  append(...children) { this.children.push(...children); }
  focus() { document.activeElement = this; }
}

function setup() {
  global.document = {
    body: { toggleAttribute() {} },
    createElement: tag => new Element(tag),
    getElementById: () => null,
  };
  const descriptor = { tool: 'josh-room', group_label: 'Directory' };
  const directory = (id, parent_id) => ({ id, label: id, node_kind: 'directory', parent_id });
  const file = (id, parent_id) => ({
    id, label: id, node_kind: 'file', parent_id, kind: 'source', metric: 64, fields: { file_bytes: 97405 },
  });
  const state = { nodes: [
    directory('room', null), directory('src', 'room'), file('unrelated', 'room'),
    file('abc', 'src'), directory('xyz', 'src'), file('deeper', 'xyz'),
  ] };
  const selected = [];
  const grid = new window.RoomViewGrid.GridFallbackView(new Element('div'), new Element('div'), null,
    { onSelectItem: id => selected.push(id) });
  const layout = window.RoomViewLayout.buildFsnLayout(descriptor, state);
  const render = (directoryId, coverage) => grid.render(descriptor, state, layout, {
    directoryId, selectedId: 'node/abc', coverage,
  });
  return { grid, selected, render };
}

test('directory grid browses only direct children, folders first, with exact file bytes', () => {
  const { grid, selected, render } = setup();
  render('node/src', 'partial');
  const rows = () => grid.contentEl.children[0].children[2].children;
  const names = () => rows().map(row => row.children[0].children[0].textContent);
  assert.deepEqual(names(), ['xyz', 'abc']);
  assert.equal(rows()[1].className, 'selected');
  assert.equal(rows()[1].children[2].textContent, '97405 B');
  rows()[0].children[0].children[0].dispatchEvent(new Event('click'));
  assert.deepEqual(selected, ['xyz']);
  rows()[1].children[0].children[0].focus();
  grid.setSearch('bc');
  assert.deepEqual(names(), ['abc']);
  assert.equal(document.activeElement, rows()[0].children[0].children[0]);
  assert.match(grid.summaryEl.textContent, /\/src · 1 of 2 observed entries · partial/);
});

test('an unresolved folder never claims an empty complete listing', () => {
  const { grid, render } = setup();
  render('node/removed');
  assert.match(grid.summaryEl.textContent, /Completeness unknown/);
  assert.equal(grid.contentEl.children[1].textContent, 'No entries available in this observation.');
});
