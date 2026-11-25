"use strict";

const ROOMS_VIEW = "joshRoom.rooms";
const COMPLETION_MS = 4000;
const CONTEXT_PHASES = {
  save: "Saving…",
  restore: "Restoring…",
  delete: "Deleting…",
  check: "Checking changes…",
};
const COMPLETED = { save: "Saved", restore: "Restored" };
const OUTCOME = { done: "Completed", cancelled: "Cancelled", failed: "Failed" };

function targetsMatch(target, row) {
  return target?.dimension && row?.dimension && target.dimension === row.dimension
    && target.namespace === row.namespace
    && (target.room || target.path
      ? (target.room && target.room === row.room) || (target.path && target.path === row.path)
      : target.namespace && target.namespace === row.namespace)
    && (!target.snapshot || !row.snapshot || target.snapshot === row.snapshot);
}

function childIndex(operation, row) {
  if (operation.target?.dimension !== row.dimension || operation.target?.namespace !== row.namespace
      || !row.namespace || !row.path) return -1;
  return operation.children?.indexOf(row.path) ?? -1;
}

class RoomOperations {
  constructor(vscode, updateStatus, updateRows) {
    this.vscode = vscode;
    this.updateStatus = updateStatus;
    this.updateRows = updateRows;
    this.active = [];
    this.last = undefined;
    this.settled = [];
  }

  get foreground() {
    return this.active.find(row => !row.quiet) || this.active[0];
  }

  tick(updateRows = false) {
    const retained = this.settled.filter(row => Date.now() - row.finished < COMPLETION_MS);
    if (retained.length !== this.settled.length) updateRows = true;
    this.settled = retained;
    if (updateRows) this.updateRows();
    this.updateStatus(this.foreground);
    if (this.active.length || this.settled.length) {
      if (!this.clock) {
        this.clock = setInterval(() => this.tick(), 1000);
        this.clock.unref?.();
      }
    } else {
      clearInterval(this.clock);
      this.clock = undefined;
    }
  }

  async run(options, task) {
    const { vscode } = this;
    const { operation = {}, retry, title, cancellable } = options;
    const cancellation = cancellable && vscode.CancellationTokenSource
      ? new vscode.CancellationTokenSource() : undefined;
    const row = { ...operation, title: title.replace(/[.…]+$/, ""), started: Date.now(), state: "running", retry };
    row.cancel = cancellation && (() => cancellation.cancel());
    this.active.unshift(row);
    this.tick(Boolean(row.target));
    try {
      const result = await vscode.window.withProgress(
        { title, location: { viewId: ROOMS_VIEW }, cancellable: !cancellation && cancellable },
        (progress, token) => task({
          report: ({ message, detail, index }) => {
            if (!message) return;
            const nextIndex = index ?? row.index;
            const changedRow = row.phase !== message || row.index !== nextIndex;
            if (!changedRow && row.detail === detail) return;
            row.phase = message;
            row.detail = detail;
            row.index = nextIndex;
            progress.report({ message });
            this.tick(Boolean(row.target) && changedRow);
          },
        }, cancellation?.token || token));
      row.state = cancellation?.token.isCancellationRequested || result === "cancelled" || result?.status === "cancelled" ? "cancelled"
        : result?.status === "partial" || result?.ok === false ? "failed" : "done";
      return result;
    } catch (error) {
      row.error = error;
      row.state = cancellation?.token.isCancellationRequested || error?.code === "ABORT_ERR"
        || error?.name === "AbortError" ? "cancelled" : "failed";
      throw error;
    } finally {
      row.finished = Date.now();
      cancellation?.dispose();
      delete row.cancel;
      this.active = this.active.filter(current => current !== row);
      if (!row.quiet || row.state === "failed") this.last = row;
      if (row.target && row.state === "done" && COMPLETED[row.verb]) this.settled.push(row);
      this.tick(Boolean(row.target));
    }
  }

  decoration(row) {
    if (!row) return undefined;
    for (const operation of [...this.active, ...this.settled]) {
      const index = childIndex(operation, row);
      const matches = targetsMatch(operation.target, row);
      if (!matches && index === -1) continue;
      if (operation.state === "done") return { icon: "check", description: COMPLETED[operation.verb] };
      if (index !== -1 && !matches) {
        const current = operation.index ?? -1;
        if (index > current) return { icon: "clock", description: "Queued" };
        if (index < current) return { icon: "check", description: COMPLETED[operation.verb] };
      }
      return { icon: "loading~spin", description: operation.phase || CONTEXT_PHASES[operation.verb] || "Working…" };
    }
  }

  noteOutcome({ target, receipt, folder }) {
    const operation = this.last;
    if (operation?.state !== "done" || !targetsMatch(operation.target, target)) return undefined;
    if (receipt) operation.receipt = receipt;
    if (folder) operation.folder = folder;
    return operation;
  }

  async showMenu({ showLog, showRooms, openFolder, copyReceipt, saveCurrent }) {
    const running = this.foreground;
    const operation = running?.quiet && this.last ? this.last : running || this.last;
    const items = [];
    if (running?.cancel) {
      items.push({ label: "$(debug-stop) Cancel operation", action: () => running.state === "running" && running.cancel?.() });
    }
    if (operation?.state === "failed" && operation.retry) {
      items.push({ label: "$(refresh) Retry", action: () => operation.retry() });
    }
    if (operation?.folder) items.push({ label: "Open Restored Folder", action: () => openFolder(operation.folder) });
    if (operation?.receipt) items.push({ label: "Copy Recovery Receipt", action: () => copyReceipt(operation.receipt) });
    if (saveCurrent && !running) items.push({ label: "Save current Room", action: saveCurrent });
    items.push({ label: "Show Rooms", action: showRooms }, { label: "Show Output Log", action: showLog });
    const elapsed = operation && Math.floor(((operation.finished || Date.now()) - operation.started) / 1000);
    const detail = operation && [OUTCOME[operation.state] || operation.phase, operation.state === "running" && operation.detail, `${elapsed}s`]
      .filter(Boolean).join(" · ");
    const choice = await this.vscode.window.showQuickPick(items, {
      title: operation?.title || "Josh Room", placeHolder: detail || "Choose an action",
    });
    return choice?.action();
  }

  dispose() {
    clearInterval(this.clock);
    for (const row of this.active) row.cancel?.();
  }
}

module.exports = { RoomOperations, ROOMS_VIEW };
