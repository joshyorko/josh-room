// Full-bleed SGI FSN / Jurassic Park 3D Filesystem Navigator (Three.js r170, core-only).
// Near-ground low forward camera, architectural SGI pedestals & chamfered file monoliths,
// restrained metallic ground conduits, arbitrary-depth branching, shape-based instancing with
// custom legend colors, dual-layer theatrical overhead Spotlight v2 BEAM + floor pool (no lamp
// fixture), Baseline A/B toggle, distance LOD with 15% hysteresis, and render-on-demand.
(function (global) {
  'use strict';

  const THEMES = {
    night: {
      skyZenith: 0x050914,
      skyHorizon: 0x16243b,
      skyBand: 0x243c60,
      fogColor: 0x132034,
      fogDensity: 1.55 / 155,
      groundNear: 0x0e1520,
      groundFar: 0x223548,

      hemiSky: 0x90acd8,
      hemiGround: 0x171f2d,
      hemiIntensity: 1.05,
      hemiDim: 0.16,
      keyColor: 0xc9ddff,
      keyIntensity: 0.86,
      keyDim: 0.11,
      headlightColor: 0xa4c8ff,
      headlightIntensity: 0.32,
      headlightDim: 0.03,
      pedestalPlinth: 0x1e2838,
      pedestalTop: 0x687487,
      pedestalDeck: 0x475369,
      pedestalSelected: 0x4f6482,
      pedestalEdge: 0x6d89ad,
      wireChannel: 0x1b2b3e,
      wireBase: 0x9eaeb6,
      wireSelected: 0xf4e5c2,
      wireGlowAlpha: 0.02,
      failCrown: 0xffd8c8,
      labelBg: 'rgba(8, 14, 26, 0.90)',
      labelBorder: 'rgba(110, 170, 220, 0.62)',
      labelText: '#f0f7ff',
      labelSub: '#8ec4e6',
      spotV2Color: 0xfff1d6,
      spotV2Intensity: 880,
      beamOuterColor: 0xffdf9e,
      beamOuterPeak: 0.44,
      beamCoreColor: 0xfff3d4,
      beamCorePeak: 0.38,
      poolHaloColor: 0xffe6b3,
      poolHaloOpacity: 0.52,
      beamBaseOpacity: 0.36,
      poolBaseOpacity: 0.92,
      additiveBeam: true,
    },
    grass: {
      // SGI Fsn app-defaults Grass landscape palette (verified)
      skyZenith: 0x3985a4,
      skyHorizon: 0xb0d7cd,
      skyBand: 0xc8ded1,
      fogColor: 0xabcac2,
      fogDensity: 1.55 / 190,
      groundNear: 0x244539,
      groundFar: 0x658975,

      hemiSky: 0xdff6ff,
      hemiGround: 0x1e5c2c,
      hemiIntensity: 1.04,
      hemiDim: 0.19,
      keyColor: 0xfff7e2,
      keyIntensity: 0.90,
      keyDim: 0.14,
      headlightColor: 0xdaf4ff,
      headlightIntensity: 0.24,
      headlightDim: 0.03,
      pedestalPlinth: 0x35474a,
      pedestalTop: 0x96ab9e,
      pedestalDeck: 0x748b80,
      pedestalSelected: 0x749094,
      pedestalEdge: 0xb0d6ca,
      wireChannel: 0x1e4e35,
      wireBase: 0xbfcec0,
      wireSelected: 0xffffff,
      wireGlowAlpha: 0.02,
      failCrown: 0xffefe8,
      labelBg: 'rgba(10, 32, 20, 0.90)',
      labelBorder: 'rgba(145, 255, 240, 0.65)',
      labelText: '#f4fff8',
      labelSub: '#a8f0d4',
      spotV2Color: 0xfffee2,
      spotV2Intensity: 780,
      beamOuterColor: 0xfff0ba,
      beamOuterPeak: 0.36,
      beamCoreColor: 0xfffbe0,
      beamCorePeak: 0.28,
      poolHaloColor: 0xfff6cf,
      poolHaloOpacity: 0.44,
      beamBaseOpacity: 0.24,
      poolBaseOpacity: 0.68,
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

  function resolveBlockHex(block, themeKey) {
    const entry = block.legendEntry;
    if (themeKey === 'grass') {
      if (block.kind === 'pass') return 0x007a5a;
      if (block.kind === 'fail') return 0xc2410c;
      if (block.kind === 'skip') return 0x6b7280;
    }
    if (entry && entry.color && /^#[0-9a-fA-F]{6}$/.test(entry.color)) {
      return parseInt(entry.color.slice(1), 16);
    }
    return 0x4cc9f0;
  }

  function createSpotMapTexture(THREE) {
    const size = 256;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    const cx = size / 2;
    const grad = ctx.createRadialGradient(cx, cx, 0, cx, cx, cx);
    grad.addColorStop(0.0, 'rgba(220, 220, 220, 1)');
    grad.addColorStop(0.58, 'rgba(255, 255, 255, 1)');
    grad.addColorStop(0.84, 'rgba(155, 155, 155, 1)');
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

  function createFloorPoolHaloTexture(THREE) {
    const size = 256;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    const cx = size / 2;
    const grad = ctx.createRadialGradient(cx, cx, 0, cx, cx, cx);
    grad.addColorStop(0.0, 'rgba(255, 246, 218, 0.85)');
    grad.addColorStop(0.36, 'rgba(255, 236, 185, 0.56)');
    grad.addColorStop(0.68, 'rgba(255, 224, 155, 0.24)');
    grad.addColorStop(0.88, 'rgba(255, 218, 140, 0.06)');
    grad.addColorStop(1.0, 'rgba(255, 218, 140, 0.0)');
    ctx.clearRect(0, 0, size, size);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, size, size);
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
    grad.addColorStop(0.16, '#dedede');
    grad.addColorStop(0.84, '#ffffff');
    grad.addColorStop(1.0, '#000000');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 64, 256);
    const tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    return tex;
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

  function createArchitecturalBlockGeometry(THREE, width) {
    // SGI FSN architectural file monolith: slight stepped base collar + main vertical body + beveled crown
    const base = new THREE.BoxGeometry(width * 1.04, 0.08, width * 1.04);
    base.translate(0, 0.04, 0);
    const body = new THREE.BoxGeometry(width, 0.84, width);
    body.translate(0, 0.50, 0);
    const crown = new THREE.BoxGeometry(width * 0.86, 0.08, width * 0.86);
    crown.translate(0, 0.96, 0);
    return mergeSimpleGeometries(THREE, [base, body, crown]);
  }

  function createNotchCrownGeometry(THREE, width) {
    // Sleek architectural X-chevron cap for WCAG 1.4.1 non-color Failure cue
    const barLen = width * 0.82;
    const barThick = width * 0.14;
    const barHeight = 0.11;
    const g1 = new THREE.BoxGeometry(barLen, barHeight, barThick);
    g1.rotateY(Math.PI / 4);
    g1.translate(0, barHeight / 2, 0);
    const g2 = new THREE.BoxGeometry(barLen, barHeight, barThick);
    g2.rotateY(-Math.PI / 4);
    g2.translate(0, barHeight / 2, 0);
    return mergeSimpleGeometries(THREE, [g1, g2]);
  }

  function createHollowPostsGeometry(THREE, width) {
    const t = width * 0.12;
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
    const t = width * 0.12;
    const off = (width - t) / 2;
    const h = 0.07;
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

  function createWireRibbonMesh(THREE, points, width, height, material) {
    const geoms = [];
    for (let i = 0; i < points.length - 1; i += 1) {
      const [x1, y1, z1] = points[i];
      const [x2, y2, z2] = points[i + 1];
      const dx = x2 - x1;
      const dz = z2 - z1;
      const len = Math.hypot(dx, dz);
      if (len < 0.001) continue;
      const g = new THREE.BoxGeometry(len + width * 0.75, height, width);
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

    retainLabels(labelKeys) {
      for (const [key, entry] of this.cache) {
        if (labelKeys.has(entry.labelKey)) continue;
        entry.tex.dispose();
        this.cache.delete(key);
      }
    }

    getTexture(title, subtitle, themeKey, highlighted) {
      const labelKey = JSON.stringify([themeKey, title, subtitle || '']);
      const key = (highlighted ? '1' : '0') + labelKey;
      if (this.cache.has(key)) return this.cache.get(key);
      const t = THEMES[themeKey] || THEMES.night;
      const canvas = document.createElement('canvas');
      canvas.width = 640;
      canvas.height = subtitle ? 184 : 132;
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      ctx.fillStyle = highlighted ? 'rgba(22, 24, 27, 0.95)' : t.labelBg;
      ctx.fillRect(3, 3, canvas.width - 6, canvas.height - 6);

      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = highlighted ? '#ffffff' : t.labelText;
      ctx.font = '700 66px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      const displayTitle = title.length > 18 ? `${title.slice(0, 17)}…` : title;
      ctx.fillText(displayTitle, canvas.width / 2, subtitle ? 72 : canvas.height / 2 + 4, canvas.width - 44);

      if (subtitle) {
        ctx.fillStyle = highlighted ? '#ffe6b0' : t.labelSub;
        ctx.font = '600 34px ui-monospace, SFMono-Regular, Menlo, monospace';
        const displaySub = subtitle.length > 28 ? `${subtitle.slice(0, 27)}…` : subtitle;
        ctx.fillText(displaySub, canvas.width / 2, 136, canvas.width - 44);
      }

      const tex = new this.THREE.CanvasTexture(canvas);
      tex.colorSpace = this.THREE.SRGBColorSpace;
      tex.generateMipmaps = true;
      tex.minFilter = this.THREE.LinearMipmapLinearFilter;
      tex.magFilter = this.THREE.LinearFilter;
      tex.anisotropy = this.maxAnisotropy;
      tex.needsUpdate = true;
      const entry = { labelKey, tex, aspect: canvas.width / canvas.height };
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
      this.spotlightMode = 'v2';
      this.reducedMotion = Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
      this.descriptor = null;

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

      this.camPos = new THREE.Vector3(0, 6.4, 11.5);
      this.camTarget = new THREE.Vector3(0, 1.3, -11.5);
      this.overviewPos = new THREE.Vector3(0, 6.4, 11.5);
      this.overviewTarget = new THREE.Vector3(0, 1.3, -11.5);
      this.flight = null;
      this.establishingShot = null;
      this.towerRiseProgress = 1;
      this.lastInputTime = performance.now();

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
        blockTop: 2.2,
        blockHalfDiag: 0.55,
      };

      this._blockLodState = new Map();
      this._pedestalLodState = new Map();

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
      this.renderer.toneMappingExposure = 1.02;
      this.renderer.outputColorSpace = THREE.SRGBColorSpace;
      this.renderer.shadowMap.enabled = true;
      this.renderer.shadowMap.type = THREE.PCFShadowMap;
      this.renderer.shadowMap.autoUpdate = false;

      this.scene = new THREE.Scene();
      this.camera = new THREE.PerspectiveCamera(42, window.innerWidth / Math.max(1, window.innerHeight), 0.25, 650);
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
              float skyT = pow(clamp(h, 0.0, 1.0), 0.52);
              col = mix(uHorizon, uZenith, skyT);
              float band = exp(-h * 14.0);
              col = mix(col, uBand, band * 0.40);
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

      const groundGeo = new THREE.PlaneGeometry(900, 900);
      groundGeo.rotateX(-Math.PI / 2);
      this.groundMat = new THREE.MeshStandardMaterial({
        color: t.groundNear,
        roughness: 0.90,
        metalness: 0.06,
      });
      this.groundMesh = new THREE.Mesh(groundGeo, this.groundMat);
      this.groundMesh.receiveShadow = true;
      this.groundMesh.position.y = 0;
      this.scene.add(this.groundMesh);

      // Distance shading stays on the shadow-receiving floor material.
      this.groundFar = new THREE.Color(t.groundFar);
      this.groundMat.onBeforeCompile = shader => {
        shader.uniforms.roomGroundFar = { value: this.groundFar };
        shader.fragmentShader = 'uniform vec3 roomGroundFar;\n' + shader.fragmentShader.replace(
          '#include <color_fragment>',
          '#include <color_fragment>\ndiffuseColor.rgb = mix(diffuseColor.rgb, roomGroundFar, smoothstep(8.0, 140.0, length(vViewPosition)));'
        );
      };

      this.hemiLight = new THREE.HemisphereLight(t.hemiSky, t.hemiGround, t.hemiIntensity);
      this.scene.add(this.hemiLight);

      this.keyLight = new THREE.DirectionalLight(t.keyColor, t.keyIntensity);
      this.keyLight.position.set(24, 38, 30);
      this.keyLight.castShadow = false;
      this.scene.add(this.keyLight);

      this.headlight = new THREE.PointLight(t.headlightColor, t.headlightIntensity, 46, 1.5);
      this.headlight.position.set(0, 1.6, 0);
      this.camera.add(this.headlight);
    }

    _initSpotlightRig() {
      const THREE = this.THREE;
      const t = THEMES[this.themeKey];
      const h = 18.5;
      const poolR = 1.85;
      const angle = Math.atan(poolR / h);

      // Persistent real SpotLight (never toggled visible/castShadow/map on click -> zero hitch)
      this.spotMapTex = createSpotMapTexture(THREE);
      this.spotLight = new THREE.SpotLight(t.spotV2Color, 0, 0, angle, 0.42, 2);
      this.spotLight.position.set(0, h, 0);
      this.spotLight.target.position.set(0, 0, 0);
      this.spotLight.map = this.spotMapTex;
      this.spotLight.castShadow = true;
      this.spotLight.shadow.mapSize.set(1024, 1024);
      this.spotLight.shadow.radius = 3;
      this.spotLight.shadow.camera.near = h - 7.5;
      this.spotLight.shadow.camera.far = h + 2.5;
      this.spotLight.shadow.bias = -0.0005;
      this.spotLight.shadow.normalBias = 0.02;
      if ('intensity' in this.spotLight.shadow) {
        this.spotLight.shadow.intensity = 0.72;
      }
      this.scene.add(this.spotLight);
      this.scene.add(this.spotLight.target);

      // Dual-layer Spotlight v2 Volumetric Cone (NO physical fixture! Unmistakable descending beam + block color preservation)
      const makeVolumetricShaderMat = (colorHex, peakVal, edgePow, blockProtectMin) => {
        const uniforms = {
          uColor: { value: new THREE.Color(colorHex) },
          uPeak: { value: peakVal },
          uEdgePow: { value: edgePow },
          uBlockMin: { value: blockProtectMin },
          uFloorY: { value: 0.4 },
          uBlockTop: { value: 2.2 },
          uFade: { value: 0.0 },
          uHeight: { value: h },
        };
        const mat = new THREE.ShaderMaterial({
          side: THREE.FrontSide,
          transparent: true,
          depthWrite: false,
          fog: false,
          blending: t.additiveBeam ? THREE.AdditiveBlending : THREE.NormalBlending,
          uniforms,
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
            uniform float uEdgePow;
            uniform float uBlockMin;
            uniform float uFloorY;
            uniform float uBlockTop;
            uniform float uFade;
            varying vec3 vN;
            varying vec3 vW;
            varying float vT;
            void main() {
              if (uFade < 0.001) discard;
              float nv = abs(dot(normalize(vN), normalize(cameraPosition - vW)));
              // Strong theatrical overhead beam tapering gently toward floor while protecting block hue
              float apexFade = smoothstep(0.0, 0.09, vT);
              float lengthTaper = mix(1.0, 0.64, vT);
              float floorFade = smoothstep(0.0, 0.35, vW.y - uFloorY);
              float blockProtect = mix(uBlockMin, 1.0, smoothstep(uBlockTop - 0.15, uBlockTop + 1.15, vW.y));
              float a = uPeak * pow(nv, uEdgePow) * apexFade * lengthTaper * floorFade * blockProtect * uFade;
              a += (rand(gl_FragCoord.xy) - 0.5) / 255.0;
              gl_FragColor = vec4(uColor, max(a, 0.0));
              #include <colorspace_fragment>
            }
          `,
        });
        return { mat, uniforms };
      };

      // Outer atmospheric volumetric cone
      const outerGeo = new THREE.CylinderGeometry(0.18, poolR * 1.12, h, 64, 1, true);
      outerGeo.translate(0, -h / 2, 0);
      const outerBuilt = makeVolumetricShaderMat(t.beamOuterColor, t.beamOuterPeak, 1.32, 0.36);
      this.beamV2Uniforms = outerBuilt.uniforms;
      this.beamV2Mat = outerBuilt.mat;
      this.beamV2Mesh = new THREE.Mesh(outerGeo, this.beamV2Mat);
      this.beamV2Mesh.frustumCulled = false;
      this.beamV2Mesh.renderOrder = 10;
      this.beamV2Mesh.userData = { isSpotBeam: true };
      this.scene.add(this.beamV2Mesh);

      // Inner luminous core shaft
      const coreGeo = new THREE.CylinderGeometry(0.10, poolR * 0.56, h, 48, 1, true);
      coreGeo.translate(0, -h / 2, 0);
      const coreBuilt = makeVolumetricShaderMat(t.beamCoreColor, t.beamCorePeak, 2.05, 0.24);
      this.beamCoreUniforms = coreBuilt.uniforms;
      this.beamCoreMat = coreBuilt.mat;
      this.beamCoreMesh = new THREE.Mesh(coreGeo, this.beamCoreMat);
      this.beamCoreMesh.frustumCulled = false;
      this.beamCoreMesh.renderOrder = 11;
      this.beamCoreMesh.userData = { isSpotBeam: true };
      this.scene.add(this.beamCoreMesh);

      // Focused radial floor-pool halo disc right around the selected file's base
      this.poolHaloTex = createFloorPoolHaloTexture(THREE);
      const v2PoolGeo = new THREE.CircleGeometry(poolR * 1.05, 48);
      v2PoolGeo.rotateX(-Math.PI / 2);
      this.poolV2Mat = new THREE.MeshBasicMaterial({
        color: t.poolHaloColor,
        map: this.poolHaloTex,
        transparent: true,
        opacity: 0,
        depthWrite: false,
        fog: false,
        blending: t.additiveBeam ? THREE.AdditiveBlending : THREE.NormalBlending,
      });
      this.poolV2Mesh = new THREE.Mesh(v2PoolGeo, this.poolV2Mat);
      this.poolV2Mesh.frustumCulled = false;
      this.poolV2Mesh.renderOrder = 6;
      this.poolV2Mesh.visible = false;
      this.scene.add(this.poolV2Mesh);

      // Known-Good Prototype Baseline Beam + Floor Pool (for direct A/B comparison)
      const baseBeamGeo = new THREE.CylinderGeometry(0.12, 1.35, 14, 64, 1, true);
      baseBeamGeo.translate(0, -7.0, 0);
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

      const basePoolGeo = new THREE.CircleGeometry(2.3, 48);
      basePoolGeo.rotateX(-Math.PI / 2);
      this.poolBaseMat = new THREE.MeshBasicMaterial({
        map: this.poolHaloTex,
        transparent: true,
        opacity: 0,
        depthWrite: false,
        fog: false,
        blending: t.additiveBeam ? THREE.AdditiveBlending : THREE.NormalBlending,
      });
      this.poolBaseMesh = new THREE.Mesh(basePoolGeo, this.poolBaseMat);
      this.poolBaseMesh.frustumCulled = false;
      this.poolBaseMesh.renderOrder = 5;
      this.poolBaseMesh.visible = false;
      this.scene.add(this.poolBaseMesh);
    }

    _precompileShaders() {
      try {
        this.renderer.compile(this.scene, this.camera);
      } catch (_) {}
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
          if (isPan) this._panCamera(dx, dy);
          else this._orbitCamera(dx, dy);
          this.requestRender();
          return;
        }
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

      dom.addEventListener('pointerdown', onPointerDown);
      dom.addEventListener('pointermove', onPointerMove);
      dom.addEventListener('pointerup', onPointerUp);
      dom.addEventListener('wheel', onWheel, { passive: false });
      dom.addEventListener('contextmenu', (ev) => ev.preventDefault());
      const reader = document.getElementById?.('inspector-drawer');
      if (reader && window.ResizeObserver) {
        this.readerObserver = new window.ResizeObserver(() => {
          this._frameReader(reader);
          this.requestRender();
        });
        this.readerObserver.observe(reader);
      }
      window.addEventListener('resize', () => this.resize());
      window.addEventListener('pagehide', (event) => {
        if (!event.persisted) {
          this.readerObserver?.disconnect();
          this._clearLabelMeshes();
          this.labelAtlas.retainLabels(new Set());
        }
      });
    }

    _orbitCamera(dx, dy) {
      const THREE = this.THREE;
      const offset = this.camPos.clone().sub(this.camTarget);
      const spherical = new THREE.Spherical().setFromVector3(offset);
      spherical.theta -= dx * 0.0055;
      spherical.phi -= dy * 0.0045;
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

    _frameReader(reader) {
      if (!reader || reader.hidden) {
        this.camera.clearViewOffset();
        return;
      }
      const viewport = this.canvas.getBoundingClientRect();
      const bounds = reader.getBoundingClientRect();
      const width = viewport.width;
      const height = viewport.height;
      // A docked reader shifts the optical centre into the remaining landscape.
      // Full-screen readers are intentionally allowed on small screens.
      const left = bounds.left - viewport.left;
      const above = bounds.top - viewport.top;
      if (left >= width * 0.24 && bounds.width > 0) {
        this.camera.setViewOffset(width, height, (width - left) / 2, 0, width, height);
      } else if (above >= height * 0.24 && bounds.height > 0) {
        this.camera.setViewOffset(width, height, 0, (height - above) / 2, width, height);
      } else {
        this.camera.clearViewOffset();
      }
    }

    resize() {
      if (this._disposed) return;
      const w = window.innerWidth;
      const h = Math.max(1, window.innerHeight);
      this.camera.aspect = w / h;
      this._frameReader(document.getElementById?.('inspector-drawer'));
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
      this.groundFar.setHex(t.groundFar);
      this.hemiLight.color.setHex(t.hemiSky);
      this.hemiLight.groundColor.setHex(t.hemiGround);
      this.keyLight.color.setHex(t.keyColor);
      this.headlight.color.setHex(t.headlightColor);

      this.spotLight.color.setHex(this.spotlightMode === 'v2' ? t.spotV2Color : 0xfff1c9);
      const blend = t.additiveBeam ? this.THREE.AdditiveBlending : this.THREE.NormalBlending;
      this.beamV2Uniforms.uColor.value.setHex(t.beamOuterColor);
      this.beamV2Uniforms.uPeak.value = t.beamOuterPeak;
      this.beamV2Mat.blending = blend;
      this.beamV2Mat.needsUpdate = true;
      this.beamCoreUniforms.uColor.value.setHex(t.beamCoreColor);
      this.beamCoreUniforms.uPeak.value = t.beamCorePeak;
      this.beamCoreMat.blending = blend;
      this.beamCoreMat.needsUpdate = true;
      this.poolV2Mat.color.setHex(t.poolHaloColor);
      this.poolV2Mat.blending = blend;
      this.poolV2Mat.needsUpdate = true;
      this.beamBaseMat.blending = blend;
      this.beamBaseMat.needsUpdate = true;
      this.poolBaseMat.blending = blend;
      this.poolBaseMat.needsUpdate = true;

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
      const h = mode === 'v2' ? 18.5 : 14.0;
      const poolR = 1.85;
      if (mode === 'v2') {
        this.spotLight.color.setHex(t.spotV2Color);
        this.spotLight.distance = 0;
        this.spotLight.decay = 2;
        this.spotLight.penumbra = 0.42;
        this.spotLight.angle = Math.atan(poolR / h);
        this.spotLight.map = this.spotMapTex;
        this.spotLight.shadow.mapSize.set(1024, 1024);
      } else {
        this.spotLight.color.setHex(0xfff1c9);
        this.spotLight.distance = 40;
        this.spotLight.decay = 1;
        this.spotLight.penumbra = 0.5;
        this.spotLight.angle = 0.30;
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
      if (this.reducedMotion) this.cancelIntroAndTour();
      this.requestRender();
    }

    setLayout(layout, { descriptor = null, runEstablishingShot = false, preserveCamera = false } = {}) {
      const previous = this.layout?.nodeById.get(this.selectedNodeId);
      const selected = layout.nodeById.get(this.selectedNodeId);
      this.layout = layout;
      if (selected?.nodeType === 'item' && this.spotState.targetActive) {
        this._bindSpotlight(selected);
      } else if (!selected && this.selectedNodeId) {
        this.flight = null;
        this.selectedNodeId = null;
        this.focusedPedestalId = 'root';
        Object.assign(this.spotState, { active: false, targetActive: false, dimL: 0, spotL: 0 });
        this._applyLightingAndDimming();
      }
      for (const id of this._blockLodState.keys()) {
        if (!layout.nodeById.has(id)) this._blockLodState.delete(id);
      }
      if (preserveCamera && previous && selected) {
        const delta = new this.THREE.Vector3(
          selected.x - previous.x, selected.y - previous.y, selected.z - previous.z
        );
        if (this.flight) {
          this.flight.toPos.add(delta);
          this.flight.toTarget.add(delta);
        } else {
          this.camPos.add(delta);
          this.camTarget.add(delta);
          this.camera.position.copy(this.camPos);
          this.camera.lookAt(this.camTarget);
        }
      }
      if (descriptor) this.descriptor = descriptor;
      this._clearWorldGroup();
      this._buildWorldGeometry(layout);
      this._computeOverviewCamera(layout);

      if (runEstablishingShot && !this.reducedMotion) {
        this.towerRiseProgress = 0.04;
        this._updateBlockMatrices();
        const offset = this.overviewPos.clone().sub(this.overviewTarget);
        const startOff = offset.clone().multiplyScalar(1.62);
        startOff.applyAxisAngle(new this.THREE.Vector3(0, 1, 0), 0.30);
        startOff.y *= 1.15;
        this.camPos.copy(this.overviewTarget).add(startOff);
        this.camTarget.copy(this.overviewTarget);
        this.camera.position.copy(this.camPos);
        this.camera.lookAt(this.camTarget);
        this.establishingShot = {
          startTime: performance.now(),
          durationMs: 950,
          fromPos: this.camPos.clone(),
          toPos: this.overviewPos.clone(),
        };
      } else {
        this.towerRiseProgress = 1;
        this._updateBlockMatrices();
        if (!preserveCamera && !this.flight) {
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
      const root = layout.nodeById.get('root');
      const { pos, target } = this._directoryPose(root);
      this.overviewPos.copy(pos);
      this.overviewTarget.copy(target);
    }

    _directoryPose(node) {
      // Fit this deck and the nearest branching pads; distant siblings stay explorable.
      const children = this.layout.pedestals.filter(p => p.parentId === node.id)
        .sort((a, b) => Math.abs(a.x - node.x) - Math.abs(b.x - node.x)).slice(0, 5);
      const decks = [node, ...children];
      const front = node.z + node.depth / 2 + 1;
      const rear = Math.min(...decks.map(p => p.z - p.depth / 2));
      const target = new this.THREE.Vector3(node.x, 1.4, (front + rear) / 2);
      const pitch = 18 * Math.PI / 180;
      const sin = Math.sin(pitch);
      const cos = Math.cos(pitch);
      const tan = Math.tan(this.camera.fov * Math.PI / 360);
      let distance = 9;
      for (const deck of decks) {
        const halfX = Math.abs(deck.x - target.x) + deck.width / 2 + 0.4;
        for (const z of [deck.z - deck.depth / 2, deck.z + deck.depth / 2 + 1]) {
          for (const y of [0, deck.height + 4.35]) {
            const dz = z - target.z;
            const dy = y - target.y;
            const towardEye = dz * cos + dy * sin;
            distance = Math.max(distance,
              towardEye + halfX / (tan * this.camera.aspect * 0.90),
              towardEye + Math.abs(dy * cos - dz * sin) / (tan * 0.74));
          }
        }
      }
      return { target, pos: target.clone().add(new this.THREE.Vector3(0, sin * distance, cos * distance)) };
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
      this.pickables = [];
      this.pedestalVisuals.clear();
      this.wireVisuals = [];
      this.instancedRefs = {};
    }

    _buildWorldGeometry(layout) {
      const THREE = this.THREE;
      const t = THEMES[this.themeKey];

      // 1. Architectural Two-Tier SGI Directory Pedestals
      const pedPlinthMat = new THREE.MeshStandardMaterial({
        color: t.pedestalPlinth,
        roughness: 0.78,
        metalness: 0.24,
      });
      const pedTopMat = new THREE.MeshStandardMaterial({
        color: t.pedestalTop,
        roughness: 0.92,
        metalness: 0.08,
      });
      const pedDeckMat = new THREE.MeshStandardMaterial({
        color: t.pedestalDeck,
        roughness: 0.96,
        metalness: 0.04,
      });
      const pedSelectedMat = new THREE.MeshStandardMaterial({
        color: t.pedestalSelected,
        roughness: 0.50,
        metalness: 0.18,
        emissive: new THREE.Color(t.pedestalSelected),
        emissiveIntensity: 0.14,
      });
      const pedCarpetMat = new THREE.MeshBasicMaterial({ color: t.pedestalTop });
      const edgeMat = new THREE.LineBasicMaterial({ color: t.pedestalEdge, transparent: true, opacity: 0.55 });
      const edgeSelectedMat = new THREE.LineBasicMaterial({ color: 0xfff3d4, transparent: true, opacity: 0.95 });
      this._sharedMats = { pedPlinthMat, pedTopMat, pedDeckMat, pedSelectedMat, pedCarpetMat, edgeMat, edgeSelectedMat };

      for (const ped of layout.pedestals) {
        const group = new THREE.Group();
        group.position.set(ped.x, 0, ped.z);

        const plinthH = Math.max(0.14, ped.height * 0.42);
        const topH = Math.max(0.16, ped.height - plinthH);

        // Wider darker foundation plinth
        const plinthGeo = new THREE.BoxGeometry(ped.width + 0.26, plinthH, ped.depth + 0.26);
        plinthGeo.translate(0, plinthH / 2, 0);
        const plinth = new THREE.Mesh(plinthGeo, pedPlinthMat);
        plinth.receiveShadow = true;
        group.add(plinth);

        // Main brushed slate platform slab
        const slabGeo = new THREE.BoxGeometry(ped.width, topH, ped.depth);
        slabGeo.translate(0, plinthH + topH / 2, 0);
        const slab = new THREE.Mesh(slabGeo, pedTopMat);
        slab.castShadow = false;
        slab.receiveShadow = true;
        slab.userData = { nodeId: ped.id, nodeType: ped.nodeType };
        group.add(slab);
        this.pickables.push(slab);

        // Subtle recessed inner deck inset so pedestals look like SGI directory pads
        const deckGeo = new THREE.PlaneGeometry(Math.max(1.2, ped.width - 0.34), Math.max(1.0, ped.depth - 0.34));
        deckGeo.rotateX(-Math.PI / 2);
        const deck = new THREE.Mesh(deckGeo, pedDeckMat);
        deck.position.y = ped.height + 0.004;
        deck.receiveShadow = true;
        group.add(deck);

        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(slabGeo), edgeMat);
        group.add(edges);

        // Far LOD carpet
        const carpetGeo = new THREE.PlaneGeometry(ped.width, ped.depth);
        carpetGeo.rotateX(-Math.PI / 2);
        const carpet = new THREE.Mesh(carpetGeo, pedCarpetMat);
        carpet.position.y = 0.03;
        carpet.visible = false;
        group.add(carpet);

        this.worldGroup.add(group);
        this.pedestalVisuals.set(ped.id, { group, plinth, slab, deck, edges, carpet, ped });
      }

      // 2. Architectural Parent-Child Ground Conduits (recessed metallic channel + restrained trace)
      const channelMat = new THREE.MeshStandardMaterial({
        color: t.wireChannel,
        roughness: 0.78,
        metalness: 0.28,
      });
      this._sharedMats.channelMat = channelMat;
      for (const wire of layout.wires) {
        const channel = createWireRibbonMesh(THREE, wire.points, 0.21, 0.025, channelMat);
        const coreMat = new THREE.MeshStandardMaterial({
          color: t.wireBase,
          emissive: new THREE.Color(t.wireBase),
          emissiveIntensity: 0.48,
          roughness: 0.42,
          metalness: 0.25,
        });
        const core = createWireRibbonMesh(THREE, wire.points, 0.11, 0.056, coreMat);
        const glowMat = new THREE.MeshBasicMaterial({
          color: t.wireBase,
          transparent: true,
          opacity: t.wireGlowAlpha,
          depthWrite: false,
          blending: t.additiveBeam ? THREE.AdditiveBlending : THREE.NormalBlending,
        });
        const glow = createWireRibbonMesh(THREE, wire.points, 0.23, 0.012, glowMat);
        if (channel) {
          channel.position.y = 0.015;
          channel.receiveShadow = true;
          this.worldGroup.add(channel);
        }
        if (core) {
          core.position.y = 0.028;
          core.userData = { wireParentId: wire.parentId, wireChildId: wire.childId, isWire: true };
          this.worldGroup.add(core);
          this.pickables.push(core);
        }
        if (glow) {
          glow.position.y = 0.014;
          this.worldGroup.add(glow);
        }
        this.wireVisuals.push({ wire, coreMat, glowMat, channel, core, glow });
      }

      // 3. Shape-Based Instanced Architectural File Monoliths (honors EVERY custom legend kind & color!)
      const solidBlocks = [];
      const notchBlocks = [];
      const hollowBlocks = [];
      for (const b of layout.blocks) {
        const shape = (b.legendEntry && b.legendEntry.shape) || 'solid';
        if (shape === 'notch') notchBlocks.push(b);
        else if (shape === 'hollow') hollowBlocks.push(b);
        else solidBlocks.push(b);
      }

      const bw = global.RoomViewLayout.BLOCK_WIDTH;
      const archGeo = createArchitecturalBlockGeometry(THREE, bw);

      const solidMat = new THREE.MeshStandardMaterial({
        color: 0xffffff,
        roughness: 0.44,
        metalness: 0.16,
      });
      const notchMat = new THREE.MeshStandardMaterial({
        color: 0xffffff,
        roughness: 0.40,
        metalness: 0.14,
      });
      const notchCrownMat = new THREE.MeshStandardMaterial({
        color: t.failCrown,
        roughness: 0.34,
        metalness: 0.12,
        emissive: new THREE.Color(t.failCrown),
        emissiveIntensity: 0.16,
      });
      const hollowMat = new THREE.MeshStandardMaterial({
        color: 0xffffff,
        roughness: 0.50,
        metalness: 0.20,
      });

      const makeInstanced = (geo, mat, list, role) => {
        if (!list.length) return null;
        const mesh = new THREE.InstancedMesh(geo, mat, list.length);
        mesh.frustumCulled = false;
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        mesh.userData = { instancedRole: role, blockList: list };
        this.worldGroup.add(mesh);
        return mesh;
      };

      const solidMesh = makeInstanced(archGeo, solidMat, solidBlocks, 'solid');
      if (solidMesh) this.pickables.push(solidMesh);

      const notchMesh = makeInstanced(archGeo.clone(), notchMat, notchBlocks, 'notch');
      if (notchMesh) this.pickables.push(notchMesh);

      const notchCrownGeo = createNotchCrownGeometry(THREE, bw);
      const notchCrownMesh = makeInstanced(notchCrownGeo, notchCrownMat, notchBlocks, 'notch-crown');
      if (notchCrownMesh) notchCrownMesh.castShadow = false;

      const hollowPostsGeo = createHollowPostsGeometry(THREE, bw);
      const hollowPostsMesh = makeInstanced(hollowPostsGeo, hollowMat, hollowBlocks, 'hollow');
      const hollowRingGeo = createHollowRingGeometry(THREE, bw);
      const hollowTopRingMesh = makeInstanced(hollowRingGeo, hollowMat, hollowBlocks, 'hollow-top');
      const hollowBotRingMesh = makeInstanced(hollowRingGeo.clone(), hollowMat, hollowBlocks, 'hollow-bot');
      const hollowHitMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.0, depthWrite: false });
      const hollowHitMesh = makeInstanced(new THREE.BoxGeometry(bw, 1.0, bw).translate(0, 0.5, 0), hollowHitMat, hollowBlocks, 'hollow-hit');
      if (hollowHitMesh) {
        hollowHitMesh.castShadow = false;
        hollowHitMesh.receiveShadow = false;
        this.pickables.push(hollowHitMesh);
      }

      // Far LOD proxies
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

      // 4. SpaceTree Summary / "+N more" Stacked Outcome Towers (honors EVERY declared legend kind!)
      const summaryMeshes = [];
      const legendEntries = global.RoomViewLayout.getLegendEntries(this.descriptor);
      for (const sNode of layout.summaryBlocks) {
        const sGroup = new THREE.Group();
        sGroup.position.set(sNode.x, sNode.y, sNode.z);
        const total = Math.max(1, sNode.counts.total);
        const byKind = sNode.counts.byKind || {};
        // Put non-failure kinds at bottom and failure kinds visibly at top cap
        const orderedEntries = legendEntries.slice().sort((a, b) => Number(a.isFailure) - Number(b.isFailure));
        let yCursor = 0;
        for (const entry of orderedEntries) {
          const count = byKind[entry.kind] || 0;
          if (count <= 0) continue;
          const sh = Math.max(0.16, (count / total) * sNode.height);
          const geo = new THREE.BoxGeometry(sNode.width, sh, sNode.depth);
          geo.translate(0, yCursor + sh / 2, 0);
          const hex = resolveBlockHex({ kind: entry.kind, legendEntry: entry }, this.themeKey);
          const mat = new THREE.MeshStandardMaterial({
            color: hex,
            roughness: 0.46,
            metalness: 0.14,
            emissive: new THREE.Color(hex),
            emissiveIntensity: 0.06,
          });
          const mesh = new THREE.Mesh(geo, mat);
          mesh.castShadow = true;
          mesh.receiveShadow = true;
          mesh.userData = { nodeId: sNode.id, nodeType: sNode.nodeType };
          sGroup.add(mesh);
          this.pickables.push(mesh);
          yCursor += sh;
        }
        const frameGeo = new THREE.BoxGeometry(sNode.width + 0.06, Math.max(0.24, yCursor) + 0.04, sNode.depth + 0.06);
        frameGeo.translate(0, Math.max(0.24, yCursor) / 2, 0);
        const frame = new THREE.LineSegments(
          new THREE.EdgesGeometry(frameGeo),
          new THREE.LineBasicMaterial({ color: t.pedestalEdge, transparent: true, opacity: 0.80 })
        );
        sGroup.add(frame);
        this.worldGroup.add(sGroup);
        summaryMeshes.push({ node: sNode, group: sGroup });
      }

      this.instancedRefs = {
        solidBlocks,
        notchBlocks,
        hollowBlocks,
        solidMesh,
        notchMesh,
        notchCrownMesh,
        hollowPostsMesh,
        hollowTopRingMesh,
        hollowBotRingMesh,
        hollowHitMesh,
        lodPlaneMesh,
        solidMat,
        notchMat,
        notchCrownMat,
        hollowMat,
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
          let rise = 1.0;
          if (this.towerRiseProgress < 1.0) {
            const delay = clamp(Math.abs(b.x) * 0.008 + Math.abs(b.z) * 0.009, 0, 0.55);
            rise = smootherstep((this.towerRiseProgress - delay) / Math.max(0.01, 1 - delay));
            rise = Math.max(0.04, rise);
          }

          const dist = Math.hypot(camPos.x - b.x, camPos.y - (b.y + b.height * 0.5), camPos.z - b.z);
          const prevLod = this._blockLodState.get(b.id) || 0;
          let lod = prevLod;
          if (prevLod === 0 && dist > 54) lod = 1;
          else if (prevLod === 1 && dist < 46) lod = 0;
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

      updateList(refs.solidBlocks, refs.solidMesh);
      updateList(refs.notchBlocks, refs.notchMesh, (i, b, h, lod, dummy, zeroScale) => {
        if (!refs.notchCrownMesh) return;
        if (lod === 0) {
          dummy.position.set(b.x, b.y + h, b.z);
          dummy.rotation.set(0, 0, 0);
          dummy.scale.set(1, 1, 1);
          dummy.updateMatrix();
          refs.notchCrownMesh.setMatrixAt(i, dummy.matrix);
        } else {
          refs.notchCrownMesh.setMatrixAt(i, zeroScale);
        }
      });
      if (refs.notchCrownMesh) refs.notchCrownMesh.instanceMatrix.needsUpdate = true;

      updateList(refs.hollowBlocks, refs.hollowPostsMesh, (i, b, h, lod, dummy, zeroScale) => {
        if (lod === 0) {
          dummy.position.set(b.x, b.y + h - 0.07, b.z);
          dummy.rotation.set(0, 0, 0);
          dummy.scale.set(1, 1, 1);
          dummy.updateMatrix();
          if (refs.hollowTopRingMesh) refs.hollowTopRingMesh.setMatrixAt(i, dummy.matrix);

          dummy.position.set(b.x, b.y, b.z);
          dummy.updateMatrix();
          if (refs.hollowBotRingMesh) refs.hollowBotRingMesh.setMatrixAt(i, dummy.matrix);

          dummy.scale.set(1, h, 1);
          dummy.updateMatrix();
          if (refs.hollowHitMesh) refs.hollowHitMesh.setMatrixAt(i, dummy.matrix);
        } else {
          if (refs.hollowTopRingMesh) refs.hollowTopRingMesh.setMatrixAt(i, zeroScale);
          if (refs.hollowBotRingMesh) refs.hollowBotRingMesh.setMatrixAt(i, zeroScale);
          if (refs.hollowHitMesh) refs.hollowHitMesh.setMatrixAt(i, zeroScale);
        }
      });
      if (refs.hollowTopRingMesh) refs.hollowTopRingMesh.instanceMatrix.needsUpdate = true;
      if (refs.hollowBotRingMesh) refs.hollowBotRingMesh.instanceMatrix.needsUpdate = true;
      if (refs.hollowHitMesh) refs.hollowHitMesh.instanceMatrix.needsUpdate = true;

      if (refs.lodPlaneMesh) {
        const all = this.layout.blocks;
        const col = new THREE.Color();
        for (let i = 0; i < all.length; i += 1) {
          const b = all[i];
          const lod = this._blockLodState.get(b.id) || 0;
          if (lod === 1) {
            const dist = Math.hypot(camPos.x - b.x, camPos.z - b.z);
            const widthScale = dist > 84 ? 0.24 : 1.0;
            dummy.position.set(b.x, b.y, b.z);
            dummy.rotation.set(0, 0, 0);
            dummy.scale.set(widthScale, b.height, 1);
            dummy.updateMatrix();
            refs.lodPlaneMesh.setMatrixAt(i, dummy.matrix);
            col.setHex(resolveBlockHex(b, this.themeKey)).multiplyScalar(0.65);
            refs.lodPlaneMesh.setColorAt(i, col);
          } else {
            refs.lodPlaneMesh.setMatrixAt(i, zeroScale);
          }
        }
        refs.lodPlaneMesh.instanceMatrix.needsUpdate = true;
        if (refs.lodPlaneMesh.instanceColor) refs.lodPlaneMesh.instanceColor.needsUpdate = true;
      }

      for (const [pedId, vis] of this.pedestalVisuals.entries()) {
        const p = vis.ped;
        const dist = Math.hypot(camPos.x - p.x, camPos.z - p.z);
        const prev = this._pedestalLodState.get(pedId) || 0;
        let lod = prev;
        if (prev === 0 && dist > 78 && p.nodeType !== 'root') lod = 1;
        else if (prev === 1 && dist < 66) lod = 0;
        this._pedestalLodState.set(pedId, lod);
        vis.plinth.visible = lod === 0;
        vis.slab.visible = lod === 0;
        vis.deck.visible = lod === 0;
        vis.edges.visible = lod === 0;
        vis.carpet.visible = lod === 1;
      }
    }

    _applyThemeToWorldMaterials() {
      const t = THEMES[this.themeKey];
      if (this._sharedMats) {
        this._sharedMats.pedPlinthMat.color.setHex(t.pedestalPlinth);
        this._sharedMats.pedTopMat.color.setHex(t.pedestalTop);
        this._sharedMats.pedDeckMat.color.setHex(t.pedestalDeck);
        this._sharedMats.pedSelectedMat.color.setHex(t.pedestalSelected);
        this._sharedMats.pedSelectedMat.emissive.setHex(t.pedestalSelected);
        this._sharedMats.pedCarpetMat.color.setHex(t.pedestalTop);
        this._sharedMats.edgeMat.color.setHex(t.pedestalEdge);
        if (this._sharedMats.channelMat) this._sharedMats.channelMat.color.setHex(t.wireChannel);
      }
      if (this.instancedRefs && this.instancedRefs.notchCrownMat) {
        this.instancedRefs.notchCrownMat.color.setHex(t.failCrown);
        this.instancedRefs.notchCrownMat.emissive.setHex(t.failCrown);
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
      const refs = this.instancedRefs;
      const selectedId = this.selectedNodeId;
      const hoveredId = this.hoveredNodeId;
      const activeChain = this._ancestorChainSet(selectedId || this.focusedPedestalId);
      const hasItemSpot = this.spotState.targetActive;

      // FSN 0.6x restraint: unselected blocks sit calm and saturated; selected pops vividly under spotlight
      const colorTmp = new THREE.Color();
      const paintList = (list, mesh) => {
        if (!list || !list.length || !mesh) return;
        for (let i = 0; i < list.length; i += 1) {
          const b = list[i];
          const isSel = b.id === selectedId;
          const isHov = b.id === hoveredId;
          colorTmp.setHex(resolveBlockHex(b, this.themeKey));
          if (isSel) {
            colorTmp.multiplyScalar(1.06);
          } else if (isHov) {
            colorTmp.multiplyScalar(0.92);
          } else {
            colorTmp.multiplyScalar(hasItemSpot ? 0.46 : 0.68);
          }
          mesh.setColorAt(i, colorTmp);
        }
        if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
      };

      paintList(refs.solidBlocks, refs.solidMesh);
      paintList(refs.notchBlocks, refs.notchMesh);
      paintList(refs.hollowBlocks, refs.hollowPostsMesh);
      paintList(refs.hollowBlocks, refs.hollowTopRingMesh);
      paintList(refs.hollowBlocks, refs.hollowBotRingMesh);

      for (const [pedId, vis] of this.pedestalVisuals.entries()) {
        const isActive = activeChain.has(pedId) || pedId === hoveredId;
        vis.slab.material = isActive && pedId !== 'root' ? this._sharedMats.pedSelectedMat : this._sharedMats.pedTopMat;
        vis.edges.material = isActive ? this._sharedMats.edgeSelectedMat : this._sharedMats.edgeMat;
      }

      this._applyLightingAndDimming();
    }

    _clearLabelMeshes() {
      while (this.labelGroup.children.length) {
        const c = this.labelGroup.children[this.labelGroup.children.length - 1];
        this.labelGroup.remove(c);
        if (c.geometry) c.geometry.dispose();
        if (c.material) c.material.dispose();
      }
      this.labelPool = [];
    }

    _rebuildLabelPool(layout) {
      const THREE = this.THREE;
      this._clearLabelMeshes();
      const labelKeys = new Set();

      const addLabelEntry = (node, title, subtitle, priority, worldW, yPos, zOffset, tiltDeg = -56) => {
        const texEntry = this.labelAtlas.getTexture(title, subtitle, this.themeKey, false);
        labelKeys.add(texEntry.labelKey);
        const h = worldW / texEntry.aspect;
        const geo = new THREE.PlaneGeometry(worldW, h);
        const mat = new THREE.MeshBasicMaterial({
          map: texEntry.tex,
          transparent: true,
          depthWrite: false,
          side: THREE.FrontSide,
        });
        const mesh = new THREE.Mesh(geo, mat);
        mesh.position.set(node.x, yPos, node.z + zOffset);
        mesh.rotation.x = (tiltDeg * Math.PI) / 180;
        mesh.renderOrder = 8;
        mesh.visible = false;
        this.labelGroup.add(mesh);
        this.labelPool.push({
          nodeId: node.id,
          parentId: node.parentId,
          nodeType: node.nodeType,
          treeDepth: node.treeDepth || 0,
          title,
          subtitle,
          priority,
          mesh,
          worldPos: mesh.position.clone(),
        });
      };

      for (const ped of layout.pedestals) {
        const labelTitle = ped.label;
        const w = Math.min(ped.width - 0.2, Math.max(2.2, labelTitle.length * 0.35));
        const prio = ped.treeDepth === 0 ? 110 : (ped.treeDepth === 1 ? 92 : 76 - Math.min(20, ped.treeDepth * 5));
        addLabelEntry(ped, labelTitle, ped.collapsed ? 'Expand' : '', prio, w, Math.max(0.43, ped.height * 0.56), ped.depth / 2 + 0.14, -22);
      }

      for (const sNode of layout.summaryBlocks) {
        const subtitle = global.RoomViewLayout.isWorkspace(this.descriptor)
          ? `${sNode.counts.total} listed files`
          : `${sNode.counts.fail} fail`;
        addLabelEntry(sNode, sNode.label, subtitle, 60, 1.75, sNode.y + sNode.height + 0.42, 0.0, -45);
      }

      for (const b of layout.blocks) {
        const unit = this.descriptor && this.descriptor.metric && this.descriptor.metric.unit
          ? ` ${this.descriptor.metric.unit}`
          : '';
        const mText = Number.isSafeInteger(b.item.fields?.file_bytes) && global.RoomViewLayout.isWorkspace(this.descriptor)
          ? `${b.item.fields.file_bytes} B`
          : b.item.metric != null ? `${b.item.metric}${unit}` : '';
        addLabelEntry(b, b.label, mText, 30, 1.65, b.y + b.height + 0.46, 0.0, -40);
      }
      // Keep both variants of current labels, including shared and temporarily hidden ones.
      this.labelAtlas.retainLabels(labelKeys);
    }

    _updateFloorLabels() {
      if (!this.labelPool.length) return;
      const THREE = this.THREE;
      const cam = this.camera;
      const w = window.innerWidth;
      const h = Math.max(1, window.innerHeight);
      const proj = new THREE.Vector3();

      const candidates = [];
      for (const item of this.labelPool) {
        item.mesh.visible = false;
        const isForce = item.nodeId === this.selectedNodeId || item.nodeId === this.hoveredNodeId;
        const isFocusedDirChild = item.nodeType === 'item' && item.parentId === this.focusedPedestalId;
        const dist = cam.position.distanceTo(item.worldPos);
        if (!isForce) {
          if (item.nodeType === 'item' && dist > (isFocusedDirChild ? 16.5 : 10.5)) continue;
          if ((item.nodeType === 'directory' || item.nodeType === 'stage') && dist > 62) continue;
          if (item.nodeType === 'group' && dist > 95) continue;
          if (item.nodeType === 'stage-summary' && dist > 28) continue;
          if (item.nodeType === 'more' && dist > 20) continue;
        }
        proj.copy(item.worldPos).project(cam);
        if (proj.z < -1 || proj.z > 1 || Math.abs(proj.x) > 1.04 || Math.abs(proj.y) > 1.04) continue;
        const sx = (proj.x * 0.5 + 0.5) * w;
        const sy = (-proj.y * 0.5 + 0.5) * h;
        const estW = clamp(128 * (17 / Math.max(4, dist)), 62, 185);
        const estH = clamp(36 * (17 / Math.max(4, dist)), 22, 54);
        const boost = isForce ? 10000 : (isFocusedDirChild ? 550 : item.priority * 10);
        const score = boost - dist;
        candidates.push({ item, isForce, isFocusedDirChild, sx, sy, estW, estH, score });
      }

      candidates.sort((a, b) => b.score - a.score);
      const accepted = [];
      const MAX_LABELS = 32;
      let nonSelectedItemLabels = 0;
      for (const cand of candidates) {
        if (!cand.isForce && accepted.length >= MAX_LABELS) continue;
        if (!cand.isForce && cand.item.nodeType === 'item') {
          if (this.spotState.targetActive || nonSelectedItemLabels >= 6) continue;
        }
        const r1 = {
          x0: cand.sx - cand.estW * 0.46,
          x1: cand.sx + cand.estW * 0.46,
          y0: cand.sy - cand.estH * 0.46,
          y1: cand.sy + cand.estH * 0.46,
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
      const h = isV2 ? 18.5 : 14.0;
      const poolR = 1.85;

      this.spotLight.position.set(s.targetX, s.floorY + h, s.targetZ);
      this.spotLight.target.position.set(s.targetX, s.floorY, s.targetZ);
      this.spotLight.target.updateMatrixWorld();

      if (isV2) {
        this.spotLight.angle = Math.atan(poolR / h);
        this.spotLight.shadow.camera.near = Math.max(1.0, h - 7.5);
        this.spotLight.shadow.camera.far = h + 2.5;
        this.spotLight.shadow.camera.updateProjectionMatrix();
        this.beamV2Mesh.position.set(s.targetX, s.floorY + h, s.targetZ);
        this.beamCoreMesh.position.set(s.targetX, s.floorY + h, s.targetZ);
        this.poolV2Mesh.position.set(s.targetX, s.floorY + 0.022, s.targetZ);
        this.beamV2Uniforms.uFloorY.value = s.floorY;
        this.beamV2Uniforms.uBlockTop.value = s.blockTop;
        this.beamCoreUniforms.uFloorY.value = s.floorY;
        this.beamCoreUniforms.uBlockTop.value = s.blockTop;
      } else {
        this.spotLight.shadow.camera.near = 1.0;
        this.spotLight.shadow.camera.far = 28.0;
        this.spotLight.shadow.camera.updateProjectionMatrix();
        this.beamBaseMesh.position.set(s.targetX, s.floorY + 14.0, s.targetZ);
        this.poolBaseMesh.position.set(s.targetX, s.floorY + 0.024, s.targetZ);
      }
      this._shadowDirty = true;
    }

    _applyLightingAndDimming() {
      const t = THEMES[this.themeKey];
      const dimL = this.spotState.dimL;
      const spotL = this.spotState.spotL;
      const isV2 = this.spotlightMode === 'v2';
      const activeChain = this._ancestorChainSet(this.selectedNodeId || this.focusedPedestalId);

      if (isV2) {
        this.hemiLight.intensity = t.hemiIntensity + (t.hemiDim - t.hemiIntensity) * dimL;
        this.keyLight.intensity = t.keyIntensity + (t.keyDim - t.keyIntensity) * dimL;
        this.headlight.intensity = t.headlightIntensity + (t.headlightDim - t.headlightIntensity) * dimL;
        this.scene.fog.density = t.fogDensity * (1 + 0.35 * dimL);
      } else {
        this.hemiLight.intensity = t.hemiIntensity * (1 - 0.78 * dimL);
        this.keyLight.intensity = t.keyIntensity * (1 - 0.82 * dimL);
        this.headlight.intensity = t.headlightIntensity * (1 - 0.86 * dimL);
        this.scene.fog.density = t.fogDensity * (1 + 0.25 * dimL);
      }

      for (const wv of this.wireVisuals) {
        const isActiveWire = activeChain.has(wv.wire.childId);
        if (isActiveWire) {
          wv.coreMat.color.setHex(t.wireSelected);
          wv.coreMat.emissive.setHex(t.wireSelected);
          wv.coreMat.emissiveIntensity = 1.35;
          wv.glowMat.opacity = t.wireGlowAlpha * 2.2;
        } else {
          wv.coreMat.color.setHex(t.wireBase);
          wv.coreMat.emissive.setHex(t.wireBase);
          wv.coreMat.emissiveIntensity = 0.48 * (1 - 0.62 * dimL);
          wv.glowMat.opacity = t.wireGlowAlpha * (1 - 0.62 * dimL);
        }
      }

      if (isV2) {
        this.spotLight.intensity = t.spotV2Intensity * spotL;
        this.beamV2Uniforms.uFade.value = spotL;
        this.beamCoreUniforms.uFade.value = spotL;
        this.beamV2Mesh.visible = spotL > 0.001;
        this.beamCoreMesh.visible = spotL > 0.001;
        this.poolV2Mesh.visible = spotL > 0.001;
        this.poolV2Mat.opacity = t.poolHaloOpacity * spotL;
        this.beamBaseMesh.visible = false;
        this.poolBaseMesh.visible = false;
      } else {
        this.spotLight.intensity = 280.0 * spotL;
        this.beamV2Mesh.visible = false;
        this.beamCoreMesh.visible = false;
        this.poolV2Mesh.visible = false;
        this.beamBaseMesh.visible = spotL > 0.001;
        this.poolBaseMesh.visible = spotL > 0.001;
        this.beamBaseMat.opacity = t.beamBaseOpacity * spotL;
        this.poolBaseMat.opacity = t.poolBaseOpacity * spotL;
      }
    }

    _bindSpotlight(blockNode) {
      const s = this.spotState;
      s.targetX = blockNode.x;
      s.targetZ = blockNode.z;
      s.floorY = blockNode.y;
      s.blockTop = blockNode.y + blockNode.height;
      s.blockHalfDiag = Math.hypot(blockNode.width * 0.5, blockNode.depth * 0.5);
      this._positionSpotlightOnState();
    }

    _startSpotlightOnBlock(blockNode) {
      const clickStart = performance.now();
      const s = this.spotState;
      this._bindSpotlight(blockNode);
      s.fromDimL = s.dimL;
      s.fromSpotL = s.spotL;
      s.targetActive = true;
      s.active = true;
      s.startTime = performance.now();
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

    _directoryWaypoints(fromId, toId) {
      const from = [...this._ancestorChainSet(fromId)];
      const to = [...this._ancestorChainSet(toId)];
      if (!from.length || !to.length) return [];
      while (from.length > 1 && to.length > 1 && from.at(-2) === to.at(-2)) {
        from.pop();
        to.pop();
      }
      // The shared ancestor occurs once on the path. Endpoints already have a camera pose.
      return from.concat(to.slice(0, -1).reverse()).slice(1, -1)
        .map((id) => this.layout.nodeById.get(id));
    }

    _flyTo(destPos, destTarget, { instant = false, fromId = 'root', toId = 'root' } = {}) {
      this.cancelIntroAndTour();
      destPos.y = Math.max(0.75, destPos.y);
      if (instant || this.reducedMotion) {
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
      const durationMs = 950;

      const midPos = this.camPos.clone().lerp(destPos, 0.5);
      if (horizTravel > curViewDist * 3.0) {
        const apogee = clamp(1.05 * Math.max(curViewDist, horizTravel * 0.36), 11, 38);
        midPos.y = Math.max(midPos.y, apogee);
      } else {
        midPos.y = Math.max(midPos.y, Math.max(this.camPos.y, destPos.y) + Math.min(3.4, dist * 0.12));
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
      const stops = this._directoryWaypoints(fromId, toId);
      if (stops.length) {
        const { Vector3, CatmullRomCurve3 } = this.THREE;
        const positions = stops.map((p) => new Vector3(p.x + 1.2, p.height + 5, p.z + p.depth / 2 + 3));
        const targets = stops.map((p) => new Vector3(p.x, p.height + 1, p.z));
        this.flight.positionCurve = new CatmullRomCurve3([this.flight.fromPos, ...positions, this.flight.toPos]);
        this.flight.targetCurve = new CatmullRomCurve3([this.flight.fromTarget, ...targets, this.flight.toTarget]);
      }
      this.requestRender();
    }

    frameOverview({ instant = false } = {}) {
      const fromId = this.focusedPedestalId;
      this.selectedNodeId = null;
      this.focusedPedestalId = 'root';
      this.clearSpotlight();
      this._updateColorsAndSelection();
      this._flyTo(this.overviewPos.clone(), this.overviewTarget.clone(), { instant, fromId });
    }

    focusNode(nodeId, { moveCamera = true, instant = false } = {}) {
      if (!this.layout) return null;
      const node = this.layout.nodeById.get(nodeId);
      if (!node) return null;
      const THREE = this.THREE;
      const fromId = this.focusedPedestalId;

      if (node.nodeType === 'item') {
        this.selectedNodeId = node.id;
        this.focusedPedestalId = node.parentId;
        this._startSpotlightOnBlock(node);
        this._updateColorsAndSelection();
        if (moveCamera) {
          // Low theatrical framing (~20 deg elevation) so the full overhead volumetric BEAM
          // descending from top of frame onto the file monolith + floor pool is unmistakable!
          const elev = (20 * Math.PI) / 180;
          const dist = clamp(node.height * 1.45 + 7.2, 8.2, 12.6);
          const target = new THREE.Vector3(node.x, node.y + Math.min(2.4, node.height * 0.68 + 0.55), node.z);
          const pos = new THREE.Vector3(
            node.x + 2.3,
            node.y + Math.sin(elev) * dist + 1.35,
            node.z + Math.cos(elev) * dist
          );
          this._flyTo(pos, target, { instant, fromId, toId: this.focusedPedestalId });
        }
      } else if (node.nodeType === 'root') {
        this.frameOverview({ instant });
      } else if (node.nodeType === 'directory' || node.nodeType === 'group' || node.nodeType === 'stage') {
        this.selectedNodeId = node.id;
        this.focusedPedestalId = node.id;
        this.clearSpotlight();
        this._updateColorsAndSelection();
        if (moveCamera) {
          const { pos, target } = this._directoryPose(node);
          this._flyTo(pos, target, { instant, fromId, toId: this.focusedPedestalId });
        }
      } else if (node.nodeType === 'more' || node.nodeType === 'stage-summary') {
        this.selectedNodeId = node.id;
        this.focusedPedestalId = node.parentId;
        this.clearSpotlight();
        this._updateColorsAndSelection();
        if (moveCamera) {
          const target = new THREE.Vector3(node.x, node.y + 1.0, node.z);
          const pos = new THREE.Vector3(node.x + 1.8, node.y + 4.4, node.z + 7.2);
          this._flyTo(pos, target, { instant, fromId, toId: this.focusedPedestalId });
        }
      }
      return node;
    }

    _pickAtEvent(ev) {
      const rect = this.canvas.getBoundingClientRect();
      this.pointerNdc.x = ((ev.clientX - rect.left) / Math.max(1, rect.width)) * 2 - 1;
      this.pointerNdc.y = -((ev.clientY - rect.top) / Math.max(1, rect.height)) * 2 + 1;
      this.raycaster.setFromCamera(this.pointerNdc, this.camera);

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
        this.focusNode(this.selectedNodeId, { moveCamera: true });
        return;
      }
      if (hit.kind === 'wire') {
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
      if (!this.layout) return '';
      const THREE = this.THREE;
      const savedPos = this.camera.position.clone();
      const savedQuat = this.camera.quaternion.clone();
      const b = this.layout.bounds;
      const span = Math.max(b.spanX, b.spanZ, 26);
      this.camera.position.set(b.centerX, span * 1.05, b.centerZ + 0.01);
      this.camera.lookAt(new THREE.Vector3(b.centerX, 0, b.centerZ));
      this.skyMesh.position.set(this.camera.position.x, 0, this.camera.position.z);
      this.groundMesh.position.set(this.camera.position.x, 0, this.camera.position.z);
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

      if (this.flight) {
        const f = this.flight;
        const tRaw = (now - f.startTime) / f.durationMs;
        const e = sigmoidEase(tRaw);
        if (f.positionCurve) {
          f.positionCurve.getPoint(e, this.camPos);
          f.targetCurve.getPoint(e, this.camTarget);
          this.camPos.y = Math.max(0.75, this.camPos.y);
        } else {
          const omt = 1 - e;
          this.camPos.set(
            omt * omt * f.fromPos.x + 2 * omt * e * f.midPos.x + e * e * f.toPos.x,
            Math.max(0.75, omt * omt * f.fromPos.y + 2 * omt * e * f.midPos.y + e * e * f.toPos.y),
            omt * omt * f.fromPos.z + 2 * omt * e * f.midPos.z + e * e * f.toPos.z
          );
          const lookE = sigmoidEase(Math.min(1, tRaw * 1.15));
          this.camTarget.lerpVectors(f.fromTarget, f.toTarget, lookE);
        }
        this.camera.position.copy(this.camPos);
        this.camera.lookAt(this.camTarget);
        if (tRaw >= 1) this.flight = null;
        else keepGoing = true;
      }

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

      this.skyMesh.position.set(this.camera.position.x, 0, this.camera.position.z);
      this.groundMesh.position.set(this.camera.position.x, 0, this.camera.position.z);

      this._updateBlockMatrices();
      this._updateFloorLabels();

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
