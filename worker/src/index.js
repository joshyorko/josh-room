const enc = new TextEncoder();
const b64 = bytes => btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
const random = () => b64(crypto.getRandomValues(new Uint8Array(32)));
const same = (left, right) => {
  const a = enc.encode(left || "");
  const b = enc.encode(right || "");
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let index = 0; index < a.length; index++) difference |= a[index] ^ b[index];
  return difference === 0;
};

const durableId = value => {
  const id = String(value || "").split(".", 1)[0];
  return /^[0-9a-f]{64}$/.test(id) ? id : null;
};

const durableStub = (env, id) => {
  try {
    return env.OAUTH_SESSION.get(env.OAUTH_SESSION.idFromString(id));
  } catch (_error) {
    return null;
  }
};
const jsonError = (error, status) => Response.json({ error }, { status });
const privateJson = (value, init = {}) => Response.json(value, {
  ...init,
  headers: { ...Object.fromEntries(new Headers(init.headers || {}).entries()), "cache-control": "no-store" },
});
const roomStoreDomainId = async (accountId, bucket) => {
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(`josh-room:r2-room-store:v1\0${accountId}\0${bucket}`)));
  return [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
};
const roomStoreStub = (env, accountId, bucket) => {
  try {
    const name = `josh-room:r2-room-store:v1:${accountId}:${bucket}`;
    return env.OAUTH_SESSION.get(env.OAUTH_SESSION.idFromName(name));
  } catch (_error) {
    return null;
  }
};
const validRoomStoreRepositoryId = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const roomStoreMaterialFields = ["ciphertext", "domainId", "format", "keysetGeneration", "version"];
const validateRoomStoreMaterial = async (body, accountId, bucket) => {
  if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).sort().join(",") !== roomStoreMaterialFields.join(",")) return null;
  const domainId = await roomStoreDomainId(accountId, bucket);
  if (body.format !== "josh-room-r2-room-store-material" || body.version !== 1 || body.domainId !== domainId) return null;
  if (!Number.isSafeInteger(body.keysetGeneration) || body.keysetGeneration < 1 || body.keysetGeneration > 2147483647) return null;
  if (typeof body.ciphertext !== "string" || body.ciphertext.length < 1 || body.ciphertext.length > 16384 || !/^[A-Za-z0-9_-]+$/.test(body.ciphertext)) return null;
  return {
    format: "josh-room-r2-room-store-material",
    version: 1,
    domainId,
    keysetGeneration: body.keysetGeneration,
    ciphertext: body.ciphertext,
    repositoryId: null,
  };
};
const sameRoomStoreMaterial = (left, right) => left && right
  && left.format === right.format
  && left.version === right.version
  && left.domainId === right.domainId
  && left.keysetGeneration === right.keysetGeneration
  && left.ciphertext === right.ciphertext;
const sameRoomStoreCiphertext = (left, right) => left && right
  && left.format === right.format
  && left.version === right.version
  && left.domainId === right.domainId
  && left.ciphertext === right.ciphertext;
const roomStoreBearer = request => {
  const authorization = request.headers.get("authorization") || "";
  const match = authorization.match(/^Bearer ([A-Za-z0-9_-]{43})$/);
  return match ? match[1] : null;
};
const roomStoreSessionRequest = async (request, env, sessionId, operation) => {
  const stub = durableStub(env, sessionId);
  if (!stub) return jsonError("room_store_capability_expired", 404);
  const headers = new Headers(request.headers);
  const body = request.method === "POST" ? await request.text() : undefined;
  return stub.fetch(new Request(`https://session/room-store/${operation}`, {
    method: request.method,
    headers,
    ...(body === undefined ? {} : { body }),
  }));
};
const authPurpose = async request => {
  if (!request.headers.get("content-type")?.includes("application/json")) return "r2";
  try {
    const body = await request.json();
    const purpose = body?.purpose || "r2";
    return purpose === "encryption" || purpose === "r2" ? purpose : null;
  } catch (_error) {
    return null;
  }
};
const authCapabilities = purpose => purpose === "encryption" ? ["encryption"] : ["encryption", "r2"];
const authUrl = (env, state, challenge, purpose) => {
  const auth = new URL("https://dash.cloudflare.com/oauth2/auth");
  const values = {
    response_type: "code",
    client_id: env.OAUTH_CLIENT_ID,
    redirect_uri: env.OAUTH_REDIRECT_URI,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  };
  if (purpose === "r2") values.scope = "workers-r2.read workers-r2.write";
  for (const [k, v] of Object.entries(values)) auth.searchParams.set(k, v);
  auth.searchParams.set("josh_room_purpose", purpose);
  return auth;
};

async function durableRouter(request, env) {
  const url = new URL(request.url);
  const roomStoreRoute = url.pathname.match(/^\/session\/([0-9a-f]{64})\/room-store\/(material|repository)$/);
  if (roomStoreRoute) {
    if (!["GET", "POST"].includes(request.method)) return jsonError("room_store_method_not_allowed", 405);
    return roomStoreSessionRequest(request, env, roomStoreRoute[1], `${roomStoreRoute[2]}${request.method === "POST" ? "-write" : "-read"}`);
  }
  if (url.pathname === "/session/start" && request.method === "POST") {
    const purpose = await authPurpose(request);
    if (!purpose) return Response.json({ error: "invalid authorization purpose" }, { status: 400 });
    const id = env.OAUTH_SESSION.newUniqueId();
    const sessionId = id.toString();
    const nonce = random();
    const verifier = random();
    const challenge = b64(new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(verifier))));
    await env.OAUTH_SESSION.get(id).fetch(new Request("https://session/start", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ nonce, verifier, purpose }),
    }));
    const state = `${sessionId}.${nonce}`;
    const auth = authUrl(env, state, challenge, purpose);
    return Response.json({ sessionId, authorizationUrl: auth.toString(), expiresIn: 600, purpose });
  }
  const cancel = url.pathname.match(/^\/session\/([^/]+)\/cancel$/);
  if (cancel && request.method === "POST") {
    const stub = durableStub(env, cancel[1]);
    return stub ? stub.fetch(new Request("https://session/cancel", { method: "POST" })) : Response.json({ status: "expired" }, { status: 404 });
  }
  if (url.pathname === "/oauth/callback") {
    const state = url.searchParams.get("state");
    const id = durableId(state);
    const code = url.searchParams.get("code");
    const stub = id && code ? durableStub(env, id) : null;
    if (!stub) return new Response("Invalid or expired login.", { status: 400 });
    const callback = new URL("https://session/callback");
    callback.searchParams.set("state", state);
    callback.searchParams.set("code", code);
    return stub.fetch(new Request(callback));
  }
  const status = url.pathname.match(/^\/session\/([^/]+)$/);
  if (status && request.method === "GET") {
    const stub = durableStub(env, status[1]);
    return stub ? stub.fetch(new Request("https://session/status")) : Response.json({ status: "expired" }, { status: 404 });
  }
  return new Response("Not found", { status: 404 });
}

export class OAuthSession {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/start" && request.method === "POST") {
      const { nonce, verifier, purpose = "r2" } = await request.json();
      if (purpose !== "encryption" && purpose !== "r2") {
        return Response.json({ error: "invalid authorization purpose" }, { status: 400 });
      }
      const expiresAt = Date.now() + 600_000;
      await this.state.storage.put("session", {
        status: "pending",
        nonce,
        verifier,
        ...(purpose === "encryption" ? { purpose } : {}),
        expiresAt,
      });
      await this.state.storage.setAlarm(expiresAt);
      return Response.json({ status: "pending" });
    }
    if (url.pathname.startsWith("/bucket-room-store/")) {
      return this.fetchBucketRoomStore(request, url.pathname.slice("/bucket-room-store/".length));
    }
    const session = await this.state.storage.get("session");
    if (!session || session.expiresAt <= Date.now()) {
      await this.state.storage.deleteAll();
      if (url.pathname.startsWith("/room-store/")) return jsonError("room_store_capability_expired", 404);
      return Response.json({ status: "expired" }, { status: 404 });
    }
    if (url.pathname === "/cancel" && request.method === "POST") {
      if (session.status === "authorized") return Response.json({ status: "authorized" }, { status: 409 });
      if (session.status === "canceled") return Response.json({ status: "canceled" });
      if (session.status !== "pending") return Response.json({ status: session.status || "expired" }, { status: 409 });
      await this.state.storage.put("session", { status: "canceled", expiresAt: Date.now() + 120_000 });
      await this.state.storage.setAlarm(Date.now() + 120_000);
      return Response.json({ status: "canceled" });
    }
    if (url.pathname === "/status" && request.method === "GET") {
      if (session.purpose === "r2") {
        const capability = random();
        const expiresAt = Date.now() + 600_000;
        const capabilityHash = b64(new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(capability))));
        const result = await this.state.storage.transaction(async transaction => {
          const current = await transaction.get("session");
          if (!current || current.status !== "authorized" || current.purpose !== "r2") return { response: { status: current?.status || "expired" } };
          await transaction.put("session", {
            status: "consumed",
            purpose: "r2",
            accountId: this.env.CLOUDFLARE_ACCOUNT_ID,
            bucket: this.env.R2_BUCKET,
            capabilityHash,
            expiresAt,
          });
          return { response: current, capability };
        });
        if (!result.capability) return Response.json(result.response);
        await this.state.storage.setAlarm(expiresAt);
        const domainId = await roomStoreDomainId(this.env.CLOUDFLARE_ACCOUNT_ID, this.env.R2_BUCKET);
        return privateJson({ ...result.response, roomStoreDomainId: domainId, roomStoreCapability: result.capability, roomStoreCapabilityExpiresIn: 600 });
      }
      if (session.status !== "authorized") return Response.json({ status: session.status || "expired" });
      await this.state.storage.deleteAll();
      return Response.json(session);
    }
    const roomStoreMatch = url.pathname.match(/^\/room-store\/(material|repository)-(read|write)$/);
    if (roomStoreMatch) return this.handleRoomStoreSession(request, session, roomStoreMatch[1], roomStoreMatch[2]);
    if (url.pathname === "/callback" && request.method === "GET") {
      const state = url.searchParams.get("state");
      const nonce = String(state || "").split(".").slice(1).join(".");
      const code = url.searchParams.get("code");
      if (session.status !== "pending" || !code || !same(nonce, session.nonce)) {
        return new Response("Invalid or expired login.", { status: 400 });
      }
      const body = new URLSearchParams({ grant_type: "authorization_code", client_id: this.env.OAUTH_CLIENT_ID, code, redirect_uri: this.env.OAUTH_REDIRECT_URI, code_verifier: session.verifier });
      const token = await fetch("https://dash.cloudflare.com/oauth2/token", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body });
      const result = await token.json();
      if (!token.ok || !result.access_token) return new Response("Cloudflare authorization failed.", { status: 502 });
      const identityResponse = await fetch("https://dash.cloudflare.com/oauth2/userinfo", { headers: { authorization: `Bearer ${result.access_token}` } });
      const identity = await identityResponse.json();
      const subject = identity.sub || identity.id;
      if (!identityResponse.ok || !same(subject, this.env.OWNER_CLOUDFLARE_USER_ID)) {
        await this.state.storage.put("session", { status: "denied", expiresAt: Date.now() + 120_000 });
        return new Response("This Cloudflare identity is not authorized for Josh Room.", { status: 403 });
      }
      const current = await this.state.storage.get("session");
      if (!current || current.status !== "pending") return new Response("Invalid or expired login.", { status: 400 });
      const purpose = current.purpose || "r2";
      let temporaryResult;
      if (purpose === "r2") {
        const temporary = await fetch(`https://api.cloudflare.com/client/v4/accounts/${this.env.CLOUDFLARE_ACCOUNT_ID}/r2/temp-access-credentials`, {
          method: "POST",
          headers: { authorization: `Bearer ${result.access_token}`, "content-type": "application/json" },
          body: JSON.stringify({ bucket: this.env.R2_BUCKET, permission: "object-read-write", ttlSeconds: 21600, parentAccessKeyId: this.env.R2_PARENT_ACCESS_KEY_ID })
        });
        temporaryResult = await temporary.json();
        if (!temporary.ok || !temporaryResult.success) return new Response("Temporary R2 authorization failed.", { status: 502 });
      }
      await this.state.storage.put("session", {
        status: "authorized",
        purpose,
        capabilities: authCapabilities(purpose),
        ...(purpose === "r2" ? {
          accessKeyId: temporaryResult.result.accessKeyId,
          secretAccessKey: temporaryResult.result.secretAccessKey,
          sessionToken: temporaryResult.result.sessionToken,
          endpoint: `https://${this.env.CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com`,
          bucket: this.env.R2_BUCKET,
          expiresIn: 21600,
        } : { expiresIn: 600 }),
        ageIdentity: this.env.OPERATIONAL_AGE_IDENTITY,
        ageRecipients: JSON.parse(this.env.AGE_RECIPIENTS),
        expiresAt: Date.now() + 600_000,
      });
      return new Response("Josh Room authorized. Return to VS Code.");
    }
    return new Response("Not found", { status: 404 });
  }

  async handleRoomStoreSession(request, session, resource, action) {
    if (request.method !== (action === "read" ? "GET" : "POST")) return jsonError("room_store_method_not_allowed", 405);
    if (!session || session.status !== "consumed" || session.purpose !== "r2" || session.expiresAt <= Date.now()) {
      return jsonError("room_store_capability_expired", 404);
    }
    if (session.accountId !== this.env.CLOUDFLARE_ACCOUNT_ID || session.bucket !== this.env.R2_BUCKET) {
      return jsonError("room_store_scope_mismatch", 403);
    }
    const capability = roomStoreBearer(request);
    if (!capability) return jsonError("room_store_capability_required", 401);
    const capabilityHash = b64(new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(capability))));
    if (!same(capabilityHash, session.capabilityHash)) return jsonError("room_store_capability_invalid", 403);
    const stub = roomStoreStub(this.env, session.accountId, session.bucket);
    if (!stub) return jsonError("room_store_durable_authority_unavailable", 503);
    const body = action === "write" ? await request.text() : undefined;
    return stub.fetch(new Request(`https://bucket/bucket-room-store/${resource}-${action}`, {
      method: request.method,
      headers: { "content-type": request.headers.get("content-type") || "" },
      ...(body === undefined ? {} : { body }),
    }));
  }

  async fetchBucketRoomStore(request, operation) {
    const key = "r2-room-store-material-v1";
    const storage = this.state.storage;
    if (operation === "material-read" && request.method === "GET") {
      const material = await storage.get(key);
      return material ? privateJson({ status: "ready", material }) : jsonError("room_store_material_missing", 404);
    }
    if (operation === "material-write" && request.method === "POST") {
      if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return jsonError("room_store_invalid_material", 400);
      let submitted;
      try {
        submitted = await validateRoomStoreMaterial(await request.json(), this.env.CLOUDFLARE_ACCOUNT_ID, this.env.R2_BUCKET);
      } catch (_error) {
        submitted = null;
      }
      if (!submitted) return jsonError("room_store_invalid_material", 400);
      try {
        return await storage.transaction(async transaction => {
          const current = await transaction.get(key);
          if (current) {
            if (!sameRoomStoreMaterial(current, submitted)) return jsonError("room_store_material_conflict", 409);
            return privateJson({ status: "existing", material: current });
          }
          await transaction.put(key, submitted);
          const winner = await transaction.get(key);
          if (!sameRoomStoreMaterial(winner, submitted) || winner.repositoryId !== null) throw new Error("room store material readback mismatch");
          return privateJson({ status: "created", material: winner }, { status: 201 });
        });
      } catch (_error) {
        return jsonError("room_store_material_write_unverified", 503);
      }
    }
    if (operation === "repository-write" && request.method === "POST") {
      if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return jsonError("room_store_invalid_repository", 400);
      let body;
      try {
        body = await request.json();
      } catch (_error) {
        return jsonError("room_store_invalid_repository", 400);
      }
      if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).sort().join(",") !== "expectedGeneration,repositoryId" || !validRoomStoreRepositoryId(body.repositoryId) || !Number.isSafeInteger(body.expectedGeneration) || body.expectedGeneration < 1 || body.expectedGeneration > 2147483646) return jsonError("room_store_invalid_repository", 400);
      try {
        return await storage.transaction(async transaction => {
          const current = await transaction.get(key);
          if (!current) return jsonError("room_store_material_missing", 404);
          if (current.repositoryId !== null && current.repositoryId !== body.repositoryId) return jsonError("room_store_repository_conflict", 409);
          if (current.repositoryId === body.repositoryId) {
            if (current.keysetGeneration !== body.expectedGeneration && current.keysetGeneration !== body.expectedGeneration + 1) return jsonError("room_store_generation_conflict", 409);
            return privateJson({ status: "bound", repositoryId: current.repositoryId, keysetGeneration: current.keysetGeneration });
          }
          if (current.keysetGeneration !== body.expectedGeneration) return jsonError("room_store_generation_conflict", 409);
          const candidate = { ...current, repositoryId: body.repositoryId, keysetGeneration: body.expectedGeneration + 1 };
          await transaction.put(key, candidate);
          const winner = await transaction.get(key);
          if (!winner || !sameRoomStoreCiphertext(winner, current) || winner.repositoryId !== body.repositoryId || winner.keysetGeneration !== body.expectedGeneration + 1) throw new Error("room store repository readback mismatch");
          return privateJson({ status: "bound", repositoryId: winner.repositoryId, keysetGeneration: winner.keysetGeneration }, { status: 201 });
        });
      } catch (_error) {
        return jsonError("room_store_repository_write_unverified", 503);
      }
    }
    return jsonError("room_store_not_found", 404);
  }

  async alarm() {
    await this.state.storage.deleteAll();
  }
}

export default {
  async fetch(request, env) {
    if (env.OAUTH_SESSION) return durableRouter(request, env);
    const url = new URL(request.url);
    if (url.pathname === "/session/start" && request.method === "POST") {
      const purpose = await authPurpose(request);
      if (!purpose) return Response.json({ error: "invalid authorization purpose" }, { status: 400 });
      const id = crypto.randomUUID();
      const state = random();
      const verifier = random();
      const challenge = b64(new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(verifier))));
      await env.OAUTH_SESSIONS.put(`state:${state}`, JSON.stringify({ id, verifier }), { expirationTtl: 600 });
      await env.OAUTH_SESSIONS.put(`session:${id}`, JSON.stringify({
        status: "pending",
        state,
        ...(purpose === "encryption" ? { purpose } : {}),
      }), { expirationTtl: 600 });
      const auth = authUrl(env, state, challenge, purpose);
      return Response.json({ sessionId: id, authorizationUrl: auth.toString(), expiresIn: 600, purpose });
    }
    const cancelMatch = url.pathname.match(/^\/session\/([^/]+)\/cancel$/);
    if (cancelMatch && request.method === "POST") {
      const key = `session:${cancelMatch[1]}`;
      const session = await env.OAUTH_SESSIONS.get(key, "json");
      if (!session) return Response.json({ status: "expired" }, { status: 404 });
      if (session.status === "authorized") return Response.json({ status: "authorized" }, { status: 409 });
      if (session.status === "canceled") return Response.json({ status: "canceled" });
      if (session.status !== "pending") return Response.json({ status: session.status || "expired" }, { status: 409 });
      await env.OAUTH_SESSIONS.put(key, JSON.stringify({ status: "canceled" }), { expirationTtl: 120 });
      if (typeof session.state === "string" && session.state) await env.OAUTH_SESSIONS.delete(`state:${session.state}`);
      return Response.json({ status: "canceled" });
    }
    if (url.pathname === "/oauth/callback") {
      const state = url.searchParams.get("state");
      const saved = await env.OAUTH_SESSIONS.get(`state:${state}`, "json");
      if (!saved || !url.searchParams.get("code")) return new Response("Invalid or expired login.", { status: 400 });
      const sessionKey = `session:${saved.id}`;
      const session = await env.OAUTH_SESSIONS.get(sessionKey, "json");
      if (!session || session.status !== "pending") return new Response("Invalid or expired login.", { status: 400 });
      await env.OAUTH_SESSIONS.delete(`state:${state}`);
      const body = new URLSearchParams({ grant_type: "authorization_code", client_id: env.OAUTH_CLIENT_ID, code: url.searchParams.get("code"), redirect_uri: env.OAUTH_REDIRECT_URI, code_verifier: saved.verifier });
      const token = await fetch("https://dash.cloudflare.com/oauth2/token", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body });
      const result = await token.json();
      if (!token.ok || !result.access_token) return new Response("Cloudflare authorization failed.", { status: 502 });
      const identityResponse = await fetch("https://dash.cloudflare.com/oauth2/userinfo", {
        headers: { authorization: `Bearer ${result.access_token}` }
      });
      const identity = await identityResponse.json();
      const subject = identity.sub || identity.id;
      if (!identityResponse.ok || !same(subject, env.OWNER_CLOUDFLARE_USER_ID)) {
        await env.OAUTH_SESSIONS.put(`session:${saved.id}`, JSON.stringify({ status: "denied" }), { expirationTtl: 120 });
        return new Response("This Cloudflare identity is not authorized for Josh Room.", { status: 403 });
      }
      const current = await env.OAUTH_SESSIONS.get(sessionKey, "json");
      if (!current || current.status !== "pending") return new Response("Invalid or expired login.", { status: 400 });
      const purpose = current.purpose || "r2";
      let temporaryResult;
      if (purpose === "r2") {
        const temporary = await fetch(`https://api.cloudflare.com/client/v4/accounts/${env.CLOUDFLARE_ACCOUNT_ID}/r2/temp-access-credentials`, {
          method: "POST",
          headers: { authorization: `Bearer ${result.access_token}`, "content-type": "application/json" },
          body: JSON.stringify({ bucket: env.R2_BUCKET, permission: "object-read-write", ttlSeconds: 21600, parentAccessKeyId: env.R2_PARENT_ACCESS_KEY_ID })
        });
        temporaryResult = await temporary.json();
        if (!temporary.ok || !temporaryResult.success) return new Response("Temporary R2 authorization failed.", { status: 502 });
      }
      await env.OAUTH_SESSIONS.put(sessionKey, JSON.stringify({
        status: "authorized",
        purpose,
        capabilities: authCapabilities(purpose),
        ...(purpose === "r2" ? {
          accessKeyId: temporaryResult.result.accessKeyId,
          secretAccessKey: temporaryResult.result.secretAccessKey,
          sessionToken: temporaryResult.result.sessionToken,
          endpoint: `https://${env.CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com`,
          bucket: env.R2_BUCKET,
          expiresIn: 21600,
        } : { expiresIn: 600 }),
        ageIdentity: env.OPERATIONAL_AGE_IDENTITY,
        ageRecipients: JSON.parse(env.AGE_RECIPIENTS),
      }), { expirationTtl: 600 });
      return new Response("Josh Room authorized. Return to VS Code.");
    }
    if (/^\/session\/[^/]+\/room-store\/(material|repository)$/.test(url.pathname)) {
      if (!["GET", "POST"].includes(request.method)) return jsonError("room_store_method_not_allowed", 405);
      return jsonError("room_store_durable_authority_unavailable", 503);
    }
    if (url.pathname.startsWith("/session/") && request.method === "GET") {
      const key = `session:${url.pathname.slice(9)}`;
      const session = await env.OAUTH_SESSIONS.get(key, "json");
      if (!session) return Response.json({ status: "expired" }, { status: 404 });
      if (session.status === "authorized") await env.OAUTH_SESSIONS.delete(key);
      return Response.json(session.status === "authorized" ? session : { status: session.status || "expired" });
    }
    return new Response("Not found", { status: 404 });
  }
};
