const { followLogFile } = require("./registry");

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

module.exports = { followProgressFile, parseProgressLine };
