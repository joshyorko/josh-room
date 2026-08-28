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

async function durableRouter(request, env) {
  const url = new URL(request.url);
  if (url.pathname === "/session/start" && request.method === "POST") {
    const id = env.OAUTH_SESSION.newUniqueId();
    const sessionId = id.toString();
    const nonce = random();
    const verifier = random();
    const challenge = b64(new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(verifier))));
    await env.OAUTH_SESSION.get(id).fetch(new Request("https://session/start", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ nonce, verifier }),
    }));
    const state = `${sessionId}.${nonce}`;
    const auth = new URL("https://dash.cloudflare.com/oauth2/auth");
    for (const [k, v] of Object.entries({ response_type: "code", client_id: env.OAUTH_CLIENT_ID, redirect_uri: env.OAUTH_REDIRECT_URI, scope: "workers-r2.read workers-r2.write", state, code_challenge: challenge, code_challenge_method: "S256" })) auth.searchParams.set(k, v);
    return Response.json({ sessionId, authorizationUrl: auth.toString(), expiresIn: 600 });
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
      const { nonce, verifier } = await request.json();
      const expiresAt = Date.now() + 600_000;
      await this.state.storage.put("session", { status: "pending", nonce, verifier, expiresAt });
      await this.state.storage.setAlarm(expiresAt);
      return Response.json({ status: "pending" });
    }
    const session = await this.state.storage.get("session");
    if (!session || session.expiresAt <= Date.now()) {
      await this.state.storage.deleteAll();
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
      if (session.status !== "authorized") return Response.json({ status: session.status || "expired" });
      await this.state.storage.deleteAll();
      return Response.json(session);
    }
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
      const temporary = await fetch(`https://api.cloudflare.com/client/v4/accounts/${this.env.CLOUDFLARE_ACCOUNT_ID}/r2/temp-access-credentials`, {
        method: "POST",
        headers: { authorization: `Bearer ${result.access_token}`, "content-type": "application/json" },
        body: JSON.stringify({ bucket: this.env.R2_BUCKET, permission: "object-read-write", ttlSeconds: 21600, parentAccessKeyId: this.env.R2_PARENT_ACCESS_KEY_ID })
      });
      const temporaryResult = await temporary.json();
      if (!temporary.ok || !temporaryResult.success) return new Response("Temporary R2 authorization failed.", { status: 502 });
      const current = await this.state.storage.get("session");
      if (!current || current.status !== "pending") return new Response("Invalid or expired login.", { status: 400 });
      await this.state.storage.put("session", {
        status: "authorized",
        accessKeyId: temporaryResult.result.accessKeyId,
        secretAccessKey: temporaryResult.result.secretAccessKey,
        sessionToken: temporaryResult.result.sessionToken,
        ageIdentity: this.env.OPERATIONAL_AGE_IDENTITY,
        ageRecipients: JSON.parse(this.env.AGE_RECIPIENTS),
        endpoint: `https://${this.env.CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com`,
        bucket: this.env.R2_BUCKET,
        expiresIn: 21600,
        expiresAt: Date.now() + 600_000,
      });
      return new Response("Josh Room authorized. Return to VS Code.");
    }
    return new Response("Not found", { status: 404 });
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
      const id = crypto.randomUUID();
      const state = random();
      const verifier = random();
      const challenge = b64(new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(verifier))));
      await env.OAUTH_SESSIONS.put(`state:${state}`, JSON.stringify({ id, verifier }), { expirationTtl: 600 });
      await env.OAUTH_SESSIONS.put(`session:${id}`, JSON.stringify({ status: "pending", state }), { expirationTtl: 600 });
      const auth = new URL("https://dash.cloudflare.com/oauth2/auth");
      for (const [k, v] of Object.entries({ response_type: "code", client_id: env.OAUTH_CLIENT_ID, redirect_uri: env.OAUTH_REDIRECT_URI, scope: "workers-r2.read workers-r2.write", state, code_challenge: challenge, code_challenge_method: "S256" })) auth.searchParams.set(k, v);
      return Response.json({ sessionId: id, authorizationUrl: auth.toString(), expiresIn: 600 });
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
      const temporary = await fetch(`https://api.cloudflare.com/client/v4/accounts/${env.CLOUDFLARE_ACCOUNT_ID}/r2/temp-access-credentials`, {
        method: "POST",
        headers: { authorization: `Bearer ${result.access_token}`, "content-type": "application/json" },
        body: JSON.stringify({ bucket: env.R2_BUCKET, permission: "object-read-write", ttlSeconds: 21600, parentAccessKeyId: env.R2_PARENT_ACCESS_KEY_ID })
      });
      const temporaryResult = await temporary.json();
      if (!temporary.ok || !temporaryResult.success) return new Response("Temporary R2 authorization failed.", { status: 502 });
      const current = await env.OAUTH_SESSIONS.get(sessionKey, "json");
      if (!current || current.status !== "pending") return new Response("Invalid or expired login.", { status: 400 });
      await env.OAUTH_SESSIONS.put(sessionKey, JSON.stringify({
        status: "authorized",
        accessKeyId: temporaryResult.result.accessKeyId,
        secretAccessKey: temporaryResult.result.secretAccessKey,
        sessionToken: temporaryResult.result.sessionToken,
        ageIdentity: env.OPERATIONAL_AGE_IDENTITY,
        ageRecipients: JSON.parse(env.AGE_RECIPIENTS),
        endpoint: `https://${env.CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com`,
        bucket: env.R2_BUCKET,
        expiresIn: 21600
      }), { expirationTtl: 600 });
      return new Response("Josh Room authorized. Return to VS Code.");
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
