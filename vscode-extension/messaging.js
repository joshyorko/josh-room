"use strict";

function notifyDone(vscode, message, options = {}) {
  const actions = options.action ? [options.action] : [];
  return vscode.window.showInformationMessage(message, ...actions);
}

function notifyError(vscode, action, safeNextStep, options = {}) {
  const message = `${action} failed. ${safeNextStep}`;
  const actions = [];
  if (options.retry && options.idempotent === true) actions.push("Retry");
  actions.push("Show Logs");
  return vscode.window.showErrorMessage(message, ...actions);
}

function confirmDestructive(vscode, verb, subject) {
  return vscode.window.showWarningMessage(`${verb} ${subject}?`, { modal: true }, verb);
}

module.exports = { notifyDone, notifyError, confirmDestructive };
