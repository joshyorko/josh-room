function records(value, idField = "id") {
  if (Array.isArray(value)) return value.map((item) => ({ ...item }));
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).map(([id, item]) => ({
    ...(item && typeof item === "object" ? item : {}),
    [idField]: item?.[idField] || id,
  }));
}

function connectionRecords(payload = {}) {
  const direct = records(payload.connections, "connection_id");
  const providers = records(payload.providers).flatMap((provider) => (
    records(provider.connections, "connection_id").map((connection) => ({
      provider: provider.id || provider.provider || connection.provider,
      ...connection,
    }))
  ));
  const seen = new Set();
  return [...direct, ...providers].filter((connection) => {
    const id = connection.id || connection.connection_id;
    if (!id || seen.has(id)) return false;
    seen.add(id);
    return true;
  }).map((connection) => ({
    ...connection,
    id: connection.id || connection.connection_id,
    connection_id: connection.connection_id || connection.id,
  }));
}

function dimensionConnection(dimension, connections = []) {
  const id = dimension?.connection_id || dimension?.connectionId
    || dimension?.connection?.id || dimension?.connection?.connection_id;
  const found = connections.find((connection) => connection.id === id || connection.connection_id === id);
  if (found) return found;
  return {
    id: id || `${dimension?.provider || "provider"}:${dimension?.endpoint || "default"}`,
    connection_id: id || `${dimension?.provider || "provider"}:${dimension?.endpoint || "default"}`,
    provider: dimension?.provider,
    display_name: dimension?.connection_name || dimension?.connection?.display_name
      || dimension?.connection?.name || dimension?.endpoint || "Provider Connection",
    endpoint: dimension?.endpoint,
  };
}

function bucketChoices(payload = {}) {
  const buckets = (Array.isArray(payload.buckets || payload.bucket_list)
    ? (payload.buckets || payload.bucket_list).map((bucket) => typeof bucket === "string" ? bucket : bucket.name || bucket.bucket)
    : records(payload.buckets || payload.bucket_list, "name").map((bucket) => bucket.name || bucket.bucket))
    .filter(Boolean)
    .map((bucket) => ({ label: bucket, bucket }));
  const choices = buckets.length || payload.ok !== false
    ? [...buckets, { label: "$(add) Create Bucket…", create: true }, { label: "Enter Bucket Manually…", manual: true }]
    : [{ label: "Enter Bucket Manually…", manual: true }];
  return choices;
}

function connectionCommand(action, { provider, endpoint, connectionId, bucket } = {}) {
  if (action === "create") return ["provider", "connection", "create", "--provider", provider, "--endpoint", endpoint];
  if (action === "list") return ["provider", "connection", "list"];
  if (action === "list-buckets") return ["provider", "bucket", "list", "--connection", connectionId];
  if (action === "create-bucket") return ["provider", "bucket", "create", "--connection", connectionId, "--bucket", bucket];
  if (action === "check-bucket") return ["provider", "bucket", "check", "--connection", connectionId, "--bucket", bucket];
  if (action === "disconnect") return ["provider", "connection", "disconnect", "--connection", connectionId];
  if (action === "reconnect") return ["provider", "connection", "reconnect", "--connection", connectionId, "--endpoint", endpoint];
  return ["provider", "connection", action, "--connection", connectionId];
}

module.exports = { bucketChoices, connectionCommand, connectionRecords, dimensionConnection, records };
