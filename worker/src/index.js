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

export default {
  async fetch(request, env) {
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
