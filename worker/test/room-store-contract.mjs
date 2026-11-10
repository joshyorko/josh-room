import assert from "node:assert/strict";

export async function contract(call, domain) {
  const missing = await call("GET", "material");
  assert.equal(missing.status, 404, "read must not initialize");
  const candidate = ciphertext => ({
    format: "josh-room-r2-room-store-material", version: 1,
    domainId: domain, keysetGeneration: 1, ciphertext,
  });
  assert.equal((await call("POST", "material", candidate("first"), "read")).status, 403);
  assert.equal((await call("GET", "material", undefined, "expired")).status, 403);
  assert.equal((await call("GET", "material", undefined, "wrong-domain")).status, 403);
  const initial = await Promise.all(["Zmlyc3Q", "c2Vjb25k"].map(value => call("POST", "material", candidate(value))));
  assert.deepEqual(initial.map(r => r.status).sort(), [201, 409], "exactly one initializer");
  const winner = (await call("GET", "material")).body.material;
  assert.equal(winner.repositoryId, null);
  assert.equal((await call("POST", "material", candidate(winner.ciphertext))).status, 200);
  assert.equal((await call("POST", "material", {...candidate("bad"), domainId:"f".repeat(64)})).status, 400);
  assert.equal((await call("POST", "repository", {repositoryId:"a".repeat(64),expectedGeneration:3})).status, 409);
  const bound = await Promise.all(["a", "b"].map(id => call("POST", "repository", {repositoryId:id.repeat(64), expectedGeneration:1})));
  assert.deepEqual(bound.map(r => r.status).sort(), [201, 409], "exactly one repository");
  const record = (await call("GET", "material")).body.material;
  assert.equal(record.ciphertext, winner.ciphertext);
  assert.equal(record.keysetGeneration, 2);
  assert.equal((await call("POST", "repository", {repositoryId:record.repositoryId,expectedGeneration:1})).status, 200);
  assert.equal((await call("POST", "repository", {repositoryId:record.repositoryId,expectedGeneration:2})).status, 200);
  assert.equal((await call("POST", "repository", {repositoryId:record.repositoryId,expectedGeneration:8})).status, 409);
  // Discard a committed response, then recover only by readback and retry.
  await call("POST", "repository", {repositoryId:record.repositoryId,expectedGeneration:1});
  assert.deepEqual((await call("GET", "material")).body.material, record);
  return record;
}
