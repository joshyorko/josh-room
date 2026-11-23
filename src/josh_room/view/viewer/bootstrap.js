// CSP-safe zero-network bootstrapper:
// Decodes embedded gzip+base64 libraries in memory (NO fetch('data:'), NO CDN, NO unsafe-eval),
// registers a blob: importmap for Three.js, initializes sql.js with wasmBinary directly,
// and lazily loads pdf.js + blob worker on first PDF view.
(function (global) {
  'use strict';

  const blobUrlCache = new Map();
  let pdfJsPromise = null;

  async function getLibraryBlobUrl(scriptId, mimeType) {
    if (blobUrlCache.has(scriptId)) return blobUrlCache.get(scriptId);
    const bytes = await global.RoomViewData.decodeEmbeddedScriptBytes(scriptId);
    if (!bytes) throw new Error(`Missing embedded library blob: ${scriptId}`);
    const blob = new Blob([bytes], { type: mimeType || 'text/javascript' });
    const url = URL.createObjectURL(blob);
    blobUrlCache.set(scriptId, url);
    return url;
  }

  function loadClassicScript(url) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = url;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error(`Failed to evaluate script blob ${url}`));
      document.head.appendChild(s);
    });
  }

  function registerImportMap(importsObj) {
    // Registers blob: URLs in an importmap (never a CDN) before module evaluation
    const existing = document.querySelector('script[type="importmap"]');
    if (existing) return;
    const im = document.createElement('script');
    im.type = 'importmap';
    im.textContent = JSON.stringify({ imports: importsObj });
    document.head.appendChild(im);
  }

  async function initSqlModule() {
    const [sqlJsUrl, wasmBytes] = await Promise.all([
      getLibraryBlobUrl('lib-sqljs', 'text/javascript'),
      global.RoomViewData.decodeEmbeddedScriptBytes('lib-sqljs-wasm'),
    ]);
    await loadClassicScript(sqlJsUrl);
    if (typeof global.initSqlJs !== 'function') {
      throw new Error('sql.js initSqlJs global not initialized');
    }
    return await global.initSqlJs({ wasmBinary: wasmBytes });
  }

  async function loadPdfJsLazy() {
    if (!pdfJsPromise) {
      pdfJsPromise = (async () => {
        const [pdfUrl, workerUrl] = await Promise.all([
          getLibraryBlobUrl('lib-pdfjs', 'text/javascript'),
          getLibraryBlobUrl('lib-pdfjs-worker', 'text/javascript'),
        ]);
        const pdfjsLib = await import(pdfUrl);
        pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;
        return pdfjsLib;
      })();
    }
    return await pdfJsPromise;
  }

  async function boot() {
    const bootOverlay = document.getElementById('boot-overlay');
    const bootStatus = document.getElementById('boot-status');
    const setStatus = (msg) => { if (bootStatus) bootStatus.textContent = msg; };

    try {
      setStatus('Decoding embedded Three.js & SQLite WASM…');
      const [threeUrl, SQL] = await Promise.all([
        getLibraryBlobUrl('lib-three', 'text/javascript'),
        initSqlModule(),
      ]);

      registerImportMap({ three: threeUrl });
      let THREE = null;
      try {
        THREE = await import(threeUrl);
      } catch (_) {
        THREE = null;
      }

      const liveConfigEl = document.getElementById('room-view-live-config');
      const dbEl = document.getElementById('room-view-db');
      let dataLayer = null;

      if (liveConfigEl) {
        setStatus('Connecting to localhost Room View…');
        const liveConfig = JSON.parse(liveConfigEl.textContent || '{}');
        const query = liveConfig.token ? `?token=${encodeURIComponent(liveConfig.token)}` : '';
        const headers = liveConfig.token ? { 'X-Josh-Room-View-Token': liveConfig.token } : {};
        const resp = await fetch(`/state.json${query}`, { cache: 'no-store', headers });
        if (!resp.ok) throw new Error(`Failed to fetch /state.json (HTTP ${resp.status})`);
        const payload = await resp.json();
        const liveDb = global.RoomViewData.hydrateLiveSqlite(SQL, payload.descriptor, payload.state, payload.sensitivity);
        dataLayer = new global.RoomViewData.RoomViewDataLayer({
          mode: 'live',
          sqlModule: SQL,
          db: liveDb,
          rawSqliteBytes: liveDb.export(),
          liveConfig,
          initialPayload: payload,
        });
      } else if (dbEl) {
        setStatus('Opening embedded room-view-v1 SQLite snapshot…');
        const dbBytes = await global.RoomViewData.decodeEmbeddedScriptBytes('room-view-db');
        const db = new SQL.Database(dbBytes);
        const payload = global.RoomViewData.readStateFromSqlite(db);
        dataLayer = new global.RoomViewData.RoomViewDataLayer({
          mode: 'offline',
          sqlModule: SQL,
          db,
          rawSqliteBytes: dbBytes,
          initialPayload: payload,
        });
      } else {
        throw new Error('No room-view-db snapshot or live configuration found in document');
      }

      setStatus('Rendering filesystem landscape…');
      global.__JOSH_ROOM_VIEW_APP__ = new global.RoomViewAppClass({
        THREE,
        dataLayer,
        loadPdfJs: loadPdfJsLazy,
      });

      if (bootOverlay) bootOverlay.hidden = true;
    } catch (err) {
      setStatus(`Room View failed to boot: ${err && err.message ? err.message : String(err)}`);
      global.__JOSH_ROOM_VIEW_BOOT_ERROR__ = err ? String(err) : 'unknown';
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})(window);
