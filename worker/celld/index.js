import { RoomStore } from "../src/room-store.js";
export { RoomStore };

const error = (code, status) => Response.json({ error: code }, {
  status, headers: { "cache-control": "no-store" },
});
const decode = value => Uint8Array.from(atob(value.replaceAll("-", "+").replaceAll("_", "/")), c => c.charCodeAt(0));

export default {
  async fetch(request, env) {
    if (env.ROOM_STORE_EXPERIMENTAL !== "enabled") return error("room_store_durable_authority_unavailable", 503);
    const path = new URL(request.url).pathname.match(/^\/room-store\/([0-9a-f]{64})\/(material|repository|seal)$/);
    if (!path || path[1] !== env.ROOM_STORE_DOMAIN_ID) return error("room_store_scope_mismatch", 403);
    if (!["GET", "POST"].includes(request.method) || (path[2] !== "material" && request.method !== "POST")) return error("room_store_method_not_allowed", 405);
    const token = request.headers.get("authorization")?.match(/^Bearer ([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$/);
    if (!token) return error("room_store_capability_required", 401);
    let claims;
    try {
      if (!/^[A-Za-z0-9_-]{43}$/.test(env.ROOM_STORE_SIGNING_KEY || "")) throw new Error();
      const key = await crypto.subtle.importKey("raw", decode(env.ROOM_STORE_SIGNING_KEY), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
      if (!await crypto.subtle.verify("HMAC", key, decode(token[2]), new TextEncoder().encode(token[1]))) throw new Error();
      claims = JSON.parse(new TextDecoder().decode(decode(token[1])));
      if (claims.domain !== path[1] || !["read", "write"].includes(claims.access)) throw new Error();
      const now = Math.floor(Date.now() / 1000);
      if (!Number.isSafeInteger(claims.exp) || !Number.isSafeInteger(claims.iat) || claims.exp <= now || claims.iat > now || claims.exp <= claims.iat || claims.exp - claims.iat > 3600) return error("room_store_capability_expired", 403);
    } catch {
      return error("room_store_capability_invalid", 403);
    }
    if (request.method === "POST" && claims.access !== "write") return error("room_store_capability_invalid", 403);
    const operation = `${path[2]}-${request.method === "GET" ? "read" : "write"}`;
    const stub = env.ROOM_STORE.get(env.ROOM_STORE.idFromName(`room-store:v1:${path[1]}`));
    return stub.fetch(new Request(`https://room-store/${operation}`, {
      method: request.method,
      headers: { "content-type": request.headers.get("content-type") || "" },
      ...(request.method === "POST" ? { body: await request.text() } : {}),
    }));
  },
};
