import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { RoomStore } from "../worker/src/room-store.js";

const [domain, file] = process.argv.slice(2);
const input = JSON.parse(readFileSync(0,"utf8"));
let values = existsSync(file) ? JSON.parse(readFileSync(file,"utf8")) : {};
const storage = {
  async get(key) { return values[key]; },
  async transaction(callback) {
    const candidate = structuredClone(values);
    const result = await callback({
      get: async key => candidate[key],
      put: async (key, value) => { candidate[key] = structuredClone(value); },
    });
    values = candidate;
    writeFileSync(file,JSON.stringify(values),{mode:0o600});
    return result;
  },
};
const actor = new RoomStore({storage},{ROOM_STORE_DOMAIN_ID:domain});
const operation = `${input.operation}-${input.method === "GET" ? "read" : "write"}`;
const response = await actor.fetch(new Request(`https://actor/${operation}`,{
  method:input.method,headers:{"content-type":"application/json"},
  ...(input.body === null?{}:{body:JSON.stringify(input.body)}),
}));
process.stdout.write(JSON.stringify({status:response.status,body:await response.json()}));
