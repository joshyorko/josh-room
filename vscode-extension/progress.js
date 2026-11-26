const { followLogFile } = require("./registry");

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

function progressDetail(event) {
  const { stage, current, total } = event;
  const hasCounts = Number.isFinite(current) && Number.isFinite(total)
    && total > 0 && current >= 0 && current <= total;
  if (!hasCounts) return undefined;
  if (stage === "upload" || stage === "download") {
    return `${formatBytes(current)} / ${formatBytes(total)}`;
  }
  if (stage === "catalog-restore" || stage === "catalog-save") return `${current} of ${total} Rooms`;
  return undefined;
}

// Use quiet status for routine, short work; callers opt into notification progress explicitly.
function quietProgress(updateStatus, initial, task) {
  updateStatus(initial);
  return Promise.resolve().then(task).finally(() => updateStatus(undefined));
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

module.exports = { followProgressFile, parseProgressLine, progressDetail, quietProgress };
