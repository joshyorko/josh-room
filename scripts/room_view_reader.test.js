'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
global.window = global;
require('../src/josh_room/view/viewer/saved_comparison.js');
require('../src/josh_room/view/viewer/layout.js');
require('../src/josh_room/view/viewer/evidence.js');

class Element extends EventTarget {
  hidden = false;
  dataset = {};
  style = {};
  classList = { toggle() {} };
  contains() { return false; }
  removeAttribute() {}
  setAttribute() {}
  getAttribute() { return null; }
  append() {}
  appendChild() {}
  querySelector() { return new Element(); }
  querySelectorAll() { return []; }
  replaceChildren() {}
  focus() { document.activeElement = this; }
  click() { this.dispatchEvent(new Event('click')); }
}

function setup(available) {
  const nodes = new Map();
  global.document = {
    activeElement: new Element(),
    createElement: () => new Element(),
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Element());
      return nodes.get(id);
    },
  };
  const tabs = ['content', 'details', 'evidence', 'compare', 'timeline'].map(name => {
    const tab = name === 'compare' ? document.getElementById('btn-compare-tab') : new Element();
    tab.dataset.tab = name;
    tab.getAttribute = key => key === 'data-tab' ? name : null;
    return tab;
  });
  const host = document.getElementById('inspector-drawer');
  host.querySelectorAll = () => tabs;
  host.querySelector = selector => tabs.find(tab => selector === '[data-tab="' + tab.dataset.tab + '"]');
  const queries = [];
  const drawer = new RoomViewEvidence.EvidenceDrawer({
    filesystem: true, comparison: true, fsGeneration: 'session',
    queryFilesystem(op) { queries.push(op); return new Promise(() => {}); },
  });
  // Count loads at the PDF rendering boundary. Selection, intents, and tab state are real.
  let attachments = 0;
  drawer._renderEvidence = () => { attachments++; };
  drawer._renderContent = drawer._renderDetails = drawer._renderTimeline = () => {};
  drawer._contentDigest = async () => 'same-content';
  const file = {
    objectId: 'file', label: 'sample.pdf', path: '/sample.pdf', nodeKind: 'artifact',
    fields: { file_bytes: 19 }, attachments: available ? [{ name: 'sample.pdf' }] : [],
    previews: available ? [] : [{ status: 'omitted', reason: 'File exceeds the preview limit' }],
  };
  const select = options => drawer.showTarget(file, { legend: {} }, {}, {}, options);
  select();
  return { drawer, file, select, queries, attachments: () => attachments };
}

test('selection opens a summary; reading, refresh, comparison and Back keep the same file', () => {
  const { drawer, select, queries, attachments } = setup(true);
  assert.equal(drawer.fileSummary.hidden, false);
  assert.equal(drawer.summaryInfo.textContent, '19 bytes');
  assert.equal(attachments(), 0);
  assert.deepEqual(queries, []);
  drawer.readButton.click();
  assert.equal(drawer.fileSummary.hidden, true);
  assert.equal(drawer.activeTab, 'evidence');
  assert.equal(attachments(), 1);
  select({ preserveTab: true });
  assert.equal(drawer.fileSummary.hidden, true);
  drawer.dismiss();
  assert.equal(drawer.fileSummary.hidden, false);
  assert.equal(document.activeElement, drawer.readButton);
  drawer.compareButton.click();
  assert.equal(drawer.activeTab, 'compare');
  assert.deepEqual(queries, ['points']);
  drawer.dismiss();
  drawer.readButton.click();
  assert.equal(drawer.activeTab, 'evidence');
  assert.equal(drawer.currentObjectId, 'file');
  assert.equal(attachments(), 1);
});

test('withheld content shows the omission reason and offers file details', () => {
  const { drawer } = setup(false);
  assert.match(drawer.summaryInfo.textContent, /preview limit/);
  assert.equal(drawer.readButton.textContent, 'File details');
  drawer.readButton.click();
  assert.equal(drawer.activeTab, 'details');
});

test('restoring or requesting a hidden section uses a visible file tab', () => {
  const { drawer } = setup(true);
  drawer.readButton.click();
  drawer.switchTab('content');
  assert.equal(drawer.activeTab, 'details');
  drawer.switchTab('timeline');
  assert.equal(drawer.activeTab, 'details');
  assert.equal(drawer.tabDetailsEl.hidden, false);
});

test('closing from the scene or a detached opener focuses the selected workspace control', () => {
  for (const tag of ['BODY', 'CANVAS', 'BUTTON']) {
    const { drawer } = setup(true);
    const opener = new Element();
    opener.tagName = tag;
    opener.isConnected = tag !== 'BUTTON';
    document.body = tag === 'BODY' ? opener : new Element();
    drawer.returnFocus = opener;
    const selected = new Element();
    document.querySelector = selector => selector.startsWith('#breadcrumbs') ? selected : null;
    drawer.dismiss();
    assert.equal(document.activeElement, selected);
    assert.match(document.getElementById('aria-live-region').textContent, /closed.*sample.pdf/);
  }
});

test('generic investigation presents only reported failure, source, and timeline capabilities', () => {
  const { drawer } = setup(false);
  drawer.data.filesystem = drawer.data.comparison = false;
  const run = { objectId: 'run1', nodeKind: 'run', label: 'Build', kind: 'fail',
    previews: [{ slot: 'execution', status: 'available', media_type: 'application/x-josh-run+json',
      body: JSON.stringify({ status: 'failed', exit_code: 1, steps: [{ name: 'compile', status: 'fail' }] }) }],
    links: [{ relation: 'output', target_id: 'log', label: 'Build log' }],
  };
  drawer.showTarget(run, { legend: {} }, { steps: [{ item_id: 'run1', name: 'compile' }] }, {});
  assert.equal(drawer.readButton.textContent, 'Investigate run');
  assert.match(drawer.summaryInfo.textContent, /failed.*compile.*exit 1/);
  assert.equal(drawer.runRelated.hidden, false);
  assert.equal(drawer.compareButton.hidden, true);
  drawer.readButton.click();
  assert.equal(drawer.activeTab, 'content');
  assert.equal(drawer.activePreviewSlot, 'execution');
  assert.equal(drawer.drawerEl.querySelector('[data-tab="timeline"]').hidden, false);
  drawer.showTarget({ ...run, objectId: 'log', nodeKind: 'file', links: [],
    previews: [{ slot: 'log', language: 'log', status: 'available', body: 'synthetic log' }] },
    { legend: {} }, { steps: [] }, {});
  assert.equal(drawer.readButton.textContent, 'Read log');
  assert.equal(drawer.runRelated.hidden, true);
  assert.equal(drawer.drawerEl.querySelector('[data-tab="timeline"]').hidden, true);
});

test('closing and reopening restores the same PDF position only with matching admitted content', async () => {
  const { drawer, select } = setup(true);
  drawer._contentDigest = async target => JSON.stringify(target.attachments);
  let restored;
  drawer._renderEvidence = (_target, reading) => { restored = reading; };
  drawer.readButton.click();
  const page = { dataset: { pageNum: '4' }, getBoundingClientRect: () => ({ top: -32, bottom: 257 }) };
  const viewport = { scrollLeft: 17, querySelectorAll: () => [page], getBoundingClientRect: () => ({ top: 10 }) };
  drawer.tabEvidenceEl.querySelector = selector => selector === '.pdf-viewport' ? viewport
    : { getAttribute: () => 'sample.pdf' };
  drawer._pdfScale = 1.8;
  drawer.dismiss();
  drawer.hide();
  select();
  await drawer._openReader('evidence');
  assert.equal(restored.page, 4);
  assert.equal(restored.pageOffset, 42);
  assert.equal(restored.pdfLeft, 17);
  assert.equal(restored.scale, 1.8);
  drawer.dismiss();
  drawer.hide();
  select();
  drawer._contentDigest = async () => 'changed';
  await drawer._openReader('evidence');
  assert.equal(restored, null);
});

test('a frozen observation containing only PDF metadata offers details', () => {
  const { drawer, file } = setup(true);
  drawer.data.filesystem = drawer.data.comparison = false;
  drawer.data.mode = 'offline';
  drawer.showTarget({ ...file, objectId: 'frozen', attachments: [
    { name: 'guide.pdf', media_type: 'application/pdf', size: 42, embedded: false },
  ] }, { legend: {} }, {}, {});
  assert.equal(drawer.readButton.textContent, 'File details');
  assert.match(drawer.summaryInfo.textContent, /omitted/);
  drawer.readButton.click();
  assert.equal(drawer.activeTab, 'details');
});
