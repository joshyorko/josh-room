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

function requestDecision(vscode, message, ...actions) {
  return vscode.window.showInformationMessage(message, ...actions);
}

function notifyFailure(vscode, message, { retry = false, logs = false } = {}) {
  return vscode.window.showErrorMessage(message, ...(retry ? ["Retry"] : []), ...(logs ? ["Show Logs"] : []));
}

function confirmDestructive(vscode, verb, subject, detail) {
  return vscode.window.showWarningMessage(`${verb} ${subject}?`, { modal: true, ...(detail ? { detail } : {}) }, verb);
}

module.exports = { notifyDone, notifyError, notifyFailure, requestDecision, confirmDestructive };
