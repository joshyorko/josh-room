function shouldMarkDirty(relativePath) {
  const normalized = String(relativePath).replaceAll("\\", "/").replace(/^\.\//, "");
  if (!normalized || normalized === ".." || normalized.startsWith("../")) return false;
  if (normalized === ".josh-room.json" || normalized === ".DS_Store") return false;
  if (normalized === ".git" || normalized.startsWith(".git/")) return false;
  if (normalized === ".pytest_cache" || normalized.startsWith(".pytest_cache/")) return false;
  if (normalized === ".ruff_cache" || normalized.startsWith(".ruff_cache/")) return false;
  if (normalized.includes("/__pycache__/") || normalized.startsWith("__pycache__/")) return false;
  if (normalized.includes("node_modules/.cache/")) return false;
  return true;
}

module.exports = { shouldMarkDirty };
