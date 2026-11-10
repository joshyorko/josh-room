import { createHmac } from "node:crypto";
import { contract } from "../../worker/test/room-store-contract.mjs";
const domain = process.env.ROOM_STORE_TEST_DOMAIN;
const key = Buffer.from(process.env.ROOM_STORE_TEST_KEY,"base64url");
const endpoints = JSON.parse(process.env.ROOM_STORE_TEST_ENDPOINTS);
let next = 0;
const call = async (method, operation, body, mode="write") => {
  const now = Math.floor(Date.now()/1000);
  const payload = Buffer.from(JSON.stringify({domain:mode==="wrong-domain"?"d".repeat(64):domain,access:mode==="read"?"read":"write",iat:now-2,exp:mode==="expired"?now-1:now+600})).toString("base64url");
  const signature = createHmac("sha256",key).update(payload).digest("base64url");
  const response = await fetch(`${endpoints[next++ % endpoints.length]}/room-store/${domain}/${operation}`,{method,headers:{authorization:`Bearer ${payload}.${signature}`,"content-type":"application/json"},...(body===undefined?{}:{body:JSON.stringify(body)})});
  return {status:response.status,body:await response.json()};
};
await contract(call,domain);
console.log("PASS celld same cross-runtime contract module across two nodes");
