// Shared data access layer for both Offline SQLite (sql.js) and Live HTTP (/state.json) modes.
(function (global) {
  'use strict';

  const READONLY_START_RE = /^\s*(?:SELECT|WITH|EXPLAIN|PRAGMA\s+(?:table_info|table_list|index_list))\b/i;
  const MUTATION_SQL_RE = /\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|VACUUM|REINDEX)\b/i;

  function decodeBase64ToBytes(b64) {
    const clean = b64.replace(/\s+/g, '');
    if (typeof Uint8Array.fromBase64 === 'function') {
      return Uint8Array.fromBase64(clean);
    }
    const chunkSize = 65536;
    const chunks = [];
    let total = 0;
    for (let i = 0; i < clean.length; i += chunkSize) {
      const bin = atob(clean.slice(i, i + chunkSize));
      const arr = new Uint8Array(bin.length);
      for (let j = 0; j < bin.length; j += 1) {
        arr[j] = bin.charCodeAt(j);
      }
      chunks.push(arr);
      total += arr.length;
    }
    const out = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      out.set(chunk, offset);
      offset += chunk.length;
    }
    return out;
  }

  async function gunzipBytes(bytes) {
    if (typeof DecompressionStream !== 'function') {
      throw new Error('DecompressionStream is unavailable in this browser');
    }
    const ds = new DecompressionStream('gzip');
    const stream = new Blob([bytes]).stream().pipeThrough(ds);
    const reader = stream.getReader();
    const chunks = [];
    let total = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      chunks.push(value);
      total += value.byteLength;
    }
    const out = new Uint8Array(total);
    let offset = 0;
    for (const c of chunks) {
      out.set(c, offset);
      offset += c.byteLength;
    }
    return out;
  }

  async function decodeEmbeddedScriptBytes(elementId) {
    const el = document.getElementById(elementId);
    if (!el) return null;
    const raw = decodeBase64ToBytes(el.textContent || '');
    const enc = (el.getAttribute('data-encoding') || 'raw').toLowerCase();
    if (enc === 'gzip') {
      return await gunzipBytes(raw);
    }
    return raw;
  }

  function hydrateLiveSqlite(SQL, descriptor, state, sensitivity) {
    const db = new SQL.Database();
    db.run(`
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE groups(id TEXT PRIMARY KEY, name TEXT, ord INT);
      CREATE TABLE stages(id TEXT PRIMARY KEY, name TEXT, ord INT, terminal INT, failure INT);
      CREATE TABLE items(id TEXT PRIMARY KEY, group_id TEXT, stage_id TEXT, kind TEXT, label TEXT, metric REAL, updated_at TEXT, fields_json TEXT);
      CREATE TABLE steps(item_id TEXT, name TEXT, at TEXT);
      CREATE TABLE attachments(item_id TEXT, name TEXT, media_type TEXT, size INT, sha256 TEXT, bytes BLOB, PRIMARY KEY(item_id, name));
      CREATE TABLE attachment_blobs(sha256 TEXT PRIMARY KEY, bytes BLOB);
      CREATE TABLE summary(key TEXT PRIMARY KEY, value TEXT);
    `);
    const metaRows = [
      ['schema_version', 'room-view-v1'],
      ['taken_at', state.taken_at || ''],
      ['since', state.since || ''],
      ['title', descriptor.title || ''],
      ['sensitivity', (sensitivity && sensitivity.level) || descriptor.sensitivity || 'synthetic'],
      ['tool', descriptor.tool || 'josh-room'],
      ['tool_version', descriptor.tool_version || '0.1.0'],
    ];
    const insertMeta = db.prepare('INSERT INTO meta(key, value) VALUES (?, ?)');
    for (const row of metaRows) insertMeta.run(row);
    insertMeta.free();

    const insertGroup = db.prepare('INSERT INTO groups(id, name, ord) VALUES (?, ?, ?)');
    (state.groups || []).forEach((g, idx) => insertGroup.run([g.id, g.name, g.order != null ? g.order : idx]));
    insertGroup.free();

    const insertStage = db.prepare('INSERT INTO stages(id, name, ord, terminal, failure) VALUES (?, ?, ?, ?, ?)');
    (descriptor.stages || []).forEach((s) => insertStage.run([s.id, s.name, s.order, s.terminal ? 1 : 0, s.failure ? 1 : 0]));
    insertStage.free();

    const insertItem = db.prepare('INSERT INTO items(id, group_id, stage_id, kind, label, metric, updated_at, fields_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)');
    const insertAtt = db.prepare('INSERT INTO attachments(item_id, name, media_type, size, sha256, bytes) VALUES (?, ?, ?, ?, ?, NULL)');
    for (const item of (state.items || [])) {
      insertItem.run([item.id, item.group, item.stage, item.kind, item.label, item.metric != null ? item.metric : null, item.updated_at, JSON.stringify(item.fields || {})]);
      for (const att of (item.attachments || [])) {
        insertAtt.run([item.id, att.name, att.media_type, att.size, att.digest]);
      }
    }
    insertItem.free();
    insertAtt.free();

    const insertStep = db.prepare('INSERT INTO steps(item_id, name, at) VALUES (?, ?, ?)');
    for (const step of (state.steps || [])) {
      insertStep.run([step.item_id, step.name, step.at]);
    }
    insertStep.free();

    const insertSum = db.prepare('INSERT INTO summary(key, value) VALUES (?, ?)');
    for (const [k, v] of Object.entries(state.summary || {})) {
      insertSum.run([k, typeof v === 'string' ? v : JSON.stringify(v)]);
    }
    insertSum.free();
    return db;
  }

  function readStateFromSqlite(db) {
    const meta = {};
    const mRes = db.exec('SELECT key, value FROM meta');
    if (mRes[0]) {
      for (const [k, v] of mRes[0].values) meta[k] = v;
    }
    const descriptor = JSON.parse(meta.descriptor_json || '{}');
    const groups = [];
    const gRes = db.exec('SELECT id, name, ord FROM groups ORDER BY ord ASC, id ASC');
    if (gRes[0]) {
      for (const [id, name, ord] of gRes[0].values) groups.push({ id, name, order: ord });
    }
    const attByItem = new Map();
    const aRes = db.exec('SELECT item_id, name, media_type, size, sha256 FROM attachments ORDER BY item_id, name');
    if (aRes[0]) {
      for (const [item_id, name, media_type, size, sha256] of aRes[0].values) {
        if (!attByItem.has(item_id)) attByItem.set(item_id, []);
        attByItem.get(item_id).push({ name, media_type, size, digest: sha256 });
      }
    }
    const items = [];
    const iRes = db.exec('SELECT id, group_id, stage_id, kind, label, metric, updated_at, fields_json FROM items ORDER BY id ASC');
    if (iRes[0]) {
      for (const [id, group, stage, kind, label, metric, updated_at, fields_json] of iRes[0].values) {
        let fields = {};
        try { fields = JSON.parse(fields_json || '{}'); } catch (_) { fields = {}; }
        items.push({
          id,
          group,
          stage,
          kind,
          label,
          metric: metric === null ? null : Number(metric),
          updated_at,
          fields,
          attachments: attByItem.get(id) || [],
        });
      }
    }
    const steps = [];
    const sRes = db.exec('SELECT item_id, name, at FROM steps ORDER BY item_id ASC, at ASC');
    if (sRes[0]) {
      for (const [item_id, name, at] of sRes[0].values) steps.push({ item_id, name, at });
    }
    const summary = {};
    const sumRes = db.exec('SELECT key, value FROM summary');
    if (sumRes[0]) {
      for (const [k, v] of sumRes[0].values) {
        try { summary[k] = JSON.parse(v); } catch (_) { summary[k] = v; }
      }
    }
    let stale = null;
    if (meta.stale_json) {
      try { stale = JSON.parse(meta.stale_json); } catch (_) { stale = null; }
    }
    const level = meta.sensitivity || descriptor.sensitivity || 'synthetic';
    const sensitivity = {
      level,
      requires_acknowledgment: level !== 'public' && level !== 'synthetic',
      banner_text: meta.sensitivity_banner || `Contains ${level} data; keep on approved storage`,
    };
    return {
      meta,
      descriptor,
      state: {
        taken_at: meta.taken_at || '',
        since: meta.since || '',
        groups,
        items,
        steps,
        summary,
        stale,
      },
      sensitivity,
    };
  }

  class RoomViewDataLayer {
    constructor({ mode, sqlModule, db, rawSqliteBytes, liveConfig, initialPayload }) {
      this.mode = mode; // 'offline' | 'live'
      this.SQL = sqlModule;
      this.db = db;
      this.rawSqliteBytes = rawSqliteBytes;
      this.liveConfig = liveConfig || null;
      this.descriptor = initialPayload.descriptor;
      this.state = initialPayload.state;
      this.sensitivity = initialPayload.sensitivity;
      this.meta = initialPayload.meta || {};
      this._blobCache = new Map();
    }

    _liveHeaders() {
      const headers = {};
      if (this.liveConfig && this.liveConfig.token) {
        headers['X-Josh-Room-View-Token'] = this.liveConfig.token;
      }
      return headers;
    }

    _liveQuerySuffix(extra) {
      const params = new URLSearchParams(extra || {});
      if (this.liveConfig && this.liveConfig.token) {
        params.set('token', this.liveConfig.token);
      }
      const q = params.toString();
      return q ? `?${q}` : '';
    }

    async refreshLive() {
      if (this.mode !== 'live') return false;
      const resp = await fetch(`/state.json${this._liveQuerySuffix()}`, {
        cache: 'no-store',
        headers: this._liveHeaders(),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload = await resp.json();
      this.descriptor = payload.descriptor;
      this.state = payload.state;
      this.sensitivity = payload.sensitivity;
      if (this.SQL) {
        if (this.db) this.db.close();
        this.db = hydrateLiveSqlite(this.SQL, this.descriptor, this.state, this.sensitivity);
        this.rawSqliteBytes = this.db.export();
      }
      return true;
    }

    async fetchLiveExportPreview() {
      const resp = await fetch(`/export-preview.json${this._liveQuerySuffix()}`, {
        cache: 'no-store',
        headers: this._liveHeaders(),
      });
      if (!resp.ok) throw new Error(`Preview HTTP ${resp.status}`);
      return await resp.json();
    }

    liveExportUrl({ ack = false, noAttachments = false } = {}) {
      const params = { download: '1' };
      if (ack) params.ack = '1';
      if (noAttachments) params.no_attachments = '1';
      return `/export.html${this._liveQuerySuffix(params)}`;
    }

    async loadAttachmentBytes(itemId, name) {
      const key = `${itemId}/${name}`;
      if (this.mode === 'live') {
        const url = `/attachments/${encodeURIComponent(itemId)}/${encodeURIComponent(name)}${this._liveQuerySuffix()}`;
        const resp = await fetch(url, { cache: 'no-store', headers: this._liveHeaders() });
        if (!resp.ok) return null;
        const buf = await resp.arrayBuffer();
        return {
          bytes: new Uint8Array(buf),
          mediaType: resp.headers.get('Content-Type') || 'application/octet-stream',
        };
      }
      if (!this.db) return null;
      const stmt = this.db.prepare(
        `SELECT a.media_type, COALESCE(a.bytes, b.bytes) AS payload
         FROM attachments a
         LEFT JOIN attachment_blobs b ON b.sha256 = a.sha256
         WHERE a.item_id = ? AND a.name = ?`
      );
      try {
        stmt.bind([itemId, name]);
        if (!stmt.step()) return null;
        const row = stmt.get();
        if (!row[1]) return null;
        return {
          mediaType: row[0],
          bytes: row[1] instanceof Uint8Array ? row[1] : new Uint8Array(row[1]),
        };
      } finally {
        stmt.free();
      }
    }

    async getAttachmentBlobUrl(itemId, name) {
      const key = `${itemId}/${name}`;
      if (this._blobCache.has(key)) return this._blobCache.get(key);
      const data = await this.loadAttachmentBytes(itemId, name);
      if (!data) return null;
      const blob = new Blob([data.bytes], { type: data.mediaType });
      const url = URL.createObjectURL(blob);
      const entry = { url, mediaType: data.mediaType, size: data.bytes.byteLength };
      this._blobCache.set(key, entry);
      return entry;
    }

    runReadOnlySql(sqlText) {
      if (!this.db) throw new Error('SQLite database is unavailable');
      const trimmed = (sqlText || '').trim();
      if (!trimmed) throw new Error('Enter a SELECT query');
      if (!READONLY_START_RE.test(trimmed) || MUTATION_SQL_RE.test(trimmed)) {
        throw new Error('Only read-only SELECT / WITH / EXPLAIN queries are permitted');
      }
      const started = performance.now();
      const results = this.db.exec(trimmed);
      const elapsedMs = Math.round((performance.now() - started) * 10) / 10;
      return { results, elapsedMs };
    }

    getSqliteDownloadBlob() {
      const bytes = this.rawSqliteBytes || (this.db ? this.db.export() : null);
      if (!bytes) return null;
      return new Blob([bytes], { type: 'application/x-sqlite3' });
    }
  }

  global.RoomViewData = {
    decodeBase64ToBytes,
    gunzipBytes,
    decodeEmbeddedScriptBytes,
    hydrateLiveSqlite,
    readStateFromSqlite,
    RoomViewDataLayer,
  };
})(window);
