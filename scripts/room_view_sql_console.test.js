'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const zlib = require('node:zlib');

const source = fs.readFileSync(path.join(__dirname, '../src/josh_room/view/viewer/sql_console.js'), 'utf8');
const count = { id: 'count', name: 'Count', sql: 'SELECT COUNT(*) FROM items' };
const labels = { id: 'labels', name: 'Labels', sql: 'SELECT label FROM items' };

function setup(presets, dataLayer) {
  const document = {};
  class Element extends EventTarget {
    children = [];
    attributes = new Map();
    dataset = {};
    defaultValue = 'SELECT 1';
    constructor() { super(); this.value = this.defaultValue; }
    set textContent(text) { this.children = []; this.content = text; }
    get textContent() { return this.content; }
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
  const data = dataLayer || {
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

test('native SQL results and acknowledged SQLite downloads follow the admitted page', async () => {
  const viewRoot = path.join(__dirname, '../src/josh_room/view');
  const template = fs.readFileSync(path.join(viewRoot, 'assets/room-view-template.html'), 'utf8');
  const embedded = id => zlib.gunzipSync(Buffer.from(template.match(new RegExp('id="' + id + '"[^>]*>([^<]+)</'))[1], 'base64'));
  const sqlContext = {};
  vm.runInNewContext(embedded('lib-sqljs').toString(), sqlContext);
  const SQL = await sqlContext.initSqlJs({ wasmBinary: embedded('lib-sqljs-wasm') });
  const window = {};
  vm.runInNewContext(fs.readFileSync(path.join(viewRoot, 'viewer/data_layer.js'), 'utf8'), { window, Blob, performance });
  const { RoomViewDataLayer, hydrateLiveSqlite } = window.RoomViewData;
  const node = (id, parent_id = null, node_kind = 'directory') => ({ id, parent_id, node_kind, label: id, updated_at: '2026-01-01T00:00:00Z' });
  const initialPayload = {
    descriptor: {}, state: { nodes: [node('bootstrap')], summary: { bootstrap_only: 'old' } },
    sensitivity: { level: 'internal', requires_acknowledgment: true }, server: { filesystem: true },
  };
  const db = hydrateLiveSqlite(SQL, initialPayload.descriptor, initialPayload.state, initialPayload.sensitivity);
  const data = new RoomViewDataLayer({ mode: 'live', sqlModule: SQL, db, rawSqliteBytes: db.export(), initialPayload });
  const { view, document } = setup([], data);
  const query = 'SELECT path FROM nodes ORDER BY path';
  const root = node('root'), dir = node('src', 'root');
  const file = { ...node('chapter.pdf', 'src', 'artifact'), attachments: [
    { name: 'document.pdf', media_type: 'application/pdf', size: 50, digest: 'abc' },
  ] };
  const page = [root, dir, file];
  data.applyFilesystemNodes(page, 'first');
  view.renderPresets(data.descriptor);
  data.queryFilesystem = async () => ({ generation: 'first', directory: dir,
    entries: [node('sample-only', 'src', 'file')], coverage: 'partial' });
  const samples = await data.sceneNeighborhood({ generation: 'first', directory: root, entries: [dir] }, new AbortController().signal);
  assert.ok(samples.some(n => n.id === 'sample-only'));
  const rows = q => Array.from(data.runReadOnlySql(q).results[0]?.values || [], row => Array.from(row));
  assert.deepEqual(rows(query), [['/'], ['/src'], ['/src/chapter.pdf']]);
  assert.deepEqual(rows('SELECT key FROM summary'), []);
  assert.deepEqual(rows('SELECT name, bytes FROM attachments'), [['document.pdf', null]]);
  assert.deepEqual(rows('SELECT bytes FROM attachment_blobs'), []);
  assert.equal(data.getSqliteDownloadBlob(), null);
  const ack = document.getElementById('sql-download-ack');
  assert.equal(view.downloadBtn.disabled, true);
  ack.checked = true;
  ack.dispatchEvent(new Event('change'));
  assert.equal(view.downloadBtn.disabled, false);
  const blob = data.getSqliteDownloadBlob({ ack: true });
  const downloaded = new SQL.Database(new Uint8Array(await blob.arrayBuffer()));
  assert.deepEqual(Array.from(downloaded.exec(query)[0].values, row => Array.from(row)), rows(query));
  assert.deepEqual(Array.from(downloaded.exec('SELECT COUNT(bytes) FROM attachments')[0].values[0]), [0]);
  assert.deepEqual(Array.from(downloaded.exec("SELECT value FROM meta WHERE key='scope'")[0].values[0]), ['live-native-directory-page']);
  downloaded.close();
  assert.equal(view.execute(query).rowCount, 3);
  data.applyFilesystemNodes([...page], 'first');
  view.renderPresets(data.descriptor);
  assert.ok(view.resultsEl.children.length);
  assert.equal(ack.checked, true);
  data.applyFilesystemNodes([root, node('other', 'root')], 'second');
  view.renderPresets(data.descriptor);
  assert.equal(ack.checked, false);
  assert.equal(view.resultsEl.children.length, 0);
  assert.deepEqual(rows(query), [['/'], ['/other']]);
  data.applyFilesystemNodes([root], null);
  // Recovery can issue a new generation before it installs a verified page.
  data.fsGeneration = 'recovering';
  view.renderPresets(data.descriptor);
  assert.throws(() => rows(query), /directory page/i);
  assert.equal(data.getSqliteDownloadBlob({ ack: true }), null);
  assert.equal(view.runBtn.disabled, true);
});
