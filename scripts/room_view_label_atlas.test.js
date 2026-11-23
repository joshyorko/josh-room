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
    measureText: text => ({ width: text.length * 7.5 }), scale() {},
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
    const visible = scene.labelPool.filter(label => label.mesh.visible);
    assert.ok(visible.includes(shared[0]), 'selected file is the first collision priority');
    for (const label of visible) {
      const r = label.screenRect;
      assert.ok(Math.abs(r.right - r.left - label.width) < 0.01);
      assert.ok(Math.abs(r.bottom - r.top - label.height) < 0.01);
      assert.ok(r.left >= 0 && r.top >= 0 && r.right <= 1280 && r.bottom <= 720);
      assert.ok(label.mesh.material.depthTest, 'foreground geometry occludes labels');
      for (const other of visible.filter(item => item !== label)) {
        const b = other.screenRect;
        assert.ok(r.right <= b.left || b.right <= r.left || r.bottom <= b.top || b.bottom <= r.top);
      }
    }
    const textureCount = 3 + visible.filter(label => label.nodeId === scene.selectedNodeId || label.nodeId === scene.hoveredNodeId).length;
    assert.equal(liveTextures.size - lightingTextures.size, textureCount);
    assert.equal(scene.labelAtlas.cache.size, textureCount);
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
  vm.runInContext(fs.readFileSync(path.join(view, 'viewer', 'layout.js'), 'utf8'), context);
  const nodes = [{ id: 'root', label: '/', node_kind: 'directory' }];
  for (let i = 0; i < 7; i++) {
    const id = 'd' + i;
    nodes.push({ id, parent_id: 'root', label: id, node_kind: 'directory' });
    for (let j = 0; j < 8; j++) {
      nodes.push({ id: id + '-' + j, parent_id: id, label: 'dir-' + j, node_kind: 'directory' });
    }
  }
  const layout = window.RoomViewLayout.buildFsnLayout({}, { nodes });
  const overlay = (left, top, right, bottom) => ({
    dataset: {}, getBoundingClientRect: () => ({ left, top, right, bottom, width: right-left, height: bottom-top }),
  });
  Object.assign(scene, { THREE, frameOverlays: [overlay(0, 0, 1280, 95), overlay(0, 752, 1280, 800),
    overlay(8, 100, 220, 740), overlay(870, 98, 1272, 735)], requestRender() {},
    camera: new THREE.PerspectiveCamera(42, 1.6, 0.25, 650),
    canvas: { getBoundingClientRect: () => ({ left: 0, top: 0, width: 1280, height: 800 }) },
  });
  scene._updateFraming();
  assert.deepEqual({...scene.safeRect}, { left: 226, top: 101, right: 864, bottom: 746, width: 638, height: 645 });
  scene.layout = layout;
  const assertVisible = (x,y,z) => {
    const ndc = new THREE.Vector3(x,y,z).project(scene.camera);
    const sx = (ndc.x + 1) * 640;
    const sy = (1 - ndc.y) * 400;
    assert.ok(sx >= scene.safeRect.left && sx <= scene.safeRect.right);
    assert.ok(sy >= scene.safeRect.top && sy <= scene.safeRect.bottom);
  };
  for (const id of ['root', 'node/d6']) {
    const directory = layout.nodeById.get(id);
    const pose = scene._directoryPose(directory);
    scene.camera.position.copy(pose.pos);
    scene.camera.lookAt(pose.target);
    scene.camera.updateMatrixWorld();
    for (const p of layout.pedestals.filter(p => p.id === id || p.parentId === id || p.id === directory.parentId)) {
      for (const x of [p.x-p.width/2, p.x+p.width/2]) {
        for (const z of [p.z-p.depth/2, p.z+p.depth/2]) assertVisible(x,p.height,z);
      }
    }
    for (const wire of layout.wires.filter(w => w.childId === id || w.parentId === id)) {
      for (const point of wire.points) assertVisible(...point);
    }
  }
  Object.assign(scene, { manualCamera: true, overviewTarget: new THREE.Vector3(), overviewPos: new THREE.Vector3() });
  const openReader = scene.frameOverlays.pop();
  scene._updateFraming();
  assert.equal(scene.safeRect.right, 1280);
  assert.equal(scene.safeRect.left, 226);
  scene.frameOverlays.push(openReader);
  scene._updateFraming();
  assert.equal(scene.safeRect.right, 864);
  // A detached map must use its complete output area even while the reader is open.
  const interactive = scene.camera;
  Object.assign(scene, { scene: new THREE.Scene(), groundMesh: new THREE.Object3D(),
    _updateBlockMatrices() {}, _updateFloorLabels() {}, renderer: { render(world, camera) {
      assert.notEqual(interactive, camera);
      assert.equal(camera.view.enabled, false);
      const b = scene.layout.bounds;
      for (const x of [b.minX, b.maxX]) {
        for (const z of [b.minZ, b.maxZ]) {
          const projected = new THREE.Vector3(x,0,z).project(camera);
          assert.ok(Math.abs(projected.x) < 1 && Math.abs(projected.y) < 1);
        }
      }
    } },
  });
  scene.canvas.toDataURL = () => 'synthetic';
  assert.equal(scene.captureTopDownDataUrl(), 'synthetic');
  assert.equal(scene.camera, interactive);
  assert.equal(scene.safeRect.right, 864);
  // Portrait split: only the left 120 CSS pixels are available, along the full height.
  scene.canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 540, height: 1000 });
  scene.frameOverlays = [overlay(126, 0, 540, 1000)];
  scene._updateFraming();
  const narrow = scene._directoryPose(layout.nodeById.get('root'));
  scene.camera.position.copy(narrow.pos);
  scene.camera.lookAt(narrow.target);
  scene.camera.updateMatrixWorld();
  assert.equal(scene.safeRect.width, 120);
  for (const p of layout.pedestals.filter(p => p.parentId === 'root')) {
    for (const x of [p.x - p.width / 2, p.x + p.width / 2]) {
      const ndc = new THREE.Vector3(x, p.height, p.z).project(scene.camera);
      assert.ok((ndc.x + 1) * 270 < 120 && ndc.x >= -1 && ndc.z < 1);
    }
  }
  const floorTarget = {};
  scene.pickables = [floorTarget];
  scene.beamV2Mesh = { visible: true };
  scene.pointerNdc = new THREE.Vector2();
  scene.raycaster = {
    setFromCamera() {}, intersectObjects(objects) { assert.deepEqual(objects, [floorTarget]); return []; },
  };
  assert.equal(scene._pickAtEvent({ clientX: 800, clientY: 300 }), null);
  const { pos, target } = scene._directoryPose(layout.nodeById.get('root'));
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
