import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import { createHmac } from "node:crypto";
import { contract } from "../../worker/test/room-store-contract.mjs";
const require = createRequire(process.env.ROOM_STORE_TEST_TOOLS ? `${process.env.ROOM_STORE_TEST_TOOLS}/package.json` : new URL("../../worker/package.json", import.meta.url));
const { Miniflare } = require("miniflare");
const { build } = require("esbuild");
const result = await build({entryPoints:[new URL("../../worker/celld/index.js", import.meta.url).pathname],bundle:true,format:"esm",write:false});
const domain = "c".repeat(64), key = Buffer.alloc(32, 7).toString("base64url");
const temporary = mkdtempSync(join(tmpdir(), "codex-room-store-workerd-"));
const mf = new Miniflare({defaultPersistRoot:temporary,modules:true,script:result.outputFiles[0].text + `
export class TestRoomStore extends RoomStore { async fetch(request) { if (new URL(request.url).pathname === "/corrupt") { await this.state.storage.put("r2-room-store-material-v1", {corrupted:true}); return new Response("ok"); } return super.fetch(request); } }`,compatibilityDate:"2026-07-01",durableObjects:{ROOM_STORE:{className:"TestRoomStore",useSQLite:true}},bindings:{ROOM_STORE_DOMAIN_ID:domain,ROOM_STORE_EXPERIMENTAL:"enabled",ROOM_STORE_SIGNING_KEY:key}});
try {
  const call = async (method, operation, body, mode="write") => {
    const now = Math.floor(Date.now()/1000);
    const payload = Buffer.from(JSON.stringify({domain:mode==="wrong-domain"?"d".repeat(64):domain,access:mode==="read"?"read":"write",iat:now-2,exp:mode==="expired"?now-1:now+600})).toString("base64url");
    const token = `${payload}.${createHmac("sha256",Buffer.from(key,"base64url")).update(payload).digest("base64url")}`;
    const response = await mf.dispatchFetch(`http://localhost/room-store/${domain}/${operation}`,{method,headers:{authorization:`Bearer ${token}`,"content-type":"application/json"},...(body===undefined?{}:{body:JSON.stringify(body)})});
    return {status:response.status,body:await response.json()};
  };
  await contract(call,domain);
  const ns = await mf.getDurableObjectNamespace("ROOM_STORE");
  await ns.get(ns.idFromName(`room-store:v1:${domain}`)).fetch("https://internal/corrupt");
  if ((await call("GET","material")).status !== 503) throw new Error("corrupted state accepted");
  console.log("PASS workerd shared authority contracts and corrupt-record rejection");
} finally { await mf.dispose(); rmSync(temporary,{recursive:true,force:true}); }
