'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/josh_room/view/viewer/sql_console.js'), 'utf8');
const count = { id: 'count', name: 'Count', sql: 'SELECT COUNT(*) FROM items' };
const labels = { id: 'labels', name: 'Labels', sql: 'SELECT label FROM items' };

function setup(presets) {
  const document = {};
  class Element extends EventTarget {
    children = [];
    attributes = new Map();
    dataset = {};
    defaultValue = 'SELECT 1';
    constructor() { super(); this.value = this.defaultValue; }
    set textContent(text) { this.children = []; }
    set value(text) { this.text = text; this.selectionStart = this.selectionEnd = text.length; }
    get value() { return this.text; }
    contains(element) { return this.children.includes(element); }
    appendChild(element) { this.children.push(element); }
    setAttribute(key, value) { this.attributes.set(key, value); }
    getAttribute(key) { return this.attributes.get(key); }
    focus() { document.activeElement = this; }
  }
  const elements = new Map();
  document.getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  document.createElement = () => new Element();
  const window = {};
  vm.runInNewContext(source, { window, document });
  const executed = [];
  const data = {
    descriptor: { sql_presets: presets },
    runReadOnlySql(sql) {
      executed.push(sql);
      return { results: [], elapsedMs: 1 };
    },
  };
  const view = new window.RoomViewSql.SqlConsoleView(data);
  view.renderPresets(data.descriptor);
  return { document, view, input: view.inputEl, buttons: view.presetsEl, executed };
}

test('typing, paste, empty edits and caret survive successive provider updates', () => {
  const { document, view, input, executed } = setup([count, labels]);
  assert.equal(input.value, count.sql);
  input.focus();
  for (const query of ['SELECT lab', 'SELECT label FROM items', '']) {
    input.value = query;
    input.selectionStart = 0;
    input.selectionEnd = Math.min(3, query.length);
    input.dispatchEvent(new Event('input'));
    for (const presets of [[count, labels], [labels], []]) {
      view.renderPresets({ sql_presets: presets });
      assert.equal(input.value, query);
      assert.equal(document.activeElement, input);
      assert.equal(input.selectionStart, 0);
      assert.equal(input.selectionEnd, Math.min(3, query.length));
    }
  }
  assert.deepEqual(executed, []);
});

test('a chosen preset survives replacement and removal until the next explicit click', () => {
  const { document, view, input, buttons, executed } = setup([count, labels]);
  buttons.children[1].focus();
  buttons.children[1].dispatchEvent(new Event('click'));
  const replacement = { ...labels, sql: 'SELECT DISTINCT label FROM items' };
  view.renderPresets({ sql_presets: [replacement, count] });
  assert.equal(document.activeElement, buttons.children[0]);
  assert.equal(input.value, labels.sql);
  view.renderPresets({ sql_presets: [count] });
  assert.equal(input.value, labels.sql);
  view.renderPresets({ sql_presets: [count, replacement] });
  buttons.children[1].dispatchEvent(new Event('click'));
  assert.equal(input.value, replacement.sql);
  assert.deepEqual(executed, [labels.sql, replacement.sql]);
});

test('presets added after an empty initial descriptor preserve the fallback query', () => {
  const { view, input } = setup([]);
  view.renderPresets({ sql_presets: [count] });
  assert.equal(input.value, input.defaultValue);
});
