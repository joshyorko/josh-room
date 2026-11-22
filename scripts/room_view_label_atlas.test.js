'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const zlib = require('node:zlib');

const view = path.join(__dirname, '../src/josh_room/view');

test('live label textures follow the current layout across polls, theme changes and page disposal', async () => {
  const template = fs.readFileSync(path.join(view, 'assets/room-view-template.html'), 'utf8');
  const blob = template.match(/id="lib-three"[^>]*>([^<]+)</)[1];
  const moduleBytes = zlib.gunzipSync(Buffer.from(blob, 'base64'));
  const three = await import('data:text/javascript;base64,' + moduleBytes.toString('base64'));
  const liveTextures = new Set();
  const materials = new Set();
  const THREE = {
    ...three,
    WebGLRenderer: class {
      capabilities = { getMaxAnisotropy: () => 4 };
      shadowMap = {};
      setPixelRatio() {}
      setSize() {}
      compile() {}
    },
    CanvasTexture: class extends three.CanvasTexture {
      constructor(canvas) {
        super(canvas);
        liveTextures.add(this);
      }
      dispose() {
        for (const material of materials) assert.notEqual(material.map, this, 'texture still in use');
        assert.ok(liveTextures.delete(this), 'texture disposed twice');
        super.dispose();
      }
    },
    MeshBasicMaterial: class extends three.MeshBasicMaterial {
      constructor(options) {
        super(options);
        materials.add(this);
      }
      dispose() { materials.delete(this); super.dispose(); }
    },
  };
  const window = Object.assign(new EventTarget(), { innerWidth: 1280, innerHeight: 720 });
  const ctx = {
    createRadialGradient: () => ({ addColorStop() {} }),
    createLinearGradient: () => ({ addColorStop() {} }),
    clearRect() {}, fillRect() {}, beginPath() {}, arc() {}, roundRect() {}, fill() {}, stroke() {}, fillText() {},
  };
  const document = { createElement: () => ({ getContext: () => ctx }) };
  const context = vm.createContext({ window, document, performance, requestAnimationFrame: () => 1 });
  for (const name of ['layout', 'scene3d']) {
    vm.runInContext(fs.readFileSync(path.join(view, 'viewer', name + '.js'), 'utf8'), context);
  }
  const scene = new window.RoomViewScene3D.FsnScene3D(THREE, new EventTarget(), {});
  const lightingTextures = new Set(liveTextures);
  const update = (tick) => {
    const nodes = [
      { id: 'root', label: '/', node_kind: 'directory' },
      ...['a', 'b'].map((id) => ({ id, parent_id: 'root', label: 'shared', metric: 42 })),
      { id: 'changing', parent_id: 'root', label: 'reading-' + tick, metric: tick },
    ];
    scene.setLayout(window.RoomViewLayout.buildFsnLayout({}, { nodes }));
  };
  const textureFor = (title) => scene.labelPool.find((label) => label.title === title).mesh.material.map;
  update(0);
  const stable = textureFor('shared');
  let highlighted;
  for (let tick = 0; tick < 125; tick++) {
    update(tick);
    const shared = scene.labelPool.filter((label) => label.title === 'shared');
    assert.equal(shared.length, 2);
    assert.ok(shared.every((label) => label.mesh.material.map === stable));
    scene.camera.updateMatrixWorld();
    scene.selectedNodeId = shared[0].nodeId;
    scene.hoveredNodeId = scene.labelPool.find((label) => label.title === 'reading-' + tick).nodeId;
    scene._updateFloorLabels();
    const current = shared[0].mesh.material.map;
    assert.notEqual(current, stable);
    if (highlighted) assert.equal(current, highlighted);
    highlighted = current;
    // Root plus normal/highlighted variants for the two distinct file labels.
    assert.equal(liveTextures.size - lightingTextures.size, 5);
    assert.equal(scene.labelAtlas.cache.size, 5);
  }
  scene.setTheme('grass');
  assert.equal(scene.labelAtlas.cache.size, 3);
  assert.ok(!liveTextures.has(highlighted) && !liveTextures.has(stable));
  const hide = (persisted) => window.dispatchEvent(Object.assign(new Event('pagehide'), { persisted }));
  hide(true);
  assert.equal(scene.labelGroup.children.length, 4);
  assert.equal(scene.labelAtlas.cache.size, 3);
  hide(false);
  assert.equal(scene.labelGroup.children.length, 0);
  assert.equal(scene.labelAtlas.cache.size, 0);
  assert.deepEqual(liveTextures, lightingTextures);
});
