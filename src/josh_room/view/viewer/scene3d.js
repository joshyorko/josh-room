// Full-bleed SGI FSN / Jurassic Park 3D Filesystem Navigator (Three.js r170, core-only).
// Implements Night (default) & Grass palettes, NeutralToneMapping, FSN 0.6x restraint,
// infinite fwidth world grid, low forward camera with swooping swing-back flights,
// Spotlight v2 + Baseline A/B, 5-quantile instanced blocks with WCAG shape cues,
// distance LOD with 15% hysteresis, floor-anchored canvas-atlas labels, and render-on-demand.
(function (global) {
  'use strict';

  const THEMES = {
    night: {
      skyZenith: 0x04060c,
      skyHorizon: 0x1a2840,
      skyBand: 0x2a4672,
      fogColor: 0x1a2840,
      fogDensity: 1.73 / 180,
      groundNear: 0x0b1220,
      groundFar: 0x152238,
      gridColor: 0x3d6fa8,
      gridAlpha: 0.35,
      hemiSky: 0x8aa4d6,
      hemiGround: 0x1a1f2a,
      hemiIntensity: 1.0,
      hemiDim: 0.22,
      keyColor: 0xc2d8ff,
      keyIntensity: 0.82,
      keyDim: 0.15,
      headlightColor: 0x9ec5ff,
      headlightIntensity: 0.34,
      headlightDim: 0.04,
      pedestalTop: 0x5c697e,
      pedestalSelected: 0x73849d,
      pedestalEdge: 0x8ba2c2,
      wireBase: 0x4fd8ff,
      wireSelected: 0xffffff,
      wireGlowAlpha: 0.09,
      pass: 0x3fb68b,
      fail: 0xf0673a,
      skip: 0x8a93a6,
      failCrown: 0xffd5c7,
      labelBg: 'rgba(6, 11, 20, 0.84)',
      labelBorder: 'rgba(79, 216, 255, 0.50)',
      labelText: '#e9f3ff',
      labelSub: '#8ecae6',
      spotV2Color: 0xfff0d8,
      spotV2Intensity: 1450,
      beamV2Color: 0xffe4b5,
      beamV2Peak: 0.28,
      beamBaseOpacity: 0.34,
      poolBaseOpacity: 1.0,
      additiveBeam: true,
    },
    grass: {
      // Exact verified SGI Fsn app-defaults Grass landscape palette (Research 1/5)
      skyZenith: 0x0087d5,
      skyHorizon: 0x91fff0,
      skyBand: 0xb8fff6,
      fogColor: 0x91fff0,
      fogDensity: 1.73 / 215,
      groundNear: 0x0f4f1a,
      groundFar: 0x418a59,
      gridColor: 0x7bc1a7,
      gridAlpha: 0.30,
      hemiSky: 0xdff6ff,
      hemiGround: 0x1e5c2c,
      hemiIntensity: 1.0,
      hemiDim: 0.24,
      keyColor: 0xfff7e2,
      keyIntensity: 0.88,
      keyDim: 0.17,
      headlightColor: 0xdaf4ff,
      headlightIntensity: 0.25,
      headlightDim: 0.04,
      pedestalTop: 0x708084,
      pedestalSelected: 0x8ea2a4,
      pedestalEdge: 0xb6d6cc,
      wireBase: 0x7bc1a7,
      wireSelected: 0xffffff,
      wireGlowAlpha: 0.04,
      pass: 0x007a5a,
      fail: 0xc2410c,
      skip: 0x6b7280,
      failCrown: 0xffefe8,
      labelBg: 'rgba(10, 32, 18, 0.84)',
      labelBorder: 'rgba(145, 255, 240, 0.55)',
      labelText: '#f4fff8',
      labelSub: '#a8f0d4',
      spotV2Color: 0xfffee2,
      spotV2Intensity: 1320,
      beamV2Color: 0xfff0c2,
      beamV2Peak: 0.25,
      beamBaseOpacity: 0.22,
      poolBaseOpacity: 0.70,
      additiveBeam: false,
    },
  };

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  function sigmoidEase(t) {
    return 0.5 * (1 - Math.cos(Math.PI * clamp(t, 0, 1)));
  }

  function smootherstep(t) {
    const x = clamp(t, 0, 1);
    return x * x * x * (x * (x * 6 - 15) + 10);
  }

  function createSpotMapTexture(THREE) {
    const size = 256;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    const cx = size / 2;
    const grad = ctx.createRadialGradient(cx, cx, 0, cx, cx, cx);
    // Research 3/5: 0.85 at r=0, 1.0 at r=0.40, 0 at r=0.50 (normalized to radius cx: 0, 0.80, 1.0)
    grad.addColorStop(0.0, 'rgba(217, 217, 217, 1)');
    grad.addColorStop(0.76, 'rgba(255, 255, 255, 1)');
    grad.addColorStop(0.92, 'rgba(140, 140, 140, 1)');
    grad.addColorStop(1.0, 'rgba(0, 0, 0, 1)');
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cx, cx, 0, Math.PI * 2);
    ctx.fill();
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.needsUpdate = true;
    return tex;
  }

  function createBaselineBeamAlphaMap(THREE) {
    const canvas = document.createElement('canvas');
    canvas.width = 64;
    canvas.height = 256;
    const ctx = canvas.getContext('2d');
    const grad = ctx.createLinearGradient(0, 0, 0, 256);
    grad.addColorStop(0.0, '#000000');
    grad.addColorStop(0.18, '#d8d8d8');
    grad.addColorStop(0.84, '#ffffff');
    grad.addColorStop(1.0, '#000000');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 64, 256);
    const tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    return tex;
  }

  function createBaselinePoolTexture(THREE) {
    const size = 256;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    const cx = size / 2;
    const grad = ctx.createRadialGradient(cx, cx, 0, cx, cx, cx);
    grad.addColorStop(0.0, 'rgba(255, 241, 201, 0.92)');
    grad.addColorStop(0.45, 'rgba(255, 232, 175, 0.55)');
    grad.addColorStop(0.82, 'rgba(255, 224, 150, 0.14)');
    grad.addColorStop(1.0, 'rgba(255, 224, 150, 0.0)');
    ctx.clearRect(0, 0, size, size);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, size, size);
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.needsUpdate = true;
    return tex;
  }

  function createCrossCrownGeometry(THREE, width) {
    // Distinct physical "X" crown geometry for WCAG 1.4.1 non-colour Fail cue
    const barLen = width * 0.88;
    const barThick = width * 0.18;
    const barHeight = 0.16;
    const g1 = new THREE.BoxGeometry(barLen, barHeight, barThick);
    g1.rotateY(Math.PI / 4);
    const g2 = new THREE.BoxGeometry(barLen, barHeight, barThick);
    g2.rotateY(-Math.PI / 4);
    g1.translate(0, barHeight / 2, 0);
    g2.translate(0, barHeight / 2, 0);
    return mergeSimpleGeometries(THREE, [g1, g2]);
  }

  function createHollowPostsGeometry(THREE, width) {
    // 4 vertical corner posts (unit height 0..1) for hollow Skip cage
    const t = width * 0.13;
    const off = (width - t) / 2;
    const parts = [];
    for (const sx of [-1, 1]) {
      for (const sz of [-1, 1]) {
        const g = new THREE.BoxGeometry(t, 1.0, t);
        g.translate(sx * off, 0.5, sz * off);
        parts.push(g);
      }
    }
    return mergeSimpleGeometries(THREE, parts);
  }

  function createHollowRingGeometry(THREE, width) {
    // Open square collar frame (top/bottom rails of hollow Skip cage, unstretched height)
    const t = width * 0.13;
    const off = (width - t) / 2;
    const h = 0.08;
    const parts = [];
    const gN = new THREE.BoxGeometry(width, h, t);
    gN.translate(0, h / 2, -off);
    const gS = new THREE.BoxGeometry(width, h, t);
    gS.translate(0, h / 2, off);
    const gW = new THREE.BoxGeometry(t, h, width - 2 * t);
    gW.translate(-off, h / 2, 0);
    const gE = new THREE.BoxGeometry(t, h, width - 2 * t);
    gE.translate(off, h / 2, 0);
    parts.push(gN, gS, gW, gE);
    return mergeSimpleGeometries(THREE, parts);
  }

  function mergeSimpleGeometries(THREE, geoms) {
    let totalVerts = 0;
    let totalIndices = 0;
    for (const g of geoms) {
      totalVerts += g.attributes.position.count;
      totalIndices += g.index ? g.index.count : g.attributes.position.count;
    }
    const positions = new Float32Array(totalVerts * 3);
    const normals = new Float32Array(totalVerts * 3);
    const indices = new Uint32Array(totalIndices);
    let vOff = 0;
    let iOff = 0;
    for (const g of geoms) {
      const pos = g.attributes.position;
      const norm = g.attributes.normal;
      positions.set(pos.array, vOff * 3);
      normals.set(norm.array, vOff * 3);
      if (g.index) {
        const idx = g.index.array;
        for (let k = 0; k < idx.length; k += 1) {
          indices[iOff + k] = idx[k] + vOff;
        }
        iOff += idx.length;
      } else {
        for (let k = 0; k < pos.count; k += 1) {
          indices[iOff + k] = vOff + k;
        }
        iOff += pos.count;
      }
      vOff += pos.count;
      g.dispose();
    }
    const merged = new THREE.BufferGeometry();
    merged.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    merged.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
    merged.setIndex(new THREE.BufferAttribute(indices, 1));
    return merged;
  }

  function createWireRibbonMesh(THREE, points, width, height, material) {
    // Build a continuous ground-hugging ribbon along orthogonal FSN polyline segments
    const geoms = [];
    for (let i = 0; i < points.length - 1; i += 1) {
      const [x1, y1, z1] = points[i];
      const [x2, y2, z2] = points[i + 1];
      const dx = x2 - x1;
      const dz = z2 - z1;
      const len = Math.hypot(dx, dz);
      if (len < 0.001) continue;
      const g = new THREE.BoxGeometry(len + width * 0.8, height, width);
      const angle = Math.atan2(-dz, dx);
      g.rotateY(angle);
      g.translate((x1 + x2) / 2, (y1 + y2) / 2, (z1 + z2) / 2);
      geoms.push(g);
    }
    if (!geoms.length) return null;
    const merged = mergeSimpleGeometries(THREE, geoms);
    return new THREE.Mesh(merged, material);
  }

  class FloorLabelAtlas {
    constructor(THREE, maxAnisotropy) {
      this.THREE = THREE;
      this.maxAnisotropy = Math.min(8, maxAnisotropy || 4);
      this.cache = new Map();
    }

    clear() {
      for (const entry of this.cache.values()) {
        entry.tex.dispose();
      }
      this.cache.clear();
    }

    getTexture(title, subtitle, themeKey, highlighted) {
      const key = `${themeKey}|${highlighted ? '1' : '0'}|${title}|${subtitle || ''}`;
      if (this.cache.has(key)) return this.cache.get(key);
      const t = THEMES[themeKey] || THEMES.night;
      const canvas = document.createElement('canvas');
      canvas.width = 512;
      canvas.height = subtitle ? 160 : 120;
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      // Clean architectural plate with high contrast (>= 3:1)
      ctx.fillStyle = highlighted ? 'rgba(18, 36, 58, 0.94)' : t.labelBg;
      ctx.strokeStyle = highlighted ? '#ffffff' : t.labelBorder;
      ctx.lineWidth = highlighted ? 6 : 3;
      const r = 16;
      const w = canvas.width - 12;
      const h = canvas.height - 12;
      ctx.beginPath();
      ctx.roundRect(6, 6, w, h, r);
      ctx.fill();
      ctx.stroke();

      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = highlighted ? '#ffffff' : t.labelText;
      ctx.font = '600 54px system-ui, -apple-system, Arial, sans-serif';
      const displayTitle = title.length > 18 ? `${title.slice(0, 17)}…` : title;
      ctx.fillText(displayTitle, canvas.width / 2, subtitle ? 58 : canvas.height / 2 + 2, canvas.width - 40);

      if (subtitle) {
        ctx.fillStyle = highlighted ? '#ffe8b6' : t.labelSub;
        ctx.font = '600 32px ui-monospace, SFMono-Regular, Menlo, monospace';
        const displaySub = subtitle.length > 26 ? `${subtitle.slice(0, 25)}…` : subtitle;
        ctx.fillText(displaySub, canvas.width / 2, 114, canvas.width - 40);
      }

      const tex = new this.THREE.CanvasTexture(canvas);
      tex.colorSpace = this.THREE.SRGBColorSpace;
      tex.generateMipmaps = true;
      tex.minFilter = this.THREE.LinearMipmapLinearFilter;
      tex.magFilter = this.THREE.LinearFilter;
      tex.anisotropy = this.maxAnisotropy;
      tex.needsUpdate = true;
      const entry = { tex, aspect: canvas.width / canvas.height };
      this.cache.set(key, entry);
      return entry;
    }
  }

  class FsnScene3D {
    constructor(THREE, canvas, callbacks) {
      this.THREE = THREE;
      this.canvas = canvas;
      this.callbacks = callbacks || {};
      this.themeKey = 'night';
      this.spotlightMode = 'v2'; // 'v2' | 'baseline'
      this.reducedMotion = Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
      this.gridVisible = true;
      this.idleTourEnabled = false;
      this.shrinkOnZoom = false;

      // Instrumentation counters for render-on-demand & perf receipts
      this.stats = {
        renderCount: 0,
        shadowUpdateCount: 0,
        drawCalls: 0,
        firstFrameMs: null,
        lastFirstClickMs: null,
        bootStartMs: performance.now(),
      };

      this._rafId = null;
      this._dirty = false;
      this._shadowDirty = false;
      this._disposed = false;

      this.layout = null;
      this.selectedNodeId = null;
      this.hoveredNodeId = null;
      this.focusedPedestalId = 'root';

      // Camera & navigation state (low forward pitch ~20° below horizon, FOV 60°)
      this.camPos = new THREE.Vector3(0, 14, 24);
      this.camTarget = new THREE.Vector3(0, 1.2, -16);
      this.overviewPos = new THREE.Vector3(0, 14, 24);
      this.overviewTarget = new THREE.Vector3(0, 1.2, -16);
      this.flight = null;
      this.establishingShot = null;
      this.towerRiseProgress = 1;
      this.lastInputTime = performance.now();

      // Spotlight animation state (fade 0..1; dimming 450ms, spot 100ms delay + 300ms)
      this.spotState = {
        active: false,
        targetActive: false,
        startTime: 0,
        dimL: 0,
        spotL: 0,
        fromDimL: 0,
        fromSpotL: 0,
        targetX: 0,
        targetZ: 0,
        floorY: 0.4,
        blockTop: 2.0,
        blockHalfDiag: 0.46,
      };

      // LOD hysteresis memory
      this._blockLodState = new Map();
      this._pedestalLodState = new Map();

      // Dynamic scene containers
      this.worldGroup = new THREE.Group();
      this.labelGroup = new THREE.Group();
      this.pickables = [];
      this.instancedRefs = {};
      this.pedestalVisuals = new Map();
      this.wireVisuals = [];
      this.labelPool = [];

      this._initRendererAndScene();
      this._initAtmosphereAndLights();
      this._initSpotlightRig();
      this._initPointerControls();
      this._precompileShaders();
    }

    _initRendererAndScene() {
      const THREE = this.THREE;
      this.renderer = new THREE.WebGLRenderer({
        canvas: this.canvas,
        antialias: true,
        powerPreference: 'high-performance',
      });
      this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      this.renderer.setSize(window.innerWidth, window.innerHeight, false);
      this.renderer.toneMapping = THREE.NeutralToneMapping;
      this.renderer.toneMappingExposure = 1.0;
      this.renderer.outputColorSpace = THREE.SRGBColorSpace;
      this.renderer.shadowMap.enabled = true;
      this.renderer.shadowMap.type = THREE.PCFShadowMap;
      this.renderer.shadowMap.autoUpdate = false;

      this.scene = new THREE.Scene();
      this.camera = new THREE.PerspectiveCamera(60, window.innerWidth / Math.max(1, window.innerHeight), 0.25, 650);
      this.camera.position.copy(this.camPos);
      this.camera.lookAt(this.camTarget);
      this.scene.add(this.camera);
      this.scene.add(this.worldGroup);
      this.scene.add(this.labelGroup);

      this.labelAtlas = new FloorLabelAtlas(THREE, this.renderer.capabilities.getMaxAnisotropy());
      this.raycaster = new THREE.Raycaster();
      this.pointerNdc = new THREE.Vector2();
    }

    _initAtmosphereAndLights() {
      const THREE = this.THREE;
      const t = THEMES[this.themeKey];

      this.scene.fog = new THREE.FogExp2(t.fogColor, t.fogDensity);
      this.scene.background = new THREE.Color(t.fogColor);

      // 1. Back-side gradient sky sphere (zenith -> horizon -> ground fade, follows camera XZ)
      const skyGeo = new THREE.SphereGeometry(420, 32, 20);
      this.skyUniforms = {
        uZenith: { value: new THREE.Color(t.skyZenith) },
        uHorizon: { value: new THREE.Color(t.skyHorizon) },
        uBand: { value: new THREE.Color(t.skyBand) },
        uGround: { value: new THREE.Color(t.groundNear) },
      };
      const skyMat = new THREE.ShaderMaterial({
        side: THREE.BackSide,
        depthWrite: false,
        fog: false,
        uniforms: this.skyUniforms,
        vertexShader: `
          varying vec3 vDir;
          void main() {
            vDir = normalize(position);
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }
        `,
        fragmentShader: `
          uniform vec3 uZenith;
          uniform vec3 uHorizon;
          uniform vec3 uBand;
          uniform vec3 uGround;
          varying vec3 vDir;
          void main() {
            float h = vDir.y;
            vec3 col;
            if (h >= 0.0) {
              float skyT = pow(clamp(h, 0.0, 1.0), 0.55);
              col = mix(uHorizon, uZenith, skyT);
              float band = exp(-h * 16.0);
              col = mix(col, uBand, band * 0.35);
            } else {
              float gT = pow(clamp(-h, 0.0, 1.0), 0.6);
              col = mix(uHorizon, uGround, gT);
            }
            gl_FragColor = vec4(col, 1.0);
            #include <colorspace_fragment>
          }
        `,
      });
      this.skyMesh = new THREE.Mesh(skyGeo, skyMat);
      this.skyMesh.renderOrder = -10;
      this.scene.add(this.skyMesh);

      // 2. Shadow-receiving ground plane
      const groundGeo = new THREE.PlaneGeometry(900, 900);
      groundGeo.rotateX(-Math.PI / 2);
      this.groundMat = new THREE.MeshStandardMaterial({
        color: t.groundNear,
        roughness: 0.92,
        metalness: 0.04,
      });
      this.groundMesh = new THREE.Mesh(groundGeo, this.groundMat);
      this.groundMesh.receiveShadow = true;
      this.groundMesh.position.y = 0;
      this.scene.add(this.groundMesh);

      // 3. Anti-aliased infinite world-space fwidth grid (MIT InfiniteGridHelper technique)
      const gridGeo = new THREE.PlaneGeometry(420, 420);
      gridGeo.rotateX(-Math.PI / 2);
      this.gridUniforms = {
        uColor: { value: new THREE.Color(t.gridColor) },
        uAlpha: { value: t.gridAlpha },
        uDistance: { value: 160.0 },
        uCamPos: { value: new THREE.Vector3() },
      };
      const gridMat = new THREE.ShaderMaterial({
        transparent: true,
        depthWrite: false,
        fog: false,
        uniforms: this.gridUniforms,
        vertexShader: `
          varying vec3 vWorldPos;
          void main() {
            vec4 wp = modelMatrix * vec4(position, 1.0);
            vWorldPos = wp.xyz;
            gl_Position = projectionMatrix * viewMatrix * wp;
          }
        `,
        fragmentShader: `
          uniform vec3 uColor;
          uniform float uAlpha;
          uniform float uDistance;
          uniform vec3 uCamPos;
          varying vec3 vWorldPos;

          float gridLine(vec2 coord, float scale) {
            vec2 r = coord / scale;
            vec2 fw = fwidth(r);
            vec2 grid = abs(fract(r - 0.5) - 0.5) / max(fw, vec2(0.0001));
            float line = min(grid.x, grid.y);
            float aa = 1.0 - min(line, 1.0);
            // Fade lines cleanly as fwidth approaches cell size to prevent moire
            float moireFade = 1.0 - smoothstep(0.35, 0.9, max(fw.x, fw.y));
            return aa * moireFade;
          }

          void main() {
            float d = length(vWorldPos.xz - uCamPos.xz);
            float distFade = pow(clamp(1.0 - d / uDistance, 0.0, 1.0), 3.0);
            float minor = gridLine(vWorldPos.xz, 1.0) * 0.42;
            float major = gridLine(vWorldPos.xz, 10.0) * 0.95;
            float g = max(minor, major);
            float alpha = g * uAlpha * distFade;
            if (alpha < 0.002) discard;
            gl_FragColor = vec4(uColor, alpha);
            #include <colorspace_fragment>
          }
        `,
      });
      this.gridMesh = new THREE.Mesh(gridGeo, gridMat);
      this.gridMesh.position.y = 0.012;
      this.gridMesh.renderOrder = 1;
      this.scene.add(this.gridMesh);

      // 4. Base lights (NO shadows on base lights; spotlight is the sole shadow caster)
      this.hemiLight = new THREE.HemisphereLight(t.hemiSky, t.hemiGround, t.hemiIntensity);
      this.scene.add(this.hemiLight);

      this.keyLight = new THREE.DirectionalLight(t.keyColor, t.keyIntensity);
      this.keyLight.position.set(28, 42, 34);
      this.keyLight.castShadow = false;
      this.scene.add(this.keyLight);

      // Camera-parented subtle headlight so destinations are never unlit
      this.headlight = new THREE.PointLight(t.headlightColor, t.headlightIntensity, 42, 1.5);
      this.headlight.position.set(0, 1.5, 0);
      this.camera.add(this.headlight);
    }

    _initSpotlightRig() {
      const THREE = this.THREE;
      const t = THEMES[this.themeKey];
      const h = 14.0;
      const poolR = 2.6;
      const angle = Math.atan(poolR / h);

      // Persistent real SpotLight (NEVER toggled visible/castShadow/map on click -> zero hitch!)
      this.spotMapTex = createSpotMapTexture(THREE);
      this.spotLight = new THREE.SpotLight(t.spotV2Color, 0, 0, angle, 0.35, 2);
      this.spotLight.position.set(0, h, 0);
      this.spotLight.target.position.set(0, 0, 0);
      this.spotLight.map = this.spotMapTex;
      this.spotLight.castShadow = true;
      this.spotLight.shadow.mapSize.set(1024, 1024);
      this.spotLight.shadow.radius = 3;
      this.spotLight.shadow.camera.near = h - 4.5;
      this.spotLight.shadow.camera.far = h + 2.5;
      this.spotLight.shadow.bias = -0.0005;
      this.spotLight.shadow.normalBias = 0.02;
      if ('intensity' in this.spotLight.shadow) {
        this.spotLight.shadow.intensity = 0.7;
      }
      this.scene.add(this.spotLight);
      this.scene.add(this.spotLight.target);

      // Spotlight v2 Volumetric Beam Cone (FrontSide, apex-distance fade, floor contact, block color protection, dithered)
      const coneGeoV2 = new THREE.CylinderGeometry(0.10, h * Math.tan(angle) * 1.02, h, 64, 1, true);
      // Shift so apex is at y=0 and base is at y=-h
      coneGeoV2.translate(0, -h / 2, 0);
      this.beamV2Uniforms = {
        uColor: { value: new THREE.Color(t.beamV2Color) },
        uPeak: { value: t.beamV2Peak },
        uFloorY: { value: 0.4 },
        uBlockTop: { value: 2.0 },
        uFade: { value: 0.0 },
        uHeight: { value: h },
      };
      this.beamV2Mat = new THREE.ShaderMaterial({
        side: THREE.FrontSide,
        transparent: true,
        depthWrite: false,
        fog: false,
        blending: t.additiveBeam ? THREE.AdditiveBlending : THREE.NormalBlending,
        uniforms: this.beamV2Uniforms,
        vertexShader: `
          varying vec3 vN;
          varying vec3 vW;
          varying float vT;
          uniform float uHeight;
          void main() {
            vT = clamp(-position.y / max(uHeight, 0.001), 0.0, 1.0);
            vec4 wp = modelMatrix * vec4(position, 1.0);
            vW = wp.xyz;
            vN = normalize(mat3(modelMatrix) * normal);
            gl_Position = projectionMatrix * viewMatrix * wp;
          }
        `,
        fragmentShader: `
          #include <common>
          uniform vec3 uColor;
          uniform float uPeak;
          uniform float uFloorY;
          uniform float uBlockTop;
          uniform float uFade;
          varying vec3 vN;
          varying vec3 vW;
          varying float vT;
          void main() {
            if (uFade < 0.001) discard;
            float nv = abs(dot(normalize(vN), normalize(cameraPosition - vW)));
            float a = uPeak * pow(nv, 1.65) * smoothstep(0.0, 0.16, vT) * (1.0 - 0.35 * vT)
                    * smoothstep(0.0, 0.55, vW.y - uFloorY)
                    * mix(0.42, 1.0, smoothstep(uBlockTop, uBlockTop + 1.0, vW.y)) * uFade;
            a += (rand(gl_FragCoord.xy) - 0.5) / 255.0;
            gl_FragColor = vec4(uColor, max(a, 0.0));
            #include <colorspace_fragment>
          }
        `,
      });
      this.beamV2Mesh = new THREE.Mesh(coneGeoV2, this.beamV2Mat);
      this.beamV2Mesh.frustumCulled = false;
      this.beamV2Mesh.renderOrder = 10;
      this.beamV2Mesh.userData = { isSpotBeam: true };
      this.scene.add(this.beamV2Mesh);

      // Known-Good Prototype Baseline Beam + Floor Pool (for side-by-side A/B comparison)
      const baseBeamGeo = new THREE.CylinderGeometry(0.1, 1.05, 12, 64, 1, true);
      baseBeamGeo.translate(0, -6.0, 0);
      this.baseBeamAlphaTex = createBaselineBeamAlphaMap(THREE);
      this.beamBaseMat = new THREE.MeshBasicMaterial({
        color: 0xfff1c9,
        alphaMap: this.baseBeamAlphaTex,
        transparent: true,
        opacity: 0,
        depthWrite: false,
        fog: false,
        side: THREE.FrontSide,
        blending: t.additiveBeam ? THREE.AdditiveBlending : THREE.NormalBlending,
      });
      this.beamBaseMesh = new THREE.Mesh(baseBeamGeo, this.beamBaseMat);
      this.beamBaseMesh.frustumCulled = false;
      this.beamBaseMesh.renderOrder = 10;
      this.beamBaseMesh.visible = false;
      this.beamBaseMesh.userData = { isSpotBeam: true };
      this.scene.add(this.beamBaseMesh);

      const poolGeo = new THREE.CircleGeometry(2.5, 48);
      poolGeo.rotateX(-Math.PI / 2);
      this.basePoolTex = createBaselinePoolTexture(THREE);
      this.poolBaseMat = new THREE.MeshBasicMaterial({
        map: this.basePoolTex,
        transparent: true,
        opacity: 0,
        depthWrite: false,
        fog: false,
        blending: t.additiveBeam ? THREE.AdditiveBlending : THREE.NormalBlending,
      });
      this.poolBaseMesh = new THREE.Mesh(poolGeo, this.poolBaseMat);
      this.poolBaseMesh.frustumCulled = false;
      this.poolBaseMesh.renderOrder = 5;
      this.poolBaseMesh.visible = false;
      this.scene.add(this.poolBaseMesh);
    }

    _precompileShaders() {
      // Pre-compile all materials including spotlight/shadow variants at boot
      // to guarantee zero first-click compilation hitch.
      try {
        this.renderer.compile(this.scene, this.camera);
      } catch (_) {
        // Safe fallback on older drivers
      }
    }

    _initPointerControls() {
      const dom = this.canvas;
      let dragging = false;
      let dragMoved = false;
      let startX = 0;
      let startY = 0;
      let lastX = 0;
      let lastY = 0;
      let isPan = false;

      const onPointerDown = (ev) => {
        if (ev.button !== 0 && ev.button !== 2) return;
        this.cancelIntroAndTour();
        dragging = true;
        dragMoved = false;
        isPan = ev.shiftKey || ev.button === 2;
        startX = lastX = ev.clientX;
        startY = lastY = ev.clientY;
        dom.setPointerCapture(ev.pointerId);
      };

      const onPointerMove = (ev) => {
        this.lastInputTime = performance.now();
        if (dragging) {
          const dx = ev.clientX - lastX;
          const dy = ev.clientY - lastY;
          if (Math.hypot(ev.clientX - startX, ev.clientY - startY) > 4) {
            dragMoved = true;
            this.flight = null;
          }
          lastX = ev.clientX;
          lastY = ev.clientY;
          if (isPan) {
            this._panCamera(dx, dy);
          } else {
            this._orbitCamera(dx, dy);
          }
          this.requestRender();
          return;
        }
        // Raycast only on pointermove (Research 2/5 #10)
        this._handleHover(ev);
      };

      const onPointerUp = (ev) => {
        if (!dragging) return;
        dragging = false;
        try { dom.releasePointerCapture(ev.pointerId); } catch (_) {}
        if (!dragMoved && ev.button === 0) {
          this._handleClick(ev);
        }
      };

      const onWheel = (ev) => {
        ev.preventDefault();
        this.cancelIntroAndTour();
        this.flight = null;
        const factor = ev.deltaY > 0 ? 1.12 : 0.89;
        const offset = this.camPos.clone().sub(this.camTarget);
        const dist = clamp(offset.length() * factor, 3.2, 135);
        offset.setLength(dist);
        this.camPos.copy(this.camTarget).add(offset);
        this.camPos.y = Math.max(0.75, this.camPos.y);
        this.camera.position.copy(this.camPos);
        this.camera.lookAt(this.camTarget);
        this.requestRender();
      };

      const onContextMenu = (ev) => ev.preventDefault();

      dom.addEventListener('pointerdown', onPointerDown);
      dom.addEventListener('pointermove', onPointerMove);
      dom.addEventListener('pointerup', onPointerUp);
      dom.addEventListener('wheel', onWheel, { passive: false });
      dom.addEventListener('contextmenu', onContextMenu);
      window.addEventListener('resize', () => this.resize());
    }

    _orbitCamera(dx, dy) {
      const THREE = this.THREE;
      const offset = this.camPos.clone().sub(this.camTarget);
      const spherical = new THREE.Spherical().setFromVector3(offset);
      spherical.theta -= dx * 0.0055;
      spherical.phi -= dy * 0.0045;
      // Low eye-level / forward-looking polar clamp: 58°..87.5° from zenith (Research 5/5 #5)
      spherical.phi = clamp(spherical.phi, (58 * Math.PI) / 180, (87.5 * Math.PI) / 180);
      offset.setFromSpherical(spherical);
      this.camPos.copy(this.camTarget).add(offset);
      this.camPos.y = Math.max(0.75, this.camPos.y);
      this.camera.position.copy(this.camPos);
      this.camera.lookAt(this.camTarget);
    }

    _panCamera(dx, dy) {
      const THREE = this.THREE;
      const forward = new THREE.Vector3();
      this.camera.getWorldDirection(forward);
      forward.y = 0;
      if (forward.lengthSq() < 0.0001) forward.set(0, 0, -1);
      forward.normalize();
      const right = new THREE.Vector3().crossVectors(forward, new THREE.Vector3(0, 1, 0)).normalize();
      const dist = this.camPos.distanceTo(this.camTarget);
      const speed = dist * 0.0018;
      const move = right.multiplyScalar(-dx * speed).add(forward.multiplyScalar(dy * speed));
      this.camPos.add(move);
      this.camTarget.add(move);
      const b = this.layout ? this.layout.bounds : { minX: -40, maxX: 40, minZ: -60, maxZ: 20 };
      this.camTarget.x = clamp(this.camTarget.x, b.minX - 18, b.maxX + 18);
      this.camTarget.z = clamp(this.camTarget.z, b.minZ - 18, b.maxZ + 18);
      this.camPos.y = Math.max(0.75, this.camPos.y);
      this.camera.position.copy(this.camPos);
      this.camera.lookAt(this.camTarget);
    }

    cancelIntroAndTour() {
      this.lastInputTime = performance.now();
      if (this.establishingShot) {
        this.establishingShot = null;
        this.towerRiseProgress = 1;
        this._updateBlockMatrices();
        this.requestRender();
      }
    }

    resize() {
      if (this._disposed) return;
      const w = window.innerWidth;
      const h = Math.max(1, window.innerHeight);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
      this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      this.renderer.setSize(w, h, false);
      this.requestRender();
    }

    setTheme(themeKey) {
      if (!THEMES[themeKey]) return;
      this.themeKey = themeKey;
      const t = THEMES[themeKey];
      this.skyUniforms.uZenith.value.setHex(t.skyZenith);
      this.skyUniforms.uHorizon.value.setHex(t.skyHorizon);
      this.skyUniforms.uBand.value.setHex(t.skyBand);
      this.skyUniforms.uGround.value.setHex(t.groundNear);
      this.scene.fog.color.setHex(t.fogColor);
      this.scene.fog.density = t.fogDensity * (1 + 0.3 * this.spotState.dimL);
      this.scene.background.setHex(t.fogColor);
      this.groundMat.color.setHex(t.groundNear);
      this.gridUniforms.uColor.value.setHex(t.gridColor);
      this.hemiLight.color.setHex(t.hemiSky);
      this.hemiLight.groundColor.setHex(t.hemiGround);
      this.keyLight.color.setHex(t.keyColor);
      this.headlight.color.setHex(t.headlightColor);

      this.spotLight.color.setHex(this.spotlightMode === 'v2' ? t.spotV2Color : 0xfff1c9);
      this.beamV2Uniforms.uColor.value.setHex(t.beamV2Color);
      this.beamV2Uniforms.uPeak.value = t.beamV2Peak;
      this.beamV2Mat.blending = t.additiveBeam ? this.THREE.AdditiveBlending : this.THREE.NormalBlending;
      this.beamV2Mat.needsUpdate = true;
      this.beamBaseMat.blending = t.additiveBeam ? this.THREE.AdditiveBlending : this.THREE.NormalBlending;
      this.beamBaseMat.needsUpdate = true;
      this.poolBaseMat.blending = t.additiveBeam ? this.THREE.AdditiveBlending : this.THREE.NormalBlending;
      this.poolBaseMat.needsUpdate = true;

      this.labelAtlas.clear();
      if (this.layout) {
        this._applyThemeToWorldMaterials();
      }
      this._applyLightingAndDimming();
      this.requestRender();
    }

    setSpotlightMode(mode) {
      if (mode !== 'v2' && mode !== 'baseline') return;
      this.spotlightMode = mode;
      const t = THEMES[this.themeKey];
      const h = mode === 'v2' ? 14.0 : 12.0;
      const poolR = Math.max(2.6, 1.35 * this.spotState.blockHalfDiag);
      if (mode === 'v2') {
        this.spotLight.color.setHex(t.spotV2Color);
        this.spotLight.distance = 0;
        this.spotLight.decay = 2;
        this.spotLight.penumbra = 0.35;
        this.spotLight.angle = Math.atan(poolR / h);
        this.spotLight.map = this.spotMapTex;
        this.spotLight.shadow.mapSize.set(1024, 1024);
      } else {
        this.spotLight.color.setHex(0xfff1c9);
        this.spotLight.distance = 40;
        this.spotLight.decay = 1;
        this.spotLight.penumbra = 0.5;
        this.spotLight.angle = 0.32;
        this.spotLight.map = null;
        this.spotLight.shadow.mapSize.set(2048, 2048);
      }
      if (this.spotLight.shadow.map) {
        this.spotLight.shadow.map.dispose();
        this.spotLight.shadow.map = null;
      }
      this._positionSpotlightOnState();
      this._applyLightingAndDimming();
      this._shadowDirty = true;
      this.requestRender();
    }

    setReducedMotion(enabled) {
      this.reducedMotion = Boolean(enabled);
      if (this.reducedMotion) {
        this.cancelIntroAndTour();
      }
      this.requestRender();
    }

    setGridVisible(visible) {
      this.gridVisible = Boolean(visible);
      this.gridMesh.visible = this.gridVisible;
      this.requestRender();
    }

    setLayout(layout, { runEstablishingShot = false } = {}) {
      this.layout = layout;
      this._clearWorldGroup();
      this._buildWorldGeometry(layout);
      this._computeOverviewCamera(layout);

      if (runEstablishingShot && !this.reducedMotion) {
        this.towerRiseProgress = 0.04;
        this._updateBlockMatrices();
        // Start 1.75x out, slightly swung (0.36 rad), land into overview over 2.4 s
        const offset = this.overviewPos.clone().sub(this.overviewTarget);
        const startOff = offset.clone().multiplyScalar(1.75);
        startOff.applyAxisAngle(new this.THREE.Vector3(0, 1, 0), 0.36);
        startOff.y *= 1.18;
        this.camPos.copy(this.overviewTarget).add(startOff);
        this.camTarget.copy(this.overviewTarget);
        this.camera.position.copy(this.camPos);
        this.camera.lookAt(this.camTarget);
        this.establishingShot = {
          startTime: performance.now(),
          durationMs: 2400,
          fromPos: this.camPos.clone(),
          toPos: this.overviewPos.clone(),
        };
      } else {
        this.towerRiseProgress = 1;
        this._updateBlockMatrices();
        if (!this.flight) {
          this.camPos.copy(this.overviewPos);
          this.camTarget.copy(this.overviewTarget);
          this.camera.position.copy(this.camPos);
          this.camera.lookAt(this.camTarget);
        }
      }

      this._shadowDirty = true;
      this.requestRender();
    }

    _computeOverviewCamera(layout) {
      // Low, forward-looking FSN camera (~20° below horizon, FOV 60°) framing root in near foreground
      // and branching group/stage pedestals receding toward the horizon.
      const b = layout.bounds;
      const halfSpanX = Math.max(10, b.spanX * 0.54);
      const fovRad = (this.camera.fov * Math.PI) / 180;
      const hFov = 2 * Math.atan(Math.tan(fovRad / 2) * this.camera.aspect);
      const distForWidth = halfSpanX / Math.max(0.45, Math.tan(hFov / 2));
      const dist = clamp(Math.max(distForWidth * 0.88, b.spanZ * 0.72, 22), 20, 88);
      const pitchRad = (20 * Math.PI) / 180; // ~20° below horizon (FSN initialTilt -70)
      const targetZ = Math.min(-10, b.centerZ + b.spanZ * 0.14);
      this.overviewTarget.set(b.centerX, 1.3, targetZ);
      this.overviewPos.set(
        b.centerX,
        Math.max(7.2, 1.3 + Math.sin(pitchRad) * dist),
        targetZ + Math.cos(pitchRad) * dist + 4.5
      );
    }

    _clearWorldGroup() {
      const disposeObj = (obj) => {
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
          if (Array.isArray(obj.material)) obj.material.forEach((m) => m.dispose());
          else obj.material.dispose();
        }
      };
      while (this.worldGroup.children.length) {
        const child = this.worldGroup.children.pop();
        child.traverse(disposeObj);
      }
      while (this.labelGroup.children.length) {
        const child = this.labelGroup.children.pop();
        if (child.geometry) child.geometry.dispose();
        if (child.material) child.material.dispose();
      }
      this.pickables = [];
      this.pedestalVisuals.clear();
      this.wireVisuals = [];
      this.instancedRefs = {};
      this.labelPool = [];
    }

    _buildWorldGeometry(layout) {
      const THREE = this.THREE;
      const t = THEMES[this.themeKey];

      // 1. Directory Pedestals (Root -> Groups -> Stages) + far flat carpets for LOD
      const pedTopMat = new THREE.MeshStandardMaterial({
        color: t.pedestalTop,
        roughness: 0.74,
        metalness: 0.18,
      });
      const pedSelectedMat = new THREE.MeshStandardMaterial({
        color: t.pedestalSelected,
        roughness: 0.56,
        metalness: 0.15,
        emissive: new THREE.Color(t.pedestalSelected),
        emissiveIntensity: 0.12,
      });
      const pedCarpetMat = new THREE.MeshBasicMaterial({ color: t.pedestalTop });
      const edgeMat = new THREE.LineBasicMaterial({ color: t.pedestalEdge, transparent: true, opacity: 0.65 });
      const edgeSelectedMat = new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.95 });
      this._sharedMats = { pedTopMat, pedSelectedMat, pedCarpetMat, edgeMat, edgeSelectedMat };

      for (const ped of layout.pedestals) {
        const group = new THREE.Group();
        group.position.set(ped.x, 0, ped.z);

        // Raised solid pedestal slab with subtle lower plinth
        const slabGeo = new THREE.BoxGeometry(ped.width, ped.height, ped.depth);
        slabGeo.translate(0, ped.height / 2, 0);
        const slab = new THREE.Mesh(slabGeo, pedTopMat);
        slab.castShadow = false;
        slab.receiveShadow = true;
        slab.userData = { nodeId: ped.id, nodeType: ped.nodeType };
        group.add(slab);
        this.pickables.push(slab);

        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(slabGeo), edgeMat);
        group.add(edges);

        // Flat LOD carpet (visible when camera is far away)
        const carpetGeo = new THREE.PlaneGeometry(ped.width, ped.depth);
        carpetGeo.rotateX(-Math.PI / 2);
        const carpet = new THREE.Mesh(carpetGeo, pedCarpetMat);
        carpet.position.y = 0.03;
        carpet.visible = false;
        group.add(carpet);

        this.worldGroup.add(group);
        this.pedestalVisuals.set(ped.id, { group, slab, edges, carpet, ped });
      }

      // 2. Parent-Child Ground Wires (core ribbon + upright fin + subtle additive glow ribbon)
      for (const wire of layout.wires) {
        const coreMat = new THREE.MeshStandardMaterial({
          color: t.wireBase,
          emissive: new THREE.Color(t.wireBase),
          emissiveIntensity: 1.15,
          roughness: 0.35,
          metalness: 0.1,
        });
        const core = createWireRibbonMesh(THREE, wire.points, 0.14, 0.05, coreMat);
        const fin = createWireRibbonMesh(THREE, wire.points, 0.035, 0.14, coreMat);
        const glowMat = new THREE.MeshBasicMaterial({
          color: t.wireBase,
          transparent: true,
          opacity: t.wireGlowAlpha,
          depthWrite: false,
          blending: t.additiveBeam ? THREE.AdditiveBlending : THREE.NormalBlending,
        });
        const glow = createWireRibbonMesh(THREE, wire.points, 0.52, 0.015, glowMat);
        if (core) {
          core.userData = { wireParentId: wire.parentId, wireChildId: wire.childId, isWire: true };
          this.worldGroup.add(core);
          this.pickables.push(core);
        }
        if (fin) {
          fin.position.y = 0.03;
          this.worldGroup.add(fin);
        }
        if (glow) {
          glow.position.y = 0.018;
          this.worldGroup.add(glow);
        }
        this.wireVisuals.push({ wire, coreMat, glowMat, core, fin, glow });
      }

      // 3. Instanced Upright File Blocks (bounded draw calls independent of item count!)
      const passBlocks = [];
      const failBlocks = [];
      const skipBlocks = [];
      for (const b of layout.blocks) {
        if (b.kind === 'fail') failBlocks.push(b);
        else if (b.kind === 'skip') skipBlocks.push(b);
        else passBlocks.push(b);
      }

      const bw = global.RoomViewLayout.BLOCK_WIDTH;
      const solidGeo = new THREE.BoxGeometry(bw, 1.0, bw);
      solidGeo.translate(0, 0.5, 0);

      const passMat = new THREE.MeshStandardMaterial({
        color: 0xffffff,
        roughness: 0.48,
        metalness: 0.12,
        emissive: new THREE.Color(t.pass),
        emissiveIntensity: 0.05,
      });
      const failMat = new THREE.MeshStandardMaterial({
        color: 0xffffff,
        roughness: 0.44,
        metalness: 0.12,
        emissive: new THREE.Color(t.fail),
        emissiveIntensity: 0.06,
      });
      const failCrownMat = new THREE.MeshStandardMaterial({
        color: t.failCrown,
        roughness: 0.35,
        metalness: 0.08,
        emissive: new THREE.Color(t.failCrown),
        emissiveIntensity: 0.14,
      });
      const skipMat = new THREE.MeshStandardMaterial({
        color: 0xffffff,
        roughness: 0.52,
        metalness: 0.16,
        emissive: new THREE.Color(t.skip),
        emissiveIntensity: 0.05,
      });

      const makeInstanced = (geo, mat, list, role) => {
        if (!list.length) return null;
        const mesh = new THREE.InstancedMesh(geo, mat, list.length);
        // Disable origin-based bounding-sphere culling so distant branch instances never disappear on zoom
        mesh.frustumCulled = false;
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        mesh.userData = { instancedRole: role, blockList: list };
        this.worldGroup.add(mesh);
        return mesh;
      };

      const passMesh = makeInstanced(solidGeo, passMat, passBlocks, 'pass');
      if (passMesh) this.pickables.push(passMesh);

      const failMesh = makeInstanced(solidGeo.clone(), failMat, failBlocks, 'fail');
      if (failMesh) this.pickables.push(failMesh);

      const failCrownGeo = createCrossCrownGeometry(THREE, bw);
      const failCrownMesh = makeInstanced(failCrownGeo, failCrownMat, failBlocks, 'fail-crown');
      if (failCrownMesh) failCrownMesh.castShadow = false;

      // Skip: Hollow wireframe-cage (4 corner posts + top/bottom square rings) + invisible picker box
      const skipPostsGeo = createHollowPostsGeometry(THREE, bw);
      const skipPostsMesh = makeInstanced(skipPostsGeo, skipMat, skipBlocks, 'skip');
      const skipRingGeo = createHollowRingGeometry(THREE, bw);
      const skipTopRingMesh = makeInstanced(skipRingGeo, skipMat, skipBlocks, 'skip-top');
      const skipBotRingMesh = makeInstanced(skipRingGeo.clone(), skipMat, skipBlocks, 'skip-bot');
      // Subtle translucent inner core so skip items remain easily clickable while unmistakably hollow
      const skipHitMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.0, depthWrite: false });
      const skipHitMesh = makeInstanced(solidGeo.clone(), skipHitMat, skipBlocks, 'skip-hit');
      if (skipHitMesh) {
        skipHitMesh.castShadow = false;
        skipHitMesh.receiveShadow = false;
        this.pickables.push(skipHitMesh);
      }

      // Distance LOD proxies for far blocks (flat plane & thin line)
      const lodPlaneGeo = new THREE.PlaneGeometry(bw * 0.9, 1.0);
      lodPlaneGeo.translate(0, 0.5, 0);
      const lodPlaneMat = new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide });
      const lodPlaneMesh = layout.blocks.length
        ? new THREE.InstancedMesh(lodPlaneGeo, lodPlaneMat, layout.blocks.length)
        : null;
      if (lodPlaneMesh) {
        lodPlaneMesh.frustumCulled = false;
        lodPlaneMesh.castShadow = false;
        lodPlaneMesh.receiveShadow = false;
        this.worldGroup.add(lodPlaneMesh);
      }

      // 4. SpaceTree Summary / "+N more" Stacked Outcome Blocks
      const summaryMeshes = [];
      for (const sNode of layout.summaryBlocks) {
        const sGroup = new THREE.Group();
        sGroup.position.set(sNode.x, sNode.y, sNode.z);
        const total = Math.max(1, sNode.counts.total);
        const segs = [
          { count: sNode.counts.pass, color: t.pass },
          { count: sNode.counts.skip, color: t.skip },
          { count: sNode.counts.fail, color: t.fail },
        ];
        let yCursor = 0;
        for (const seg of segs) {
          if (seg.count <= 0) continue;
          const sh = Math.max(0.14, (seg.count / total) * sNode.height);
          const geo = new THREE.BoxGeometry(sNode.width, sh, sNode.depth);
          geo.translate(0, yCursor + sh / 2, 0);
          const mat = new THREE.MeshStandardMaterial({
            color: seg.color,
            roughness: 0.45,
            metalness: 0.14,
            emissive: new THREE.Color(seg.color),
            emissiveIntensity: 0.08,
          });
          const mesh = new THREE.Mesh(geo, mat);
          mesh.castShadow = true;
          mesh.receiveShadow = true;
          mesh.userData = { nodeId: sNode.id, nodeType: sNode.nodeType };
          sGroup.add(mesh);
          this.pickables.push(mesh);
          yCursor += sh;
        }
        // Crisp outline cap indicating expandable SpaceTree summary
        const frameGeo = new THREE.BoxGeometry(sNode.width + 0.06, Math.max(0.2, yCursor) + 0.04, sNode.depth + 0.06);
        frameGeo.translate(0, Math.max(0.2, yCursor) / 2, 0);
        const frame = new THREE.LineSegments(
          new THREE.EdgesGeometry(frameGeo),
          new THREE.LineBasicMaterial({ color: t.wireBase, transparent: true, opacity: 0.85 })
        );
        sGroup.add(frame);
        this.worldGroup.add(sGroup);
        summaryMeshes.push({ node: sNode, group: sGroup });
      }

      this.instancedRefs = {
        passBlocks,
        failBlocks,
        skipBlocks,
        passMesh,
        failMesh,
        failCrownMesh,
        skipPostsMesh,
        skipTopRingMesh,
        skipBotRingMesh,
        skipHitMesh,
        lodPlaneMesh,
        passMat,
        failMat,
        failCrownMat,
        skipMat,
        summaryMeshes,
      };

      this._updateBlockMatrices();
      this._updateColorsAndSelection();
      this._rebuildLabelPool(layout);
    }

    _updateBlockMatrices() {
      if (!this.layout || !this.instancedRefs) return;
      const THREE = this.THREE;
      const refs = this.instancedRefs;
      const dummy = new THREE.Object3D();
      const zeroScale = new THREE.Matrix4().makeScale(0, 0, 0);
      const camPos = this.camera.position;

      const updateList = (list, primaryMesh, onExtra) => {
        if (!list || !list.length || !primaryMesh) return;
        for (let i = 0; i < list.length; i += 1) {
          const b = list[i];
          // Staggered tower rise during establishing shot
          let rise = 1.0;
          if (this.towerRiseProgress < 1.0) {
            const delay = clamp((Math.abs(b.x) * 0.008 + Math.abs(b.z) * 0.009), 0, 0.55);
            rise = smootherstep((this.towerRiseProgress - delay) / Math.max(0.01, 1 - delay));
            rise = Math.max(0.04, rise);
          }

          // Distance LOD with ~15% hysteresis (blocks -> flat planes beyond ~44 units)
          const dist = Math.hypot(camPos.x - b.x, camPos.y - (b.y + b.height * 0.5), camPos.z - b.z);
          const prevLod = this._blockLodState.get(b.id) || 0;
          let lod = prevLod;
          if (prevLod === 0 && dist > 46) lod = 1;
          else if (prevLod === 1 && dist < 39.5) lod = 0;
          this._blockLodState.set(b.id, lod);

          const h = b.height * rise;
          if (lod === 0) {
            dummy.position.set(b.x, b.y, b.z);
            dummy.rotation.set(0, 0, 0);
            dummy.scale.set(1, h, 1);
            dummy.updateMatrix();
            primaryMesh.setMatrixAt(i, dummy.matrix);
          } else {
            primaryMesh.setMatrixAt(i, zeroScale);
          }
          if (onExtra) onExtra(i, b, h, lod, dummy, zeroScale);
        }
        primaryMesh.instanceMatrix.needsUpdate = true;
      };

      updateList(refs.passBlocks, refs.passMesh);
      updateList(refs.failBlocks, refs.failMesh, (i, b, h, lod, dummy, zeroScale) => {
        if (!refs.failCrownMesh) return;
        if (lod === 0) {
          dummy.position.set(b.x, b.y + h, b.z);
          dummy.rotation.set(0, 0, 0);
          dummy.scale.set(1, 1, 1);
          dummy.updateMatrix();
          refs.failCrownMesh.setMatrixAt(i, dummy.matrix);
        } else {
          refs.failCrownMesh.setMatrixAt(i, zeroScale);
        }
      });
      if (refs.failCrownMesh) refs.failCrownMesh.instanceMatrix.needsUpdate = true;

      updateList(refs.skipBlocks, refs.skipPostsMesh, (i, b, h, lod, dummy, zeroScale) => {
        if (lod === 0) {
          dummy.position.set(b.x, b.y + h - 0.08, b.z);
          dummy.rotation.set(0, 0, 0);
          dummy.scale.set(1, 1, 1);
          dummy.updateMatrix();
          if (refs.skipTopRingMesh) refs.skipTopRingMesh.setMatrixAt(i, dummy.matrix);

          dummy.position.set(b.x, b.y, b.z);
          dummy.updateMatrix();
          if (refs.skipBotRingMesh) refs.skipBotRingMesh.setMatrixAt(i, dummy.matrix);

          dummy.scale.set(1, h, 1);
          dummy.updateMatrix();
          if (refs.skipHitMesh) refs.skipHitMesh.setMatrixAt(i, dummy.matrix);
        } else {
          if (refs.skipTopRingMesh) refs.skipTopRingMesh.setMatrixAt(i, zeroScale);
          if (refs.skipBotRingMesh) refs.skipBotRingMesh.setMatrixAt(i, zeroScale);
          if (refs.skipHitMesh) refs.skipHitMesh.setMatrixAt(i, zeroScale);
        }
      });
      if (refs.skipTopRingMesh) refs.skipTopRingMesh.instanceMatrix.needsUpdate = true;
      if (refs.skipBotRingMesh) refs.skipBotRingMesh.instanceMatrix.needsUpdate = true;
      if (refs.skipHitMesh) refs.skipHitMesh.instanceMatrix.needsUpdate = true;

      // Far LOD plane proxy instances
      if (refs.lodPlaneMesh) {
        const all = this.layout.blocks;
        const t = THEMES[this.themeKey];
        const col = new THREE.Color();
        for (let i = 0; i < all.length; i += 1) {
          const b = all[i];
          const lod = this._blockLodState.get(b.id) || 0;
          if (lod === 1) {
            const dist = Math.hypot(camPos.x - b.x, camPos.z - b.z);
            const widthScale = dist > 72 ? 0.22 : 1.0; // Beyond 72 units -> thin vertical line (FSN LOD)
            dummy.position.set(b.x, b.y, b.z);
            dummy.rotation.set(0, 0, 0);
            dummy.scale.set(widthScale, b.height, 1);
            dummy.updateMatrix();
            refs.lodPlaneMesh.setMatrixAt(i, dummy.matrix);
            col.setHex(b.kind === 'fail' ? t.fail : (b.kind === 'skip' ? t.skip : t.pass)).multiplyScalar(0.65);
            refs.lodPlaneMesh.setColorAt(i, col);
          } else {
            refs.lodPlaneMesh.setMatrixAt(i, zeroScale);
          }
        }
        refs.lodPlaneMesh.instanceMatrix.needsUpdate = true;
        if (refs.lodPlaneMesh.instanceColor) refs.lodPlaneMesh.instanceColor.needsUpdate = true;
      }

      // Pedestal LOD with 15% hysteresis (far pedestals become flat "carpets")
      for (const [pedId, vis] of this.pedestalVisuals.entries()) {
        const p = vis.ped;
        const dist = Math.hypot(camPos.x - p.x, camPos.z - p.z);
        const prev = this._pedestalLodState.get(pedId) || 0;
        let lod = prev;
        if (prev === 0 && dist > 68 && p.nodeType !== 'root') lod = 1;
        else if (prev === 1 && dist < 58) lod = 0;
        this._pedestalLodState.set(pedId, lod);
        vis.slab.visible = lod === 0;
        vis.edges.visible = lod === 0;
        vis.carpet.visible = lod === 1;
      }
    }

    _applyThemeToWorldMaterials() {
      const t = THEMES[this.themeKey];
      if (this._sharedMats) {
        this._sharedMats.pedTopMat.color.setHex(t.pedestalTop);
        this._sharedMats.pedSelectedMat.color.setHex(t.pedestalSelected);
        this._sharedMats.pedSelectedMat.emissive.setHex(t.pedestalSelected);
        this._sharedMats.pedCarpetMat.color.setHex(t.pedestalTop);
        this._sharedMats.edgeMat.color.setHex(t.pedestalEdge);
      }
      if (this.instancedRefs) {
        if (this.instancedRefs.passMat) this.instancedRefs.passMat.emissive.setHex(t.pass);
        if (this.instancedRefs.failMat) this.instancedRefs.failMat.emissive.setHex(t.fail);
        if (this.instancedRefs.failCrownMat) {
          this.instancedRefs.failCrownMat.color.setHex(t.failCrown);
          this.instancedRefs.failCrownMat.emissive.setHex(t.failCrown);
        }
        if (this.instancedRefs.skipMat) this.instancedRefs.skipMat.emissive.setHex(t.skip);
      }
      this._updateColorsAndSelection();
      if (this.layout) this._rebuildLabelPool(this.layout);
    }

    _ancestorChainSet(nodeId) {
      const set = new Set();
      if (!this.layout || !nodeId) return set;
      let cur = this.layout.nodeById.get(nodeId);
      while (cur) {
        set.add(cur.id);
        cur = cur.parentId ? this.layout.nodeById.get(cur.parentId) : null;
      }
      return set;
    }

    _updateColorsAndSelection() {
      if (!this.layout || !this.instancedRefs) return;
      const THREE = this.THREE;
      const t = THEMES[this.themeKey];
      const refs = this.instancedRefs;
      const selectedId = this.selectedNodeId;
      const hoveredId = this.hoveredNodeId;
      const activeChain = this._ancestorChainSet(selectedId || this.focusedPedestalId);
      const hasItemSpot = this.spotState.targetActive;

      // FSN restraint (Research 1/5 & 5/5): unselected blocks sit ~0.62x value; selected stands at 1.0!
      const colorTmp = new THREE.Color();
      const paintList = (list, mesh, baseHex) => {
        if (!list || !list.length || !mesh) return;
        for (let i = 0; i < list.length; i += 1) {
          const b = list[i];
          const isSel = b.id === selectedId;
          const isHov = b.id === hoveredId;
          colorTmp.setHex(baseHex);
          if (isSel) {
            colorTmp.multiplyScalar(1.08);
          } else if (isHov) {
            colorTmp.multiplyScalar(0.92);
          } else {
            colorTmp.multiplyScalar(hasItemSpot ? 0.56 : 0.68);
          }
          mesh.setColorAt(i, colorTmp);
        }
        if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
      };

      paintList(refs.passBlocks, refs.passMesh, t.pass);
      paintList(refs.failBlocks, refs.failMesh, t.fail);
      paintList(refs.skipBlocks, refs.skipPostsMesh, t.skip);
      paintList(refs.skipBlocks, refs.skipTopRingMesh, t.skip);
      paintList(refs.skipBlocks, refs.skipBotRingMesh, t.skip);

      // Pedestal highlight along active branch
      for (const [pedId, vis] of this.pedestalVisuals.entries()) {
        const isActive = activeChain.has(pedId) || pedId === hoveredId;
        vis.slab.material = isActive && pedId !== 'root' ? this._sharedMats.pedSelectedMat : this._sharedMats.pedTopMat;
        vis.edges.material = isActive ? this._sharedMats.edgeSelectedMat : this._sharedMats.edgeMat;
      }

      this._applyLightingAndDimming();
    }

    _rebuildLabelPool(layout) {
      const THREE = this.THREE;
      while (this.labelGroup.children.length) {
        const c = this.labelGroup.children.pop();
        if (c.geometry) c.geometry.dispose();
        if (c.material) c.material.dispose();
      }
      this.labelPool = [];

      const addLabelEntry = (node, title, subtitle, priority, worldW, yPos, zOffset, xOffset = 0) => {
        const texEntry = this.labelAtlas.getTexture(title, subtitle, this.themeKey, false);
        const h = worldW / texEntry.aspect;
        const geo = new THREE.PlaneGeometry(worldW, h);
        const mat = new THREE.MeshBasicMaterial({
          map: texEntry.tex,
          transparent: true,
          depthWrite: false,
          side: THREE.FrontSide,
        });
        const mesh = new THREE.Mesh(geo, mat);
        // Floor-anchored, tilted 18° toward camera, raised cleanly above ground wires
        mesh.position.set(node.x + xOffset, yPos, node.z + zOffset);
        mesh.rotation.x = (-68 * Math.PI) / 180;
        mesh.renderOrder = 8;
        mesh.visible = false;
        this.labelGroup.add(mesh);
        this.labelPool.push({
          nodeId: node.id,
          nodeType: node.nodeType,
          title,
          subtitle,
          priority,
          mesh,
          worldPos: mesh.position.clone(),
          halfW: worldW * 0.5,
          halfH: h * 0.5,
        });
      };

      for (const ped of layout.pedestals) {
        const sub = ped.nodeType === 'root'
          ? `${ped.counts.total} items · ${ped.counts.fail} fail`
          : (ped.collapsed
            ? `${ped.counts.total} items (click to expand)`
            : `${ped.counts.total} items${ped.counts.fail ? ` · ${ped.counts.fail} fail` : ''}`);
        const w = ped.nodeType === 'root' ? 4.4 : (ped.nodeType === 'group' ? 3.8 : 2.7);
        const prio = ped.nodeType === 'root' ? 100 : (ped.nodeType === 'group' ? 80 : 65);
        // Place cleanly at front apron above ground wire height so wires pass underneath without slicing text
        addLabelEntry(ped, ped.label, sub, prio, w, Math.max(0.32, ped.height * 0.55), ped.depth / 2 + 0.72);
      }

      for (const sNode of layout.summaryBlocks) {
        addLabelEntry(sNode, sNode.label, `${sNode.counts.fail} fail`, 58, 1.55, sNode.y + sNode.height + 0.35, 0.0);
      }

      for (const b of layout.blocks) {
        const mText = b.item.metric != null ? `${b.item.metric}` : '';
        addLabelEntry(b, b.label, mText, 24, 1.28, b.y + b.height + 0.38, 0.0);
      }
    }

    _updateFloorLabels() {
      if (!this.labelPool.length) return;
      const THREE = this.THREE;
      const cam = this.camera;
      const w = window.innerWidth;
      const h = Math.max(1, window.innerHeight);
      const proj = new THREE.Vector3();

      // Distance LOD (FSN rules): items within ~9 units, directories within ~42 units;
      // hovered or selected always shown; budget nearest ~30 non-overlapping labels.
      const candidates = [];
      for (const item of this.labelPool) {
        item.mesh.visible = false;
        const isForce = item.nodeId === this.selectedNodeId || item.nodeId === this.hoveredNodeId;
        const dist = cam.position.distanceTo(item.worldPos);
        if (!isForce) {
          if (item.nodeType === 'item' && dist > 8.2) continue;
          if (item.nodeType === 'stage' && dist > 54) continue;
          if (item.nodeType === 'group' && dist > 95) continue;
          if (item.nodeType === 'stage-summary' && dist > 26) continue;
          if (item.nodeType === 'more' && dist > 18) continue;
        }
        proj.copy(item.worldPos).project(cam);
        if (proj.z < -1 || proj.z > 1 || Math.abs(proj.x) > 1.05 || Math.abs(proj.y) > 1.05) continue;
        const sx = (proj.x * 0.5 + 0.5) * w;
        const sy = (-proj.y * 0.5 + 0.5) * h;
        const estW = clamp(120 * (18 / Math.max(4, dist)), 64, 180);
        const estH = clamp(36 * (18 / Math.max(4, dist)), 22, 52);
        const score = (isForce ? 10000 : item.priority * 10) - dist;
        candidates.push({ item, isForce, sx, sy, estW, estH, score });
      }

      candidates.sort((a, b) => b.score - a.score);
      const accepted = [];
      const MAX_LABELS = 30;
      let nonSelectedItemLabels = 0;
      for (const cand of candidates) {
        if (!cand.isForce && accepted.length >= MAX_LABELS) continue;
        if (!cand.isForce && cand.item.nodeType === 'item') {
          if (this.spotState.targetActive || nonSelectedItemLabels >= 4) continue;
        }
        const r1 = {
          x0: cand.sx - cand.estW * 0.48,
          x1: cand.sx + cand.estW * 0.48,
          y0: cand.sy - cand.estH * 0.48,
          y1: cand.sy + cand.estH * 0.48,
        };
        let collides = false;
        if (!cand.isForce) {
          for (const prev of accepted) {
            if (r1.x0 < prev.x1 && r1.x1 > prev.x0 && r1.y0 < prev.y1 && r1.y1 > prev.y0) {
              collides = true;
              break;
            }
          }
        }
        if (!collides) {
          cand.item.mesh.visible = true;
          if (!cand.isForce && cand.item.nodeType === 'item') nonSelectedItemLabels += 1;
          const texEntry = this.labelAtlas.getTexture(
            cand.item.title,
            cand.item.subtitle,
            this.themeKey,
            cand.isForce
          );
          if (cand.item.mesh.material.map !== texEntry.tex) {
            cand.item.mesh.material.map = texEntry.tex;
            cand.item.mesh.material.needsUpdate = true;
          }
          accepted.push(r1);
        }
      }
    }

    _positionSpotlightOnState() {
      const s = this.spotState;
      const isV2 = this.spotlightMode === 'v2';
      const h = isV2 ? 14.0 : 12.0;
      const poolR = Math.max(2.6, 1.35 * s.blockHalfDiag);

      this.spotLight.position.set(s.targetX, s.floorY + h, s.targetZ);
      this.spotLight.target.position.set(s.targetX, s.floorY, s.targetZ);
      this.spotLight.target.updateMatrixWorld();

      if (isV2) {
        const angle = Math.atan(poolR / h);
        this.spotLight.angle = angle;
        this.spotLight.shadow.camera.near = Math.max(1.0, h - 4.5);
        this.spotLight.shadow.camera.far = h + 2.5;
        this.spotLight.shadow.camera.updateProjectionMatrix();
        this.beamV2Mesh.position.set(s.targetX, s.floorY + h, s.targetZ);
        this.beamV2Uniforms.uFloorY.value = s.floorY;
        this.beamV2Uniforms.uBlockTop.value = s.blockTop;
      } else {
        this.spotLight.shadow.camera.near = 1.0;
        this.spotLight.shadow.camera.far = 26.0;
        this.spotLight.shadow.camera.updateProjectionMatrix();
        this.beamBaseMesh.position.set(s.targetX, s.floorY + 12.0, s.targetZ);
        this.poolBaseMesh.position.set(s.targetX, s.floorY + 0.025, s.targetZ);
      }
      this._shadowDirty = true;
    }

    _applyLightingAndDimming() {
      const t = THEMES[this.themeKey];
      const dimL = this.spotState.dimL;
      const spotL = this.spotState.spotL;
      const isV2 = this.spotlightMode === 'v2';
      const activeChain = this._ancestorChainSet(this.selectedNodeId || this.focusedPedestalId);

      // World dimming over 450 ms (Research 3/5 & 5/5 #11)
      if (isV2) {
        this.hemiLight.intensity = t.hemiIntensity + (t.hemiDim - t.hemiIntensity) * dimL;
        this.keyLight.intensity = t.keyIntensity + (t.keyDim - t.keyIntensity) * dimL;
        this.headlight.intensity = t.headlightIntensity + (t.headlightDim - t.headlightIntensity) * dimL;
        this.gridUniforms.uAlpha.value = t.gridAlpha * (1 - 0.5 * dimL);
        this.scene.fog.density = t.fogDensity * (1 + 0.3 * dimL);
      } else {
        this.hemiLight.intensity = t.hemiIntensity * (1 - 0.75 * dimL);
        this.keyLight.intensity = t.keyIntensity * (1 - 0.80 * dimL);
        this.headlight.intensity = t.headlightIntensity * (1 - 0.85 * dimL);
        this.gridUniforms.uAlpha.value = t.gridAlpha * (1 - 0.45 * dimL);
        this.scene.fog.density = t.fogDensity * (1 + 0.2 * dimL);
      }

      // Wires: dim unselected x0.45, keep active ancestor chain bright
      for (const wv of this.wireVisuals) {
        const isActiveWire = activeChain.has(wv.wire.childId);
        if (isActiveWire) {
          wv.coreMat.color.setHex(t.wireSelected);
          wv.coreMat.emissive.setHex(t.wireSelected);
          wv.coreMat.emissiveIntensity = 1.4;
          wv.glowMat.opacity = t.wireGlowAlpha * 1.4;
        } else {
          wv.coreMat.color.setHex(t.wireBase);
          wv.coreMat.emissive.setHex(t.wireBase);
          wv.coreMat.emissiveIntensity = 1.15 * (1 - 0.55 * dimL);
          wv.glowMat.opacity = t.wireGlowAlpha * (1 - 0.55 * dimL);
        }
      }

      // Spotlight & volumetric beam (never toggle SpotLight.visible / castShadow / map!)
      if (isV2) {
        this.spotLight.intensity = t.spotV2Intensity * spotL;
        this.beamV2Uniforms.uFade.value = spotL;
        this.beamV2Mesh.visible = spotL > 0.001;
        this.beamBaseMesh.visible = false;
        this.poolBaseMesh.visible = false;
      } else {
        this.spotLight.intensity = 300.0 * spotL;
        this.beamV2Mesh.visible = false;
        this.beamBaseMesh.visible = spotL > 0.001;
        this.poolBaseMesh.visible = spotL > 0.001;
        this.beamBaseMat.opacity = t.beamBaseOpacity * spotL;
        this.poolBaseMat.opacity = t.poolBaseOpacity * spotL;
      }
    }

    _startSpotlightOnBlock(blockNode) {
      const clickStart = performance.now();
      const s = this.spotState;
      const moved = s.targetX !== blockNode.x || s.targetZ !== blockNode.z || !s.targetActive;
      s.targetX = blockNode.x;
      s.targetZ = blockNode.z;
      s.floorY = blockNode.y;
      s.blockTop = blockNode.y + blockNode.height;
      s.blockHalfDiag = Math.hypot(blockNode.width * 0.5, blockNode.depth * 0.5);
      s.fromDimL = s.dimL;
      s.fromSpotL = s.spotL;
      s.targetActive = true;
      s.active = true;
      s.startTime = performance.now();
      if (moved) {
        this._positionSpotlightOnState();
      }
      this.requestRender();
      this.stats.lastFirstClickMs = Math.round((performance.now() - clickStart) * 10) / 10;
    }

    clearSpotlight() {
      const s = this.spotState;
      if (!s.targetActive && s.dimL === 0 && s.spotL === 0) return;
      s.fromDimL = s.dimL;
      s.fromSpotL = s.spotL;
      s.targetActive = false;
      s.active = true;
      s.startTime = performance.now();
      this.requestRender();
    }

    _flyTo(destPos, destTarget, { instant = false } = {}) {
      this.cancelIntroAndTour();
      destPos.y = Math.max(0.75, destPos.y);
      if (instant || this.reducedMotion) {
        // WCAG 2.3.3: instant cut (or <=150ms short crossfade cut) when reduced motion active
        this.flight = null;
        this.camPos.copy(destPos);
        this.camTarget.copy(destTarget);
        this.camera.position.copy(this.camPos);
        this.camera.lookAt(this.camTarget);
        this.requestRender();
        return;
      }
      const dist = this.camPos.distanceTo(destPos);
      const horizTravel = Math.hypot(destTarget.x - this.camTarget.x, destTarget.z - this.camTarget.z);
      const curViewDist = this.camPos.distanceTo(this.camTarget);
      // Duration: clamp(0.6 + 0.35 * sqrt(distance), 0.8, 2.6) s (Research 5/5 #6)
      const durationMs = clamp(0.6 + 0.35 * Math.sqrt(dist), 0.8, 2.6) * 1000;

      // fsv swing-back / lifted Bézier midpoint for long hops (> 3x view distance)
      const midPos = this.camPos.clone().lerp(destPos, 0.5);
      if (horizTravel > curViewDist * 3.0) {
        const apogee = clamp(1.2 * Math.max(curViewDist, horizTravel * 0.42), 14, 48);
        midPos.y = Math.max(midPos.y, apogee);
      } else {
        midPos.y = Math.max(midPos.y, Math.max(this.camPos.y, destPos.y) + Math.min(4.5, dist * 0.14));
      }

      this.flight = {
        startTime: performance.now(),
        durationMs,
        fromPos: this.camPos.clone(),
        midPos,
        toPos: destPos.clone(),
        fromTarget: this.camTarget.clone(),
        toTarget: destTarget.clone(),
      };
      this.requestRender();
    }

    frameOverview({ instant = false } = {}) {
      this.selectedNodeId = null;
      this.focusedPedestalId = 'root';
      this.clearSpotlight();
      this._updateColorsAndSelection();
      this._flyTo(this.overviewPos.clone(), this.overviewTarget.clone(), { instant });
    }

    focusNode(nodeId, { moveCamera = true, instant = false } = {}) {
      if (!this.layout) return null;
      const node = this.layout.nodeById.get(nodeId);
      if (!node) return null;
      const THREE = this.THREE;

      if (node.nodeType === 'item') {
        this.selectedNodeId = node.id;
        this.focusedPedestalId = node.parentId;
        this._startSpotlightOnBlock(node);
        this._updateColorsAndSelection();
        if (moveCamera) {
          // Arrive ~45° above item (Research 5/5 #5), keeping spotlight beam & pool framed
          const elev = (40 * Math.PI) / 180;
          const dist = Math.max(6.2, node.height * 2.2 + 3.6);
          const target = new THREE.Vector3(node.x, node.y + Math.min(1.6, node.height * 0.48), node.z);
          const pos = new THREE.Vector3(
            node.x + 1.8,
            node.y + Math.sin(elev) * dist + 0.6,
            node.z + Math.cos(elev) * dist
          );
          this._flyTo(pos, target, { instant });
        }
      } else if (node.nodeType === 'root') {
        this.frameOverview({ instant });
      } else if (node.nodeType === 'group' || node.nodeType === 'stage') {
        this.selectedNodeId = node.id;
        this.focusedPedestalId = node.id;
        this.clearSpotlight();
        this._updateColorsAndSelection();
        if (moveCamera) {
          // Arrive ~30° above directory pedestal (Research 5/5 #5)
          const elev = (28 * Math.PI) / 180;
          const span = Math.max(node.width, node.depth);
          const dist = node.nodeType === 'group' ? Math.max(15.5, span * 2.4) : Math.max(8.8, span * 1.95);
          const lookZ = node.nodeType === 'group' ? node.z - 6.5 : node.z;
          const target = new THREE.Vector3(node.x, node.height + 0.6, lookZ);
          const pos = new THREE.Vector3(
            node.x,
            node.height + Math.sin(elev) * dist + 0.8,
            node.z + Math.cos(elev) * dist
          );
          this._flyTo(pos, target, { instant });
        }
      } else if (node.nodeType === 'more' || node.nodeType === 'stage-summary') {
        this.selectedNodeId = node.id;
        this.focusedPedestalId = node.parentId;
        this.clearSpotlight();
        this._updateColorsAndSelection();
        if (moveCamera) {
          const target = new THREE.Vector3(node.x, node.y + 0.8, node.z);
          const pos = new THREE.Vector3(node.x + 1.5, node.y + 4.8, node.z + 6.4);
          this._flyTo(pos, target, { instant });
        }
      }
      return node;
    }

    _pickAtEvent(ev) {
      const rect = this.canvas.getBoundingClientRect();
      this.pointerNdc.x = ((ev.clientX - rect.left) / Math.max(1, rect.width)) * 2 - 1;
      this.pointerNdc.y = -((ev.clientY - rect.top) / Math.max(1, rect.height)) * 2 + 1;
      this.raycaster.setFromCamera(this.pointerNdc, this.camera);

      // Include active spotlight beam as clickable (FSN faithful extra, Research 1/5 & 5/5 #13)
      const targets = this.pickables.slice();
      if (this.beamV2Mesh.visible) targets.push(this.beamV2Mesh);
      if (this.beamBaseMesh.visible) targets.push(this.beamBaseMesh);

      const hits = this.raycaster.intersectObjects(targets, false);
      if (!hits.length) return null;
      const hit = hits[0];
      const ud = hit.object.userData || {};
      if (ud.isSpotBeam) {
        return { kind: 'spot-beam', nodeId: this.selectedNodeId };
      }
      if (ud.instancedRole && ud.blockList && hit.instanceId != null) {
        const b = ud.blockList[hit.instanceId];
        if (b) return { kind: 'node', nodeId: b.id, node: b };
      }
      if (ud.isWire) {
        return { kind: 'wire', parentId: ud.wireParentId, childId: ud.wireChildId };
      }
      if (ud.nodeId && this.layout) {
        return { kind: 'node', nodeId: ud.nodeId, node: this.layout.nodeById.get(ud.nodeId) };
      }
      return null;
    }

    _handleHover(ev) {
      const hit = this._pickAtEvent(ev);
      const nextId = hit && hit.kind === 'node' ? hit.nodeId : null;
      if (nextId !== this.hoveredNodeId) {
        this.hoveredNodeId = nextId;
        this.canvas.style.cursor = hit ? 'pointer' : 'grab';
        this._updateColorsAndSelection();
        this.requestRender();
      }
      if (this.callbacks.onHover) {
        this.callbacks.onHover(hit && hit.node ? hit.node : null, ev.clientX, ev.clientY);
      }
    }

    _handleClick(ev) {
      const hit = this._pickAtEvent(ev);
      if (!hit) return;
      if (hit.kind === 'spot-beam' && this.selectedNodeId) {
        // Clicking the spotlight zooms closer to the spotlit file (FSN convention)
        this.focusNode(this.selectedNodeId, { moveCamera: true });
        return;
      }
      if (hit.kind === 'wire') {
        // Clicking wire selects child directory; second click ascends to parent (FSN convention)
        const targetId = this.selectedNodeId === hit.childId ? hit.parentId : hit.childId;
        if (this.callbacks.onSelectNode) {
          this.callbacks.onSelectNode(targetId, { shiftKey: ev.shiftKey });
        }
        return;
      }
      if (hit.kind === 'node' && this.callbacks.onSelectNode) {
        this.callbacks.onSelectNode(hit.nodeId, { shiftKey: ev.shiftKey });
      }
    }

    captureTopDownDataUrl() {
      // Render a crisp static top-down overview frame for @media print without leaving idle rendering on
      if (!this.layout) return '';
      const THREE = this.THREE;
      const savedPos = this.camera.position.clone();
      const savedQuat = this.camera.quaternion.clone();
      const b = this.layout.bounds;
      const span = Math.max(b.spanX, b.spanZ, 26);
      this.camera.position.set(b.centerX, span * 1.05, b.centerZ + 0.01);
      this.camera.lookAt(new THREE.Vector3(b.centerX, 0, b.centerZ));
      this.skyMesh.position.set(this.camera.position.x, 0, this.camera.position.z);
      this.gridMesh.position.set(this.camera.position.x, 0.012, this.camera.position.z);
      this.gridUniforms.uCamPos.value.copy(this.camera.position);
      this._updateFloorLabels();
      this.renderer.render(this.scene, this.camera);
      const dataUrl = this.canvas.toDataURL('image/png');
      this.camera.position.copy(savedPos);
      this.camera.quaternion.copy(savedQuat);
      this.requestRender();
      return dataUrl;
    }

    requestRender() {
      if (this._disposed) return;
      this._dirty = true;
      if (this._rafId === null) {
        this._rafId = requestAnimationFrame((now) => this._tick(now));
      }
    }

    _tick(now) {
      this._rafId = null;
      if (this._disposed) return;
      let keepGoing = false;

      // 1. Establishing shot + staggered tower rise
      if (this.establishingShot) {
        const tRaw = (now - this.establishingShot.startTime) / this.establishingShot.durationMs;
        const e = smootherstep(tRaw);
        this.camPos.lerpVectors(this.establishingShot.fromPos, this.establishingShot.toPos, e);
        this.camera.position.copy(this.camPos);
        this.camera.lookAt(this.camTarget);
        this.towerRiseProgress = clamp(tRaw * 1.15, 0.04, 1);
        this._updateBlockMatrices();
        if (tRaw >= 1) {
          this.establishingShot = null;
          this.towerRiseProgress = 1;
          this._updateBlockMatrices();
        } else {
          keepGoing = true;
        }
      }

      // 2. Swooping camera flight (Quadratic Bézier + leading look-at, no quaternion slerping)
      if (this.flight) {
        const f = this.flight;
        const tRaw = (now - f.startTime) / f.durationMs;
        const e = sigmoidEase(tRaw);
        const omt = 1 - e;
        this.camPos.set(
          omt * omt * f.fromPos.x + 2 * omt * e * f.midPos.x + e * e * f.toPos.x,
          Math.max(0.75, omt * omt * f.fromPos.y + 2 * omt * e * f.midPos.y + e * e * f.toPos.y),
          omt * omt * f.fromPos.z + 2 * omt * e * f.midPos.z + e * e * f.toPos.z
        );
        const lookE = sigmoidEase(Math.min(1, tRaw * 1.15));
        this.camTarget.lerpVectors(f.fromTarget, f.toTarget, lookE);
        this.camera.position.copy(this.camPos);
        this.camera.lookAt(this.camTarget);
        if (tRaw >= 1) {
          this.flight = null;
        } else {
          keepGoing = true;
        }
      }

      // 3. Spotlight fade & world dimming (450 ms dim; 100 ms delay + 300 ms beam/spot)
      if (this.spotState.active) {
        const s = this.spotState;
        const elapsed = now - s.startTime;
        const dimDuration = 450;
        const dimP = sigmoidEase(elapsed / dimDuration);
        const targetVal = s.targetActive ? 1 : 0;
        s.dimL = s.fromDimL + (targetVal - s.fromDimL) * dimP;

        if (s.targetActive) {
          const spotP = elapsed < 100 ? 0 : sigmoidEase((elapsed - 100) / 300);
          s.spotL = s.fromSpotL + (1 - s.fromSpotL) * spotP;
        } else {
          const spotP = sigmoidEase(elapsed / 260);
          s.spotL = s.fromSpotL + (0 - s.fromSpotL) * spotP;
        }
        this._applyLightingAndDimming();
        if (elapsed >= dimDuration) {
          s.dimL = targetVal;
          s.spotL = targetVal;
          s.active = false;
          this._applyLightingAndDimming();
        } else {
          keepGoing = true;
        }
      }

      // Keep sky & infinite grid centered on camera XZ
      this.skyMesh.position.set(this.camera.position.x, 0, this.camera.position.z);
      this.gridMesh.position.set(this.camera.position.x, 0.012, this.camera.position.z);
      this.gridUniforms.uCamPos.value.copy(this.camera.position);

      // Update LOD & floor labels
      this._updateBlockMatrices();
      this._updateFloorLabels();

      // Shadow map updates ONLY when spotlight moved or layout changed (NOT on intensity fades!)
      if (this._shadowDirty) {
        this.renderer.shadowMap.needsUpdate = true;
        this.stats.shadowUpdateCount += 1;
        this._shadowDirty = false;
      }

      this.renderer.render(this.scene, this.camera);
      this.stats.renderCount += 1;
      this.stats.drawCalls = this.renderer.info.render.calls;
      if (this.stats.firstFrameMs === null) {
        this.stats.firstFrameMs = Math.round((performance.now() - this.stats.bootStartMs) * 10) / 10;
        if (this.callbacks.onFirstFrame) this.callbacks.onFirstFrame();
      }
      this._dirty = false;

      if (keepGoing || this._dirty) {
        this._rafId = requestAnimationFrame((ts) => this._tick(ts));
      }
    }

    settleAnimationsImmediately() {
      if (this.establishingShot) {
        this.camPos.copy(this.establishingShot.toPos);
        this.establishingShot = null;
        this.towerRiseProgress = 1;
      }
      if (this.flight) {
        this.camPos.copy(this.flight.toPos);
        this.camTarget.copy(this.flight.toTarget);
        this.flight = null;
      }
      this.camera.position.copy(this.camPos);
      this.camera.lookAt(this.camTarget);
      if (this.spotState.active) {
        const v = this.spotState.targetActive ? 1 : 0;
        this.spotState.dimL = v;
        this.spotState.spotL = v;
        this.spotState.active = false;
        this._applyLightingAndDimming();
      }
      this._updateBlockMatrices();
      this._tick(performance.now());
    }
  }

  global.RoomViewScene3D = {
    THEMES,
    FsnScene3D,
  };
})(window);
