'use strict';

const assert = require('node:assert/strict');
const { createHash, webcrypto } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const viewer = fs.readFileSync(path.join(__dirname, '../src/josh_room/view/viewer/data_layer.js'), 'utf8');

function payload(bytes, collection = 'items') {
  const attachments = bytes === null ? [] : [{
    name: 'frame.png',
    media_type: 'image/png',
    digest: createHash('sha256').update(bytes).digest('hex'),
  }];
  return { descriptor: {}, state: { [collection]: [{ id: 'sample', attachments }] } };
}

function fixture(initialPayload, fetch, options = {}) {
  const window = {};
  const urls = new Map();
  let count = 0;
  const URL = {
    createObjectURL(blob) {
      const url = 'blob:' + ++count;
      urls.set(url, blob);
      return url;
    },
    revokeObjectURL(url) {
      assert.ok(urls.delete(url));
    },
  };
  vm.runInNewContext(viewer, { window, URL, URLSearchParams, fetch, Blob, Uint8Array, crypto: webcrypto });
  const data = new window.RoomViewData.RoomViewDataLayer({ initialPayload, mode: 'live', ...options });
  return { data, urls, image: () => data.getAttachmentBlobUrl('sample', 'frame.png') };
}

const imageResponse = bytes => new Response(bytes, { headers: { 'Content-Type': 'image/png' } });

test('live polls retain unchanged images and replace or revoke changed and removed images', async () => {
  let bytes = 'old';
  let snapshot = payload(bytes);
  let loads = 0;
  const { data, urls, image } = fixture(snapshot, url => {
    if (url === '/state.json') return Response.json(snapshot);
    assert.equal(url, '/attachments/sample/frame.png');
    loads++;
    return imageResponse(bytes);
  });
  const old = await image();
  assert.equal(await urls.get(old.url).text(), 'old');
  await data.refreshLive();
  assert.equal(await image(), old);
  assert.equal(loads, 1);

  bytes = 'new';
  snapshot = payload(bytes);
  await data.refreshLive();
  const [fresh, same] = await Promise.all([image(), image()]);
  assert.equal(await urls.get(fresh.url).text(), 'new');
  assert.equal(fresh, same);
  assert.equal(urls.has(old.url), false);
  assert.equal(loads, 2);
  snapshot = payload(null);
  await data.refreshLive();
  assert.equal(urls.size, 0);
  assert.equal(await image(), null);
  assert.equal(loads, 2);
});

test('superseded fetch cannot publish its blob after a newer node attachment has loaded', async () => {
  let finishOld;
  const oldResponse = new Promise(resolve => { finishOld = resolve; });
  let loads = 0;
  const { data, image, urls } = fixture(payload('old', 'nodes'), url => {
    if (url === '/state.json') return Response.json(payload('new', 'nodes'));
    return ++loads === 1 ? oldResponse : imageResponse('new');
  });
  const pending = image();
  await data.refreshLive();
  const fresh = await image();
  finishOld(imageResponse('old'));
  assert.equal(await pending, null);
  assert.equal(await image(), fresh);
  assert.equal(urls.size, 1);
  assert.equal(loads, 2);
});

test('live bytes that disagree with the advertised digest can be retried', async () => {
  let bytes = 'different';
  const { image, urls } = fixture(payload('advertised'), () => imageResponse(bytes));
  assert.equal(await image(), null);
  assert.equal(urls.size, 0);
  bytes = 'advertised';
  const fresh = await image();
  assert.equal(await urls.get(fresh.url).text(), bytes);
});

test('an older poll finishing late retains the newest successful attachment state', async () => {
  let finishOlder;
  const older = new Promise(resolve => { finishOlder = resolve; });
  let polls = 0;
  const { data, image } = fixture(payload('initial'), url => {
    if (url === '/state.json') return ++polls === 1 ? older : Response.json(payload('new'));
    return imageResponse('new');
  });
  const pendingPoll = data.refreshLive();
  assert.equal(await data.refreshLive(), true);
  const fresh = await image();
  assert.ok(fresh);
  finishOlder(Response.json(payload('old')));
  assert.equal(await pendingPoll, false);
  assert.equal(await image(), fresh);
});

test('offline images reuse embedded SQLite bytes without fetching', async () => {
  let reads = 0;
  const db = {
    prepare() {
      reads++;
      return {
        bind(args) { assert.deepEqual([...args], ['sample', 'frame.png']); },
        step() { return true; },
        get() { return ['image/png', Buffer.from('embedded')]; },
        free() {},
      };
    },
  };
  const { data, image, urls } = fixture(payload('embedded'), () => assert.fail('offline fetch'), { db, mode: 'offline' });
  const embedded = await image();
  assert.equal(await urls.get(embedded.url).text(), 'embedded');
  assert.equal(await data.refreshLive(), false);
  assert.equal(await image(), embedded);
  assert.equal(reads, 1);
});
