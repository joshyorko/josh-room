export const state = v => v.phase === "ready" ? (v.closed ? "Lease closed" : "Ready for a bound request")
  : v.phase === "pending" ? (v.closed ? "Queued request after closure" : v.requestObservation !== v.observation ? "Queued request after new observation" : "Bound request pending")
  : v.phase === "rejected" ? "Stale request rejected" : v.badDispatch ? "Wrong context dispatched" : "Current request dispatched";
export const step = action => ({Queue:"Explorer submits a bound request", Observe:"Collector retains a new observation", Close:"Owner closes the session", Dispatch:"Receiver checks immediately before dispatch", Done:"Stopped"})[action];
export const title = "Single-owner explorer receiver fence";
