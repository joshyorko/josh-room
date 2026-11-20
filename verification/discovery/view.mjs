export const title = "Discovery: validate all siblings before entering a child";
export const state = (v) => {
  const alias = (name) => name === "SAFE" ? "safe" : name;
  const collision = (names) => names.some((name) => v.entries.filter((entry) => alias(entry) === alias(name)).length > 1);
  if (v.opened.length && (v.rootState !== "validated" || collision(v.opened))) return "Unsafe child entered";
  if (v.offered.length && (v.rootState !== "validated" || collision(v.offered))) return "Unsafe choices offered";
  if (v.paused) return "Paused";
  if (v.rootState === "finalizing" && Object.values(v.pending).includes("queued")) return "Child queued; parent not complete";
  if (v.offered.length) return "Safe choices available";
  if (v.opened.length) return "Validated child entered";
  return ({ reading: "Reading all sibling names", finalizing: "Checking aliases", validated: "Parent validated", failed: "Scope inaccessible" })[v.rootState];
};
export const step = (action) => ({
  ReadEntry: "observe a sibling", EndRead: "all names observed", ReadFailure: "directory read fails",
  FinalizeChild: "admit or reject a child", FinishParent: "alias checks finish", OpenChild: "enter child",
  OfferChild: "offer choices", Pause: "pause", Resume: "resume",
})[action];
