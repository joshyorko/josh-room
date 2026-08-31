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
  }

  async get(key) { return this.values.get(key); }
  async put(key, value) { this.values.set(key, value); }
  async deleteAll() { this.values.clear(); }
  async setAlarm() {}
}

class MemoryDurableNamespace {
  constructor(env) {
    this.env = env;
    this.instances = new Map();
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
