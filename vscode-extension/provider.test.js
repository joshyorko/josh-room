const assert = require("node:assert/strict");
const test = require("node:test");

const {
  bucketChoices,
  connectionCommand,
  connectionRecords,
  dimensionConnection,
} = require("./provider");

test("connectionRecords normalizes the backend worker connection JSON contract", () => {
  assert.deepEqual(connectionRecords({
    providers: [{
      id: "minio",
      connections: [{ connection_id: "home", name: "Home MinIO", provider: "minio" }],
    }],
  }), [{
    id: "home",
    connection_id: "home",
    name: "Home MinIO",
    provider: "minio",
  }]);
});

test("dimensionConnection reuses one worker connection for every bucket Dimension", () => {
  const connections = connectionRecords({
    connections: [{ id: "home", display_name: "Home MinIO", provider: "minio" }],
  });
  assert.equal(dimensionConnection({
    id: "rooms",
    provider: "minio",
    connection_id: "home",
    bucket: "rooms",
  }, connections).id, "home");
});

test("bucketChoices includes accessible buckets plus Create Bucket and preserves manual fallback", () => {
  assert.deepEqual(bucketChoices({ buckets: [{ name: "rooms" }, { bucket: "archive" }] }), [
    { label: "rooms", bucket: "rooms" },
    { label: "archive", bucket: "archive" },
    { label: "$(add) Create Bucket…", create: true },
    { label: "Enter Bucket Manually…", manual: true },
  ]);
  assert.deepEqual(bucketChoices({ ok: false, forbidden: true }), [
    { label: "Enter Bucket Manually…", manual: true },
  ]);
});

test("connectionCommand keeps MinIO credentials in the JSON handoff, never argv", () => {
  assert.deepEqual(connectionCommand("create", {
    provider: "minio",
    endpoint: "https://minio.example",
  }), ["provider", "connection", "create", "--provider", "minio", "--endpoint", "https://minio.example"]);
  assert.deepEqual(connectionCommand("list"), ["provider", "connection", "list"]);
  assert.deepEqual(connectionCommand("list-buckets", { connectionId: "home-minio" }), [
    "provider", "bucket", "list", "--connection", "home-minio",
  ]);
  assert.deepEqual(connectionCommand("create-bucket", { connectionId: "home-minio", bucket: "new-room" }), [
    "provider", "bucket", "create", "--connection", "home-minio", "--bucket", "new-room",
  ]);
  assert.deepEqual(connectionCommand("check-bucket", { connectionId: "home-minio", bucket: "new-room" }), [
    "provider", "bucket", "check", "--connection", "home-minio", "--bucket", "new-room",
  ]);
});
