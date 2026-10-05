import assert from "node:assert/strict";
import test from "node:test";

import worker, { OAuthSession } from "../src/index.js";

class MemoryKV {
  constructor() {
    this.values = new Map();
    this.puts = [];
    this.deletes = [];
  }

  async get(key, type) {
    const value = this.values.get(key);
    if (value === undefined) return null;
    return type === "json" ? JSON.parse(value) : value;
  }

  async put(key, value, options) {
    this.values.set(key, value);
    this.puts.push({ key, value: JSON.parse(value), options });
  }

  async delete(key) {
    this.values.delete(key);
    this.deletes.push(key);
  }
}

class MemoryStorage {
  constructor() {
    this.values = new Map();
    this.transactionQueue = Promise.resolve();
  }

  async get(key) { return this.values.get(key); }
  async put(key, value) { this.values.set(key, value); }
  async deleteAll() { this.values.clear(); }
  async setAlarm() {}
  transaction(callback) {
    const result = this.transactionQueue.then(() => callback({
      get: key => this.get(key),
      put: (key, value) => this.put(key, value),
    }));
    this.transactionQueue = result.then(() => undefined, () => undefined);
    return result;
  }
}

class MemoryDurableNamespace {
  constructor(env) {
    this.env = env;
    this.instances = new Map();
    this.names = new Map();
    this.next = 0;
  }

  newUniqueId() {
    this.next += 1;
    return { toString: () => this.next.toString(16).padStart(64, "0") };
  }

  idFromString(value) {
    if (!/^[0-9a-f]{64}$/.test(value)) throw new Error("invalid durable object id");
    return { toString: () => value };
  }

  idFromName(value) {
    if (!this.names.has(value)) this.names.set(value, this.newUniqueId().toString());
    return this.idFromString(this.names.get(value));
  }

  get(id) {
    const key = id.toString();
    if (!this.instances.has(key)) {
      this.instances.set(key, new OAuthSession({ storage: new MemoryStorage() }, this.env));
    }
    const instance = this.instances.get(key);
    return { fetch: (request) => instance.fetch(request) };
  }
}

function environment(kv) {
  return {
    OAUTH_SESSIONS: kv,
    OAUTH_CLIENT_ID: "synthetic-client",
    OAUTH_REDIRECT_URI: "https://worker.test/oauth/callback",
    OWNER_CLOUDFLARE_USER_ID: "synthetic-owner",
    CLOUDFLARE_ACCOUNT_ID: "synthetic-account",
    R2_BUCKET: "synthetic-bucket",
    R2_PARENT_ACCESS_KEY_ID: "synthetic-parent",
    OPERATIONAL_AGE_IDENTITY: "AGE-SECRET-KEY-synthetic",
    AGE_RECIPIENTS: JSON.stringify(["age1daily", "age1recovery"]),
  };
}

function durableEnvironment() {
  const env = environment(undefined);
  delete env.OAUTH_SESSIONS;
  env.OAUTH_SESSION = new MemoryDurableNamespace(env);
  return env;
}

function request(path, method = "GET") {
  return new Request(`https://worker.test${path}`, { method });
}

function purposeRequest(purpose) {
  return new Request("https://worker.test/session/start", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ purpose }),
  });
}

async function readJson(response) {
  return response.json();
}

async function startSession(env, purpose) {
  const response = await worker.fetch(purpose ? purposeRequest(purpose) : request("/session/start", "POST"), env);
  const body = await readJson(response);
  const state = new URL(body.authorizationUrl).searchParams.get("state");
  return { ...body, state };
}

async function authorizeR2Session(env) {
  const started = await startSession(env);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (url === "https://dash.cloudflare.com/oauth2/token") return Response.json({ access_token: "synthetic-cloudflare-token" });
    if (url === "https://dash.cloudflare.com/oauth2/userinfo") return Response.json({ sub: "synthetic-owner" });
    if (String(url) === "https://api.cloudflare.com/client/v4/accounts/synthetic-account/r2/temp-access-credentials") {
      return Response.json({ success: true, result: {
        accessKeyId: "temporary-access",
        secretAccessKey: "temporary-secret",
        sessionToken: "temporary-token",
      } });
    }
    throw new Error("unexpected authorization request");
  };
  try {
    const callback = await worker.fetch(
      request(`/oauth/callback?state=${encodeURIComponent(started.state)}&code=synthetic-code`),
      env,
    );
    assert.equal(callback.status, 200);
  } finally {
    globalThis.fetch = originalFetch;
  }
  const response = await worker.fetch(request(`/session/${started.sessionId}`), env);
  const body = await response.json();
  return { started, response: body, headers: response.headers };
}

async function authorizeLegacySession(env) {
  return authorizeR2Session(env);
}

const roomStoreMaterial = domainId => ({
  format: "josh-room-r2-room-store-material",
  version: 1,
  domainId,
  keysetGeneration: 1,
  ciphertext: "c3ludGhldGljLWFnZS1jaXBoZXJ0ZXh0",
});

function roomStoreRequest(sessionId, resource, method, capability, body) {
  return new Request(`https://worker.test/session/${sessionId}/room-store/${resource}`, {
    method,
    headers: {
      authorization: `Bearer ${capability}`,
      ...(body === undefined ? {} : { "content-type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

test("cancel removes pending state, keeps linkage private, and returns canceled", async () => {
  const kv = new MemoryKV();
  const env = environment(kv);
  const started = await startSession(env);

  const storedPending = await kv.get(`session:${started.sessionId}`, "json");
  assert.deepEqual(Object.keys(storedPending).sort(), ["state", "status"]);
  assert.equal(storedPending.status, "pending");
  assert.equal(typeof storedPending.state, "string");
  assert.equal("verifier" in storedPending, false);
  assert.deepEqual(kv.puts.slice(0, 2).map(({ options }) => options), [
    { expirationTtl: 600 },
    { expirationTtl: 600 },
  ]);

  const pendingResponse = await worker.fetch(request(`/session/${started.sessionId}`), env);
  assert.equal(pendingResponse.status, 200);
  const pendingBody = await readJson(pendingResponse);
  assert.deepEqual(pendingBody, { status: "pending" });
  assert.equal("state" in pendingBody, false);
  assert.equal("verifier" in pendingBody, false);

  const cancelResponse = await worker.fetch(
    request(`/session/${started.sessionId}/cancel`, "POST"),
    env,
  );
  assert.equal(cancelResponse.status, 200);
  assert.deepEqual(await readJson(cancelResponse), { status: "canceled" });
  assert.equal(await kv.get(`state:${started.state}`, "json"), null);
  assert.deepEqual(await kv.get(`session:${started.sessionId}`, "json"), { status: "canceled" });
  assert.deepEqual(kv.deletes, [`state:${started.state}`]);
  assert.deepEqual(kv.puts.at(-1), {
    key: `session:${started.sessionId}`,
    value: { status: "canceled" },
    options: { expirationTtl: 120 },
  });

  const afterCancel = await worker.fetch(request(`/session/${started.sessionId}`), env);
  assert.equal(afterCancel.status, 200);
  assert.deepEqual(await readJson(afterCancel), { status: "canceled" });

  const repeatedCancel = await worker.fetch(
    request(`/session/${started.sessionId}/cancel`, "POST"),
    env,
  );
  assert.equal(repeatedCancel.status, 200);
  assert.deepEqual(await readJson(repeatedCancel), { status: "canceled" });
});

test("callback after cancel is rejected and cannot recreate an authorized session", async () => {
  const kv = new MemoryKV();
  const env = environment(kv);
  const started = await startSession(env);
  await worker.fetch(request(`/session/${started.sessionId}/cancel`, "POST"), env);

  let upstreamCalls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    upstreamCalls += 1;
    throw new Error("canceled callback reached OAuth upstream");
  };
  try {
    const response = await worker.fetch(
      request(`/oauth/callback?state=${encodeURIComponent(started.state)}&code=synthetic-code`),
      env,
    );
    assert.equal(response.status, 400);
    assert.equal(await response.text(), "Invalid or expired login.");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(upstreamCalls, 0);
  assert.deepEqual(await kv.get(`session:${started.sessionId}`, "json"), { status: "canceled" });
  assert.equal(await kv.get(`state:${started.state}`, "json"), null);
});

test("a live callback still authorizes once and consumes its state", async () => {
  const kv = new MemoryKV();
  const env = environment(kv);
  const started = await startSession(env);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (url === "https://dash.cloudflare.com/oauth2/token") {
      return Response.json({ access_token: "synthetic-cloudflare-token" });
    }
    if (url === "https://dash.cloudflare.com/oauth2/userinfo") {
      return Response.json({ sub: "synthetic-owner" });
    }
    if (url === "https://api.cloudflare.com/client/v4/accounts/synthetic-account/r2/temp-access-credentials") {
      return Response.json({
        success: true,
        result: {
          accessKeyId: "temporary-access",
          secretAccessKey: "temporary-secret",
          sessionToken: "temporary-token",
        },
      });
    }
    throw new Error(`unexpected upstream URL: ${url}`);
  };
  try {
    const callback = await worker.fetch(
      request(`/oauth/callback?state=${encodeURIComponent(started.state)}&code=synthetic-code`),
      env,
    );
    assert.equal(callback.status, 200);
  } finally {
    globalThis.fetch = originalFetch;
  }

  const authorized = await kv.get(`session:${started.sessionId}`, "json");
  assert.equal(authorized.status, "authorized");
  assert.equal(authorized.secretAccessKey, "temporary-secret");
  assert.equal(await kv.get(`state:${started.state}`, "json"), null);
});

test("encryption-only authorization returns age material without R2 credentials", async () => {
  const kv = new MemoryKV();
  const env = environment(kv);
  const started = await startSession(env, "encryption");
  const authQuery = new URL(started.authorizationUrl).searchParams;
  const pending = await kv.get(`session:${started.sessionId}`, "json");
  assert.equal(pending.purpose, "encryption");
  assert.equal(authQuery.has("scope"), false);

  let temporaryCalls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (url === "https://dash.cloudflare.com/oauth2/token") {
      return Response.json({ access_token: "synthetic-cloudflare-token" });
    }
    if (url === "https://dash.cloudflare.com/oauth2/userinfo") {
      return Response.json({ sub: "synthetic-owner" });
    }
    if (String(url).includes("temp-access-credentials")) {
      temporaryCalls += 1;
      throw new Error("encryption-only authorization requested R2 credentials");
    }
    throw new Error(`unexpected upstream URL: ${url}`);
  };
  try {
    const callback = await worker.fetch(
      request(`/oauth/callback?state=${encodeURIComponent(started.state)}&code=synthetic-code`),
      env,
    );
    assert.equal(callback.status, 200);
  } finally {
    globalThis.fetch = originalFetch;
  }

  const authorized = await kv.get(`session:${started.sessionId}`, "json");
  assert.deepEqual(authorized.capabilities, ["encryption"]);
  assert.equal(authorized.purpose, "encryption");
  assert.equal("accessKeyId" in authorized, false);
  assert.equal("secretAccessKey" in authorized, false);
  assert.equal("sessionToken" in authorized, false);
  assert.equal("endpoint" in authorized, false);
  assert.equal("bucket" in authorized, false);
  assert.equal(temporaryCalls, 0);
});

test("encryption-only Durable Object authorization also omits R2 credentials", async () => {
  const env = durableEnvironment();
  const started = await startSession(env, "encryption");
  let temporaryCalls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (url === "https://dash.cloudflare.com/oauth2/token") return Response.json({ access_token: "synthetic-cloudflare-token" });
    if (url === "https://dash.cloudflare.com/oauth2/userinfo") return Response.json({ sub: "synthetic-owner" });
    if (String(url).includes("temp-access-credentials")) {
      temporaryCalls += 1;
      throw new Error("encryption-only Durable Object authorization requested R2 credentials");
    }
    throw new Error(`unexpected upstream URL: ${url}`);
  };
  try {
    const callback = await worker.fetch(
      request(`/oauth/callback?state=${encodeURIComponent(started.state)}&code=synthetic-code`),
      env,
    );
    assert.equal(callback.status, 200);
  } finally {
    globalThis.fetch = originalFetch;
  }

  const response = await worker.fetch(request(`/session/${started.sessionId}`), env);
  const authorized = await readJson(response);
  assert.deepEqual(authorized.capabilities, ["encryption"]);
  assert.equal(authorized.purpose, "encryption");
  assert.equal("accessKeyId" in authorized, false);
  assert.equal("secretAccessKey" in authorized, false);
  assert.equal("sessionToken" in authorized, false);
  assert.equal(temporaryCalls, 0);
});

test("Durable Object callback is immediately visible to a poll from another request", async () => {
  const env = durableEnvironment();
  const started = await startSession(env);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (url === "https://dash.cloudflare.com/oauth2/token") {
      return Response.json({ access_token: "synthetic-cloudflare-token" });
    }
    if (url === "https://dash.cloudflare.com/oauth2/userinfo") {
      return Response.json({ sub: "synthetic-owner" });
    }
    if (url === "https://api.cloudflare.com/client/v4/accounts/synthetic-account/r2/temp-access-credentials") {
      return Response.json({ success: true, result: {
        accessKeyId: "temporary-access",
        secretAccessKey: "temporary-secret",
        sessionToken: "temporary-token",
      } });
    }
    throw new Error(`unexpected upstream URL: ${url}`);
  };
  try {
    const callback = await worker.fetch(
      request(`/oauth/callback?state=${encodeURIComponent(started.state)}&code=synthetic-code`),
      env,
    );
    assert.equal(callback.status, 200);
    const status = await worker.fetch(request(`/session/${started.sessionId}`), env);
    assert.equal(status.status, 200);
    const body = await status.json();
    assert.equal(body.status, "authorized");
    assert.equal(body.secretAccessKey, "temporary-secret");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("R2 Room Store keeps only a short-lived capability and immutable ciphertext in Durable Objects", async () => {
  const env = durableEnvironment();
  const { started, response: authorized, headers } = await authorizeR2Session(env);
  assert.equal(authorized.status, "authorized");
  assert.equal(authorized.roomStoreCapabilityExpiresIn, 600);
  assert.match(authorized.roomStoreDomainId, /^[0-9a-f]{64}$/);
  assert.match(authorized.roomStoreCapability, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(headers.get("cache-control"), "no-store");
  const consumed = await worker.fetch(request(`/session/${started.sessionId}`), env);
  assert.deepEqual(await consumed.json(), { status: "consumed" });

  const sessionState = env.OAUTH_SESSION.instances.get(started.sessionId).state.storage.values.get("session");
  assert.deepEqual(Object.keys(sessionState).sort(), [
    "accountId", "bucket", "capabilityHash", "expiresAt", "purpose", "status",
  ]);
  assert.equal(sessionState.status, "consumed");
  assert.equal(sessionState.purpose, "r2");
  assert.equal(JSON.stringify(sessionState).includes("temporary-secret"), false);
  assert.equal(JSON.stringify(sessionState).includes("AGE-SECRET-KEY-synthetic"), false);

  const material = roomStoreMaterial(authorized.roomStoreDomainId);
  const invalid = await worker.fetch(roomStoreRequest(
    started.sessionId, "material", "POST", authorized.roomStoreCapability,
    { ...material, unexpected: true },
  ), env);
  assert.equal(invalid.status, 400);
  assert.deepEqual(await invalid.json(), { error: "room_store_invalid_material" });

  const wrongScope = await worker.fetch(roomStoreRequest(
    started.sessionId, "material", "POST", authorized.roomStoreCapability,
    { ...material, domainId: "0".repeat(64) },
  ), env);
  assert.equal(wrongScope.status, 400);

  const created = await worker.fetch(roomStoreRequest(
    started.sessionId, "material", "POST", authorized.roomStoreCapability, material,
  ), env);
  assert.equal(created.status, 201);
  assert.equal(created.headers.get("cache-control"), "no-store");
  const createdBody = await created.json();
  assert.equal(createdBody.status, "created");
  assert.deepEqual(createdBody.material, { ...material, repositoryId: null });

  const read = await worker.fetch(roomStoreRequest(
    started.sessionId, "material", "GET", authorized.roomStoreCapability,
  ), env);
  assert.equal(read.status, 200);
  assert.deepEqual(await read.json(), { status: "ready", material: createdBody.material });

  const secondSession = await authorizeR2Session(env);
  const secondRead = await worker.fetch(roomStoreRequest(
    secondSession.started.sessionId, "material", "GET", secondSession.response.roomStoreCapability,
  ), env);
  assert.equal(secondRead.status, 200);
  assert.deepEqual(await secondRead.json(), { status: "ready", material: createdBody.material });

  const replacement = await worker.fetch(roomStoreRequest(
    started.sessionId, "material", "POST", authorized.roomStoreCapability,
    { ...material, ciphertext: `${material.ciphertext}A` },
  ), env);
  assert.equal(replacement.status, 409);
  assert.deepEqual(await replacement.json(), { error: "room_store_material_conflict" });

  const repositoryId = "a".repeat(64);
  const bound = await worker.fetch(roomStoreRequest(
    started.sessionId, "repository", "POST", authorized.roomStoreCapability, { repositoryId, expectedGeneration: 1 },
  ), env);
  assert.equal(bound.status, 201);
  assert.deepEqual(await bound.json(), { status: "bound", repositoryId, keysetGeneration: 2 });
  const repeatedBind = await worker.fetch(roomStoreRequest(
    secondSession.started.sessionId, "repository", "POST", secondSession.response.roomStoreCapability, { repositoryId, expectedGeneration: 1 },
  ), env);
  assert.equal(repeatedBind.status, 200);
  assert.deepEqual(await repeatedBind.json(), { status: "bound", repositoryId, keysetGeneration: 2 });
  const currentGenerationRetry = await worker.fetch(roomStoreRequest(
    secondSession.started.sessionId, "repository", "POST", secondSession.response.roomStoreCapability, { repositoryId, expectedGeneration: 2 },
  ), env);
  assert.equal(currentGenerationRetry.status, 200);
  assert.deepEqual(await currentGenerationRetry.json(), { status: "bound", repositoryId, keysetGeneration: 2 });
  const conflictingBind = await worker.fetch(roomStoreRequest(
    started.sessionId, "repository", "POST", authorized.roomStoreCapability, { repositoryId: "b".repeat(64), expectedGeneration: 1 },
  ), env);
  assert.equal(conflictingBind.status, 409);
  assert.deepEqual(await conflictingBind.json(), { error: "room_store_repository_conflict" });

  const wrongCapability = await worker.fetch(roomStoreRequest(
    started.sessionId, "material", "GET", `${authorized.roomStoreCapability.slice(0, -1)}A`,
  ), env);
  assert.equal(wrongCapability.status, 403);
  assert.deepEqual(await wrongCapability.json(), { error: "room_store_capability_invalid" });

  sessionState.expiresAt = Date.now() - 1;
  const expiredCapability = await worker.fetch(roomStoreRequest(
    started.sessionId, "material", "GET", authorized.roomStoreCapability,
  ), env);
  assert.equal(expiredCapability.status, 404);
  assert.deepEqual(await expiredCapability.json(), { error: "room_store_capability_expired" });
});

test("R2 Room Store capability is unavailable to encryption-only and legacy KV sessions", async () => {
  const env = durableEnvironment();
  const started = await startSession(env, "encryption");
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (url === "https://dash.cloudflare.com/oauth2/token") return Response.json({ access_token: "synthetic-cloudflare-token" });
    if (url === "https://dash.cloudflare.com/oauth2/userinfo") return Response.json({ sub: "synthetic-owner" });
    throw new Error("unexpected encryption authorization request");
  };
  try {
    const callback = await worker.fetch(request(
      `/oauth/callback?state=${encodeURIComponent(started.state)}&code=synthetic-code`,
    ), env);
    assert.equal(callback.status, 200);
  } finally {
    globalThis.fetch = originalFetch;
  }
  const encryptionStatus = await worker.fetch(request(`/session/${started.sessionId}`), env);
  const encryptionBody = await encryptionStatus.json();
  assert.equal("roomStoreCapability" in encryptionBody, false);

  const kvEnv = environment(new MemoryKV());
  const legacy = await authorizeLegacySession(kvEnv);
  assert.equal("roomStoreCapability" in legacy.response, false);
  const unsupported = await worker.fetch(roomStoreRequest(
    legacy.started.sessionId, "material", "GET", "A".repeat(43),
  ), kvEnv);
  assert.equal(unsupported.status, 503);
  assert.deepEqual(await unsupported.json(), { error: "room_store_durable_authority_unavailable" });
});

test("concurrent Room Store material creation has one durable winner", async () => {
  const env = durableEnvironment();
  const firstSession = await authorizeR2Session(env);
  const domainId = firstSession.response.roomStoreDomainId;
  const first = roomStoreMaterial(domainId);
  const second = { ...first, ciphertext: `${first.ciphertext}A` };
  const [firstResponse, secondResponse] = await Promise.all([
    worker.fetch(roomStoreRequest(firstSession.started.sessionId, "material", "POST", firstSession.response.roomStoreCapability, first), env),
    worker.fetch(roomStoreRequest(firstSession.started.sessionId, "material", "POST", firstSession.response.roomStoreCapability, second), env),
  ]);
  assert.deepEqual([firstResponse.status, secondResponse.status].sort(), [201, 409]);
  const winner = await worker.fetch(roomStoreRequest(
    firstSession.started.sessionId, "material", "GET", firstSession.response.roomStoreCapability,
  ), env);
  assert.equal(winner.status, 200);
  const material = (await winner.json()).material;
  assert.equal([first.ciphertext, second.ciphertext].includes(material.ciphertext), true);
});

test("Room Store records are isolated by the configured physical bucket", async () => {
  const firstEnv = durableEnvironment();
  const firstSession = await authorizeR2Session(firstEnv);
  const firstMaterial = roomStoreMaterial(firstSession.response.roomStoreDomainId);
  const created = await worker.fetch(roomStoreRequest(
    firstSession.started.sessionId, "material", "POST", firstSession.response.roomStoreCapability, firstMaterial,
  ), firstEnv);
  assert.equal(created.status, 201);

  const secondEnv = environment(undefined);
  delete secondEnv.OAUTH_SESSIONS;
  secondEnv.R2_BUCKET = "synthetic-other-bucket";
  secondEnv.OAUTH_SESSION = firstEnv.OAUTH_SESSION;
  secondEnv.OAUTH_SESSION.env = secondEnv;
  const secondSession = await authorizeR2Session(secondEnv);
  assert.notEqual(secondSession.response.roomStoreDomainId, firstSession.response.roomStoreDomainId);
  const secondMaterial = await worker.fetch(roomStoreRequest(
    secondSession.started.sessionId, "material", "GET", secondSession.response.roomStoreCapability,
  ), secondEnv);
  assert.equal(secondMaterial.status, 404);
  assert.deepEqual(await secondMaterial.json(), { error: "room_store_material_missing" });
});

test("Room Store material is rejected and rolled back when transactional readback differs", async () => {
  const env = durableEnvironment();
  const authorized = await authorizeR2Session(env);
  const name = "josh-room:r2-room-store:v1:synthetic-account:synthetic-bucket";
  const objectId = env.OAUTH_SESSION.idFromName(name).toString();
  env.OAUTH_SESSION.get(env.OAUTH_SESSION.idFromString(objectId));
  const storage = env.OAUTH_SESSION.instances.get(objectId).state.storage;
  storage.transaction = async callback => {
    const before = new Map(storage.values);
    try {
      return await callback({
        get: key => storage.get(key),
        put: (key, value) => storage.put(key, { ...value, ciphertext: `${value.ciphertext}tampered` }),
      });
    } catch (error) {
      storage.values = before;
      throw error;
    }
  };
  const response = await worker.fetch(roomStoreRequest(
    authorized.started.sessionId,
    "material",
    "POST",
    authorized.response.roomStoreCapability,
    roomStoreMaterial(authorized.response.roomStoreDomainId),
  ), env);
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "room_store_material_write_unverified" });
  assert.equal(await storage.get("r2-room-store-material-v1"), undefined);
});

test("unknown cancellation is expired and authorized sessions are protected", async () => {
  const kv = new MemoryKV();
  const env = environment(kv);

  const unknown = await worker.fetch(request("/session/unknown/cancel", "POST"), env);
  assert.equal(unknown.status, 404);
  assert.deepEqual(await readJson(unknown), { status: "expired" });
  assert.deepEqual(kv.deletes, []);

  const sessionId = "authorized-session";
  const state = "authorized-state";
  const authorized = {
    status: "authorized",
    accessKeyId: "temporary-access",
    secretAccessKey: "temporary-secret",
    sessionToken: "temporary-token",
    state,
  };
  await kv.put(`session:${sessionId}`, JSON.stringify(authorized), { expirationTtl: 600 });
  await kv.put(`state:${state}`, JSON.stringify({ id: sessionId, verifier: "temporary-verifier" }), { expirationTtl: 600 });

  const canceledAuthorized = await worker.fetch(request(`/session/${sessionId}/cancel`, "POST"), env);
  assert.equal(canceledAuthorized.status, 409);
  assert.deepEqual(await readJson(canceledAuthorized), { status: "authorized" });
  assert.deepEqual(await kv.get(`session:${sessionId}`, "json"), authorized);
  assert.deepEqual(await kv.get(`state:${state}`, "json"), {
    id: sessionId,
    verifier: "temporary-verifier",
  });
  assert.equal(JSON.stringify(await readJson(await worker.fetch(request(`/session/${sessionId}/cancel`, "POST"), env))).includes("temporary-secret"), false);
});
