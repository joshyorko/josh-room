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

  function computeNodePathsJs(nodes) {
    const byId = new Map();
    for (const n of nodes || []) byId.set(n.id, n);
    const cache = new Map();
    const resolve = (id) => {
      if (cache.has(id)) return cache.get(id);
      const n = byId.get(id);
      if (!n) return '/';
      let path = '/';
      if (n.parent_id != null && byId.has(n.parent_id)) {
        const pp = resolve(n.parent_id);
        const seg = String(n.label || '').replace(/^\/+|\/+$/g, '');
        path = pp === '/' ? `/${seg}` : `${pp}/${seg}`;
      }
      cache.set(id, path);
      return path;
    };
    for (const n of nodes || []) resolve(n.id);
    return cache;
  }

  function hydrateLiveSqlite(SQL, descriptor, state, sensitivity) {
    const db = new SQL.Database();
    db.run(`
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE groups(id TEXT PRIMARY KEY, name TEXT, ord INT);
      CREATE TABLE stages(id TEXT PRIMARY KEY, name TEXT, ord INT, terminal INT, failure INT);
      CREATE TABLE items(id TEXT PRIMARY KEY, group_id TEXT, stage_id TEXT, kind TEXT, label TEXT, metric REAL, updated_at TEXT, fields_json TEXT);
      CREATE TABLE nodes(id TEXT PRIMARY KEY, parent_id TEXT, node_kind TEXT, label TEXT, path TEXT, kind TEXT, metric REAL, updated_at TEXT, summary TEXT, ord INT, fields_json TEXT);
      CREATE TABLE previews(node_id TEXT, slot TEXT, media_type TEXT, title TEXT, status TEXT, body TEXT, reason TEXT, language TEXT, truncated INT, summary TEXT, PRIMARY KEY(node_id, slot));
      CREATE TABLE links(source_id TEXT, relation TEXT, target_id TEXT, label TEXT, PRIMARY KEY(source_id, relation, target_id));
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
      ['descriptor_json', JSON.stringify(descriptor || {})],
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
    const insertAtt = db.prepare('INSERT OR IGNORE INTO attachments(item_id, name, media_type, size, sha256, bytes) VALUES (?, ?, ?, ?, ?, NULL)');
    const insertPrev = db.prepare('INSERT OR REPLACE INTO previews(node_id, slot, media_type, title, status, body, reason, language, truncated, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)');
    const insertLink = db.prepare('INSERT OR IGNORE INTO links(source_id, relation, target_id, label) VALUES (?, ?, ?, ?)');

    for (const item of (state.items || [])) {
      insertItem.run([item.id, item.group, item.stage, item.kind, item.label, item.metric != null ? item.metric : null, item.updated_at, JSON.stringify(item.fields || {})]);
      for (const att of (item.attachments || [])) {
        insertAtt.run([item.id, att.name, att.media_type, att.size, att.digest]);
      }
      for (const prev of (item.previews || [])) {
        insertPrev.run([
          item.id, prev.slot, prev.media_type, prev.title, prev.status || 'available',
          prev.body != null ? prev.body : null, prev.reason || null, prev.language || null,
          prev.truncated ? 1 : 0, prev.summary || null,
        ]);
      }
      for (const lk of (item.links || [])) {
        insertLink.run([item.id, lk.relation, lk.target_id, lk.label || '']);
      }
    }
    insertItem.free();

    const nodes = state.nodes || [];
    if (nodes.length) {
      const pathMap = computeNodePathsJs(nodes);
      const insertNode = db.prepare('INSERT INTO nodes(id, parent_id, node_kind, label, path, kind, metric, updated_at, summary, ord, fields_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)');
      nodes.forEach((n, idx) => {
        insertNode.run([
          n.id, n.parent_id != null ? n.parent_id : null, n.node_kind, n.label,
          pathMap.get(n.id) || `/${n.label}`, n.kind || null,
          n.metric != null ? n.metric : null, n.updated_at,
          n.summary || null, n.order != null ? n.order : idx,
          JSON.stringify(n.fields || {}),
        ]);
        for (const att of (n.attachments || [])) {
          insertAtt.run([n.id, att.name, att.media_type, att.size, att.digest]);
        }
        for (const prev of (n.previews || [])) {
          insertPrev.run([
            n.id, prev.slot, prev.media_type, prev.title, prev.status || 'available',
            prev.body != null ? prev.body : null, prev.reason || null, prev.language || null,
            prev.truncated ? 1 : 0, prev.summary || null,
          ]);
        }
        for (const lk of (n.links || [])) {
          insertLink.run([n.id, lk.relation, lk.target_id, lk.label || '']);
        }
      });
      insertNode.free();
    }
    insertAtt.free();
    insertPrev.free();
    insertLink.free();

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
    const tables = new Set();
    const tRes = db.exec("SELECT name FROM sqlite_master WHERE type='table'");
    if (tRes[0]) {
      for (const [name] of tRes[0].values) tables.add(name);
    }

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
    const prevByNode = new Map();
    if (tables.has('previews')) {
      const pRes = db.exec('SELECT node_id, slot, media_type, title, status, body, reason, language, truncated, summary FROM previews ORDER BY node_id, slot');
      if (pRes[0]) {
        for (const [node_id, slot, media_type, title, status, body, reason, language, truncated, summary] of pRes[0].values) {
          if (!prevByNode.has(node_id)) prevByNode.set(node_id, []);
          prevByNode.get(node_id).push({
            slot,
            media_type,
            title,
            status: status || 'available',
            body: body === null ? null : String(body),
            reason: reason || null,
            language: language || null,
            truncated: Boolean(truncated),
            summary: summary || null,
          });
        }
      }
    }
    const linksBySource = new Map();
    if (tables.has('links')) {
      const lRes = db.exec('SELECT source_id, relation, target_id, label FROM links ORDER BY source_id, relation, target_id');
      if (lRes[0]) {
        for (const [source_id, relation, target_id, label] of lRes[0].values) {
          if (!linksBySource.has(source_id)) linksBySource.set(source_id, []);
          linksBySource.get(source_id).push({ relation, target_id, label: label || '' });
        }
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
          previews: prevByNode.get(id) || [],
          links: linksBySource.get(id) || [],
        });
      }
    }

    const nodes = [];
    if (tables.has('nodes')) {
      const nRes = db.exec('SELECT id, parent_id, node_kind, label, path, kind, metric, updated_at, summary, ord, fields_json FROM nodes ORDER BY ord ASC, id ASC');
      if (nRes[0]) {
        for (const [id, parent_id, node_kind, label, path, kind, metric, updated_at, summary, ord, fields_json] of nRes[0].values) {
          let fields = {};
          try { fields = JSON.parse(fields_json || '{}'); } catch (_) { fields = {}; }
          nodes.push({
            id,
            parent_id: parent_id === null ? null : String(parent_id),
            node_kind,
            label,
            path,
            kind: kind || null,
            metric: metric === null ? null : Number(metric),
            updated_at,
            summary: summary || null,
            order: ord === null ? null : Number(ord),
            fields,
            attachments: attByItem.get(id) || [],
            previews: prevByNode.get(id) || [],
            links: linksBySource.get(id) || [],
          });
        }
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
        nodes,
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
      this.filesystem = mode === 'live' && initialPayload.server?.filesystem === true;
      this.fsGeneration = null;
      this._blobCache = new Map();
      this._indexAttachments();
      this._refreshSequence = 0;
      this._appliedRefresh = 0;
    }

    _indexAttachments() {
      const next = new Map();
      for (const owner of [...(this.state.items || []), ...(this.state.nodes || [])]) {
        for (const ref of owner.attachments || []) {
          const key = JSON.stringify([owner.id, ref.name]);
          const cached = this._blobCache.get(key);
          next.set(key, cached && cached.digest === ref.digest && cached.mediaType === ref.media_type
            ? cached
            : { digest: ref.digest, mediaType: ref.media_type });
        }
      }
      for (const [key, cached] of this._blobCache) {
        if (cached.entry && next.get(key) !== cached) URL.revokeObjectURL(cached.entry.url);
      }
      this._blobCache = next;
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

    async queryFilesystem(operation, params, signal) {
      if (!this.filesystem) throw new Error('Live filesystem unavailable');
      const resp = await fetch(`/fs/${operation}?${new URLSearchParams(params)}`, {
        cache: 'no-store', headers: this._liveHeaders(), signal,
      });
      if (!resp.ok) {
        const error = new Error(`Filesystem request failed (HTTP ${resp.status})`);
        error.status = resp.status;
        throw error;
      }
      return resp.json();
    }

    applyFilesystemNodes(nodes, generation) {
      this.fsGeneration = generation;
      this.state = { ...this.state, nodes, items: [], groups: [] };
      this._indexAttachments();
    }

    async refreshLive() {
      if (this.mode !== 'live') return false;
      const sequence = ++this._refreshSequence;
      const resp = await fetch(`/state.json${this._liveQuerySuffix()}`, {
        cache: 'no-store',
        headers: this._liveHeaders(),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload = await resp.json();
      if (sequence < this._appliedRefresh) return false;
      if (this.SQL) {
        const db = hydrateLiveSqlite(this.SQL, payload.descriptor, payload.state, payload.sensitivity);
        try {
          this.rawSqliteBytes = db.export();
        } catch (err) {
          db.close();
          throw err;
        }
        if (this.db) this.db.close();
        this.db = db;
      }
      this._appliedRefresh = sequence;
      this.descriptor = payload.descriptor;
      this.state = payload.state;
      this.sensitivity = payload.sensitivity;
      this._indexAttachments();
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

    async fetchPreviewSlot(objectId, slot) {
      if (this.mode === 'live' && !this.filesystem) {
        const url = `/content/${encodeURIComponent(objectId)}/${encodeURIComponent(slot)}${this._liveQuerySuffix()}`;
        const resp = await fetch(url, { cache: 'no-store', headers: this._liveHeaders() });
        if (!resp.ok) return null;
        const payload = await resp.json();
        return payload && payload.preview ? payload.preview : null;
      }
      const allObjs = [...(this.state.nodes || []), ...(this.state.items || [])];
      const match = allObjs.find((o) => o.id === objectId);
      if (!match || !match.previews) return null;
      return match.previews.find((p) => p.slot === slot) || null;
    }

    async loadAttachmentBytes(itemId, name) {
      if (this.mode === 'live') {
        const ref = this._blobCache.get(JSON.stringify([itemId, name]));
        if (this.filesystem && !ref) return null;
        const url = this.filesystem
          ? `/fs/attachment?${new URLSearchParams({ id: itemId, name, generation: this.fsGeneration, digest: ref.digest })}`
          : `/attachments/${encodeURIComponent(itemId)}/${encodeURIComponent(name)}${this._liveQuerySuffix()}`;
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
      const key = JSON.stringify([itemId, name]);
      const cached = this._blobCache.get(key);
      if (!cached) return null;
      if (cached.entry) return cached.entry;
      if (cached.pending) return cached.pending;
      cached.pending = this.loadAttachmentBytes(itemId, name).then(async data => {
        if (!data) return null;
        if (this.mode === 'live') {
          // The name-addressed route may already serve a newer version than state.json.
          const hash = await crypto.subtle.digest('SHA-256', data.bytes);
          const hex = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
          if (hex !== cached.digest) return null;
        }
        if (this._blobCache.get(key) !== cached) return null;
        const blob = new Blob([data.bytes], { type: data.mediaType });
        const url = URL.createObjectURL(blob);
        cached.entry = { url, mediaType: data.mediaType, size: data.bytes.byteLength };
        return cached.entry;
      });
      try {
        return await cached.pending;
      } finally {
        cached.pending = null;
      }
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
