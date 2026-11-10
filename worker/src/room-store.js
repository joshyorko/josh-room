const jsonError = (error, status) => Response.json({ error }, { status, headers: { "cache-control": "no-store" } });
const privateJson = (value, init = {}) => Response.json(value, {
  ...init, headers: { "cache-control": "no-store" },
});

const validRoomStoreRepositoryId = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const roomStoreMaterialFields = ["ciphertext", "domainId", "format", "keysetGeneration", "version"];
const validateRoomStoreMaterial = (body, domainId) => {
  if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).sort().join(",") !== roomStoreMaterialFields.join(",")) return null;
  if (body.format !== "josh-room-r2-room-store-material" || body.version !== 1 || body.domainId !== domainId) return null;
  if (!Number.isSafeInteger(body.keysetGeneration) || body.keysetGeneration < 1 || body.keysetGeneration > 2147483647) return null;
  if (typeof body.ciphertext !== "string" || body.ciphertext.length < 1 || body.ciphertext.length > 16384 || !/^[A-Za-z0-9_-]+$/.test(body.ciphertext)) return null;
  return {
    format: "josh-room-r2-room-store-material",
    version: 1,
    domainId,
    keysetGeneration: body.keysetGeneration,
    ciphertext: body.ciphertext,
    repositoryId: null,
  };
};
const sameRoomStoreMaterial = (left, right) => left && right
  && left.format === right.format
  && left.version === right.version
  && left.domainId === right.domainId
  && left.keysetGeneration === right.keysetGeneration
  && left.ciphertext === right.ciphertext;
const sameRoomStoreCiphertext = (left, right) => left && right
  && left.format === right.format
  && left.version === right.version
  && left.domainId === right.domainId
  && left.ciphertext === right.ciphertext;

const validRecord = (value, domainId) => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const { repositoryId, ...material } = value;
  return validateRoomStoreMaterial(material, domainId) !== null
    && (repositoryId === null || validRoomStoreRepositoryId(repositoryId));
};

export class RoomStore {
  constructor(state, env) {
    this.state = state;
    this.domainId = env.ROOM_STORE_DOMAIN_ID;
    if (!/^[0-9a-f]{64}$/.test(this.domainId || "")) throw new Error("room_store_scope_invalid");
  }

  async fetch(request, operation = new URL(request.url).pathname.split("/").pop()) {
    const key = "r2-room-store-material-v1";
    const storage = this.state.storage;
    if (operation === "seal-write" && request.method === "POST") {
      try {
        await storage.transaction(transaction => transaction.put("room-store-sealed-v1", true));
        return privateJson({ status: "sealed" });
      } catch {
        return jsonError("room_store_repository_write_unverified", 503);
      }
    }
    if (operation === "material-read" && request.method === "GET") {
      const material = await storage.get(key);
      if (material && !validRecord(material, this.domainId)) return jsonError("room_store_material_corrupt", 503);
      return material ? privateJson({ status: "ready", material }) : jsonError("room_store_material_missing", 404);
    }
    if (operation === "material-write" && request.method === "POST") {
      if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return jsonError("room_store_invalid_material", 400);
      let submitted;
      try {
        submitted = validateRoomStoreMaterial(await request.json(), this.domainId);
      } catch (_error) {
        submitted = null;
      }
      if (!submitted) return jsonError("room_store_invalid_material", 400);
      try {
        return await storage.transaction(async transaction => {
          if (await transaction.get("room-store-sealed-v1")) return jsonError("room_store_authority_sealed", 409);
          const current = await transaction.get(key);
          if (current) {
            if (!validRecord(current, this.domainId)) return jsonError("room_store_material_corrupt", 503);
            if (!sameRoomStoreMaterial(current, submitted)) return jsonError("room_store_material_conflict", 409);
            return privateJson({ status: "existing", material: current });
          }
          await transaction.put(key, submitted);
          const winner = await transaction.get(key);
          if (!validRecord(winner, this.domainId) || !sameRoomStoreMaterial(winner, submitted) || winner.repositoryId !== null) throw new Error("room store material readback mismatch");
          return privateJson({ status: "created", material: winner }, { status: 201 });
        });
      } catch (_error) {
        return jsonError("room_store_material_write_unverified", 503);
      }
    }
    if (operation === "repository-write" && request.method === "POST") {
      if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return jsonError("room_store_invalid_repository", 400);
      let body;
      try {
        body = await request.json();
      } catch (_error) {
        return jsonError("room_store_invalid_repository", 400);
      }
      if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).sort().join(",") !== "expectedGeneration,repositoryId" || !validRoomStoreRepositoryId(body.repositoryId) || !Number.isSafeInteger(body.expectedGeneration) || body.expectedGeneration < 1 || body.expectedGeneration > 2147483646) return jsonError("room_store_invalid_repository", 400);
      try {
        return await storage.transaction(async transaction => {
          if (await transaction.get("room-store-sealed-v1")) return jsonError("room_store_authority_sealed", 409);
          const current = await transaction.get(key);
          if (!current) return jsonError("room_store_material_missing", 404);
          if (!validRecord(current, this.domainId)) return jsonError("room_store_material_corrupt", 503);
          if (current.repositoryId !== null && current.repositoryId !== body.repositoryId) return jsonError("room_store_repository_conflict", 409);
          if (current.repositoryId === body.repositoryId) {
            if (current.keysetGeneration !== body.expectedGeneration && current.keysetGeneration !== body.expectedGeneration + 1) return jsonError("room_store_generation_conflict", 409);
            return privateJson({ status: "bound", repositoryId: current.repositoryId, keysetGeneration: current.keysetGeneration });
          }
          if (current.keysetGeneration !== body.expectedGeneration) return jsonError("room_store_generation_conflict", 409);
          const candidate = { ...current, repositoryId: body.repositoryId, keysetGeneration: body.expectedGeneration + 1 };
          await transaction.put(key, candidate);
          const winner = await transaction.get(key);
          if (!validRecord(winner, this.domainId) || !sameRoomStoreCiphertext(winner, current) || winner.repositoryId !== body.repositoryId || winner.keysetGeneration !== body.expectedGeneration + 1) throw new Error("room store repository readback mismatch");
          return privateJson({ status: "bound", repositoryId: winner.repositoryId, keysetGeneration: winner.keysetGeneration }, { status: 201 });
        });
      } catch (_error) {
        return jsonError("room_store_repository_write_unverified", 503);
      }
    }
    return jsonError("room_store_not_found", 404);
  }

}
