"use strict";
const http = require("node:http");
const net = require("node:net");

function request(url, method = "GET", capture = false) {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method, agent: false, timeout: 5000 }, response => {
      let body = "";
      response.on("data", chunk => {
        if (capture) {
          body += chunk;
          if (body.length > 2 * 1024 * 1024) req.destroy(new Error("Fixture state exceeded probe limit"));
        }
      });
      response.on("end", () => resolve({ status: response.statusCode, body }));
    });
    req.on("timeout", () => req.destroy(new Error("Fixture probe deadline exceeded")));
    req.on("error", () => reject(new Error("Fixture viewer endpoint unavailable")));
    req.end();
  });
}

async function probe(url) {
  const root = await request(url);
  const unauthenticated = await request(new URL("/", url));
  const post = await request(url, "POST");
  const stateUrl = new URL(url); stateUrl.pathname = "/state.json";
  const state = await request(stateUrl, "GET", true);
  const payload = JSON.parse(state.body);
  const listing = new URL(url);
  listing.pathname = "/fs/list";
  listing.searchParams.set("id", "root");
  let hasFixture = false;
  if (payload.server?.filesystem === true) {
    for (let attempt = 0; attempt < 40; attempt++) {
      const response = await request(listing, "GET", true);
      if (response.status !== 200) break;
      const page = JSON.parse(response.body);
      const file = page.entries.find(entry => entry.label === "example.txt");
      if (file) {
        const preview = new URL(url);
        preview.pathname = "/fs/preview";
        preview.searchParams.set("id", file.id);
        preview.searchParams.set("generation", page.generation);
        const result = await request(preview, "GET", true);
        hasFixture = result.status === 200 && JSON.parse(result.body).node?.previews?.some(
          slot => slot.status === "available" && typeof slot.body === "string"
        );
        break;
      }
      if (!page.cursor) break;
      listing.searchParams.set("generation", page.generation);
      listing.searchParams.set("cursor", page.cursor);
    }
  }
  if (root.status !== 200 || unauthenticated.status !== 403 || post.status !== 405 || state.status !== 200 || !hasFixture) {
    throw new Error("Installed viewer failed root/state/authentication/method fixture checks");
  }
  return { root: root.status, state: state.status, hasFixture, unauthenticated: unauthenticated.status, post: post.status };
}

async function isStopped(url) {
  try { return (await request(url)).status >= 400; } catch { return true; }
}

function isListenerStopped(url) {
  const endpoint = new URL(url);
  return new Promise(resolve => {
    const socket = net.connect({ host: endpoint.hostname, port: Number(endpoint.port) });
    const finish = stopped => { socket.destroy(); resolve(stopped); };
    socket.once("connect", () => finish(false));
    socket.once("error", error => finish(error.code === "ECONNREFUSED"));
    socket.setTimeout(2000, () => finish(false));
  });
}
module.exports = { probe, isStopped, isListenerStopped };
