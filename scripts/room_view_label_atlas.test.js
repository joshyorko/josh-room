'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const zlib = require('node:zlib');

const view = path.join(__dirname, '../src/josh_room/view');

async function bundledThree() {
  const template = fs.readFileSync(path.join(view, 'assets/room-view-template.html'), 'utf8');
  const blob = template.match(/id="lib-three"[^>]*>([^<]+)</)[1];
  const bytes = zlib.gunzipSync(Buffer.from(blob, 'base64'));
  return import('data:text/javascript;base64,' + bytes.toString('base64'));
}

test('live label textures follow the current layout across polls, theme changes and page disposal', async () => {
  const three = await bundledThree();
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
  assert.equal(scene.camera.fov, 42);
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

test('camera frames expanded deck corners and recenters the selected file beside the open reader', async () => {
  const THREE = await bundledThree();
  const window = {};
  const context = vm.createContext({ window, performance });
  vm.runInContext(fs.readFileSync(path.join(view, 'viewer', 'scene3d.js'), 'utf8'), context);
  const scene = Object.create(window.RoomViewScene3D.FsnScene3D.prototype);
  const deck = { id: 'root', x: 0, z: 0, width: 6, depth: 20, height: 0.7 };
  const children = [-12, 0, 12].map((x, i) => ({
    id: 'd' + i, parentId: deck.id, x, z: -18, width: 5, depth: 4, height: 0.6,
  }));
  Object.assign(scene, { THREE, layout: { pedestals: [deck, ...children] },
    camera: new THREE.PerspectiveCamera(42, 1.6, 0.25, 650),
    canvas: { getBoundingClientRect: () => ({ left: 0, top: 0, width: 1280, height: 800 }) },
  });
  const { pos, target } = scene._directoryPose(deck);
  scene.camera.position.copy(pos);
  scene.camera.lookAt(target);
  scene.camera.updateMatrixWorld();
  for (const p of scene.layout.pedestals) {
    for (const x of [p.x - p.width / 2, p.x + p.width / 2]) {
      for (const z of [p.z - p.depth / 2, p.z + p.depth / 2]) {
        const point = new THREE.Vector3(x, p.height, z).project(scene.camera);
        assert.ok(Math.abs(point.x) < 0.91 && Math.abs(point.y) < 0.75);
      }
    }
  }
  scene._frameReader({ getBoundingClientRect: () => ({ left: 780, top: 100, width: 500, height: 600 }) });
  const onscreenX = (target.clone().project(scene.camera).x + 1) * 640;
  assert.ok(Math.abs(onscreenX - 390) < 0.01);
  scene._frameReader({ hidden: true });
  assert.ok(Math.abs(target.clone().project(scene.camera).x) < 0.001);

  Object.assign(scene, { camPos: pos, camTarget: target, requestRender() {}, cancelIntroAndTour() {},
    _directoryWaypoints: () => [], reducedMotion: false });
  const destination = pos.clone().add(new THREE.Vector3(3, 0, 4));
  scene._flyTo(destination, target);
  assert.equal(scene.flight.durationMs, 950);
  scene.reducedMotion = true;
  scene._flyTo(destination, target);
  assert.equal(scene.flight, null);
  assert.equal(scene.camPos.distanceTo(destination), 0);
});
