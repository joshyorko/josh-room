const { followLogFile } = require("./registry");

const PROFILES = {
  save: {
    auth: 8,
    build: "indeterminate",
    jat: "indeterminate",
    package: 48,
    encrypt: 58,
    upload: [60, 92],
    verify: 94,
    catalog: 96,
    complete: 100,
  },
  restore: {
    auth: 8,
    catalog: 15,
    download: [18, 58],
    decrypt: 65,
    verify: 72,
    restore: "indeterminate",
    jat: "indeterminate",
    promote: 96,
    complete: 100,
  },
  remove: { auth: 10, catalog: 65, cleanup: 88, complete: 100 },
  serve: {
    auth: 8,
    catalog: 15,
    download: [18, 55],
    decrypt: 65,
    verify: 72,
    serve: 78,
    jat: "indeterminate",
    complete: 100,
  },
  "jat-build": { build: "indeterminate", jat: "indeterminate", complete: 100 },
  "jat-restore": { restore: "indeterminate", jat: "indeterminate", complete: 100 },
  catalog: { auth: 20, catalog: 85, complete: 100 },
  generic: { complete: 100 },
};

function operationKind(args) {
  if (args[0] === "snapshot" && args[1] === "create") return "save";
  if (args[0] === "hydrate" || args[0] === "enter") return "restore";
  if (args[0] === "rooms" && args[1] === "remove") return "remove";
  if (args[0] === "snapshots" && args[1] === "remove") return "remove";
  if (args[0] === "serve") return "serve";
  if (args[0] === "jat" && args[1] === "build") return "jat-build";
  if (args[0] === "jat" && args[1] === "restore") return "jat-restore";
  if (args[0] === "jat" && args[1] === "serve") return "serve";
  if (args[0] === "projects" || args[0] === "snapshots") return "catalog";
  return "generic";
}

function formatBytes(value) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = Number(value);
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? Math.round(amount) : amount.toFixed(2)} ${units[unit]}`;
}

function renderProgressBar(percent, width = 20) {
  const filled = Math.max(0, Math.min(width, Math.round(Number(percent) * width / 100)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}

function formatTransfer(current, total) {
  const currentDisplay = formatBytes(current);
  const totalDisplay = formatBytes(total);
  const currentParts = currentDisplay.split(" ");
  const totalParts = totalDisplay.split(" ");
  return currentParts[1] === totalParts[1]
    ? `${currentParts[0]} / ${totalDisplay}`
    : `${currentDisplay} / ${totalDisplay}`;
}

function createProgressTracker(kind) {
  const profile = PROFILES[kind] || PROFILES.generic;
  let lastPercent = 0;
  return {
    update(event) {
      let rule = profile[event.stage];
      if (event.stage === "jat" && /completed/i.test(event.message)) rule = 100;
      if (rule === "indeterminate" || rule === undefined) {
        return {
          bar: renderProgressBar(lastPercent),
          indeterminate: true,
          message: event.message,
          percent: undefined,
          transfer: undefined,
        };
      }
      let percent = Array.isArray(rule) ? rule[0] : rule;
      let transfer;
      if (Array.isArray(rule) && Number.isFinite(event.current) && Number.isFinite(event.total) && event.total > 0) {
        const ratio = Math.max(0, Math.min(1, event.current / event.total));
        percent = Math.round(rule[0] + ratio * (rule[1] - rule[0]));
        transfer = formatTransfer(event.current, event.total);
      }
      percent = Math.max(lastPercent, Math.round(percent));
      lastPercent = percent;
      return {
        bar: renderProgressBar(percent),
        indeterminate: false,
        message: event.message,
        percent,
        transfer,
      };
    },
  };
}

const SCANNER_FRAMES = [
  "▓▒░░░░░░▒▓",
  "▒▓▒░░░░▒▓▒",
  "░▒▓▒░░▒▓▒░",
  "░░▒▓▒▒▓▒░░",
  "░░░▒▓▓▒░░░",
  "░░▒▓▒▒▓▒░░",
  "░▒▓▒░░▒▓▒░",
  "▒▓▒░░░░▒▓▒",
];

function renderStatusBar(percent, frame = 0, width = 10) {
  if (percent === undefined) return SCANNER_FRAMES[frame % SCANNER_FRAMES.length];
  const filled = Math.max(0, Math.min(width, Math.round(Number(percent) * width / 100)));
  if (filled === width) return "█".repeat(width);
  const empty = width - filled;
  const pulse = frame % empty;
  return "█".repeat(filled) + "░".repeat(pulse) + "◆" + "░".repeat(empty - pulse - 1);
}

function formatElapsed(elapsedMs) {
  const seconds = Math.max(0, Math.floor(Number(elapsedMs || 0) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatProgressDisplay(title, _kind, state, frame = 0, elapsedMs) {
  const percent = state.indeterminate || state.percent === undefined ? "…" : `${state.percent}%`;
  const transfer = state.transfer ? ` · ${state.transfer}` : "";
  const detail = `${state.message}${transfer}`;
  const elapsed = elapsedMs === undefined ? "" : ` · elapsed ${formatElapsed(elapsedMs)}`;
  const statusBar = renderStatusBar(state.indeterminate ? undefined : state.percent, frame);
  return {
    logLine: `${state.bar} ${percent} · ${detail}`,
    notification: `${detail} · ${percent}${elapsed}`,
    statusText: `$(sync~spin) ${statusBar} ${percent === "…" ? "" : `${percent} `}${title}${elapsedMs === undefined ? "" : ` · ${formatElapsed(elapsedMs)}`}`,
    tooltip: `${title}\n${state.bar} ${percent}\n${detail}${elapsed}\nFull details: Output → Josh Room`,
  };
}

function parseProgressLine(line) {
  try {
    const event = JSON.parse(line);
    if (
      event?.format_version !== 1
      || typeof event.stage !== "string"
      || typeof event.message !== "string"
    ) return undefined;
    return event;
  } catch (_error) {
    return undefined;
  }
}

function followProgressFile(progressPath, onEvent, options = {}) {
  return followLogFile(progressPath, (line) => {
    const event = parseProgressLine(line);
    if (event) onEvent(event);
  }, options);
}

module.exports = {
  createProgressTracker,
  followProgressFile,
  formatBytes,
  formatElapsed,
  formatProgressDisplay,
  operationKind,
  parseProgressLine,
  renderProgressBar,
  renderStatusBar,
};

Object.assign(PROFILES, {
  dimension: { auth: 8, catalog: 35, configure: 72, complete: 100 },
  link: { status: 18, catalog: 48, verify: 82, complete: 100 },
  repair: { status: 18, catalog: 48, verify: 82, repair: 94, complete: 100 },
  copy: { auth: 8, catalog: 22, download: [25, 70], verify: 84, upload: [85, 96], complete: 100 },
});

const baseOperationKind = operationKind;
function nativeOperationKind(args) {
  if (args[0] === "dimensions") return "dimension";
  if (args[0] === "status" || args[0] === "link") return args[0] === "link" ? "link" : "catalog";
  if (args[0] === "repair") return "repair";
  if (args[0] === "snapshot" && args[1] === "copy") return "copy";
  return baseOperationKind(args);
}

module.exports.operationKind = nativeOperationKind;
