"use strict";

const VIEW_ID = "joshRoom.activity";

class ActivityProvider {
  constructor(vscode, updateStatus) {
    this.vscode = vscode;
    this.updateStatus = updateStatus;
    this.events = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.events.event;
    this.operations = [];
    this.sequence = 0;
    this.clock = undefined;
  }

  refresh(operation) {
    const active = this.operations.filter(row => row.state === "running");
    if (this.view) this.view.badge = active.length
      ? { value: active.length, tooltip: `${active.length} running Josh Room operations` } : undefined;
    this.events.fire(operation);
    if (active.length) {
      this.updateStatus(active[0]);
      if (!this.clock) {
        this.clock = setInterval(() => this.refresh(), 1000);
        this.clock.unref?.();
      }
    } else {
      clearInterval(this.clock);
      this.clock = undefined;
      this.updateStatus(undefined);
    }
  }

  async run(options, task) {
    const { vscode } = this;
    const cancellation = options.cancellable && vscode.CancellationTokenSource
      ? new vscode.CancellationTokenSource() : undefined;
    const operation = { id: String(++this.sequence), label: options.title.replace(/[.…]+$/, ""),
      state: "running", started: Date.now(), steps: [], cancellation, retry: options.retry, lastUpdate: Date.now() };
    this.operations.unshift(operation);
    this.operations = this.operations.filter((row, index) => index < 10 || row.state === "running");
    this.refresh();
    try {
      const result = await vscode.window.withProgress({ ...options, location: { viewId: VIEW_ID }, cancellable: false },
        (progress, token) => task({
          report: ({ message, detail }) => {
            if (!message) return;
            const last = operation.steps.at(-1);
            if (last?.label === message) {
              if (last.detail === detail) return;
              last.detail = detail;
            } else {
              if (last) last.finished = Date.now();
              operation.steps.push({ id: `${operation.id}:${++this.sequence}`, label: message, detail,
                operation, started: Date.now() });
              if (operation.steps.length > 24) operation.steps.shift();
            }
            operation.lastUpdate = Date.now();
            progress.report({ message });
            this.refresh(operation);
          },
        }, cancellation?.token || token));
      operation.state = cancellation?.token.isCancellationRequested ? "cancelled" : "done";
      return result;
    } catch (error) {
      operation.state = cancellation?.token.isCancellationRequested || error?.code === "ABORT_ERR"
        || error?.name === "AbortError" ? "cancelled" : "failed";
      throw error;
    } finally {
      operation.finished = Date.now();
      const last = operation.steps.at(-1);
      if (last) last.finished = operation.finished;
      cancellation?.dispose();
      operation.cancellation = undefined;
      this.refresh();
    }
  }

  getChildren(operation) {
    return operation ? operation.steps || [] : this.operations;
  }

  getTreeItem(row) {
    const { vscode } = this;
    const item = new vscode.TreeItem(row.label, row.steps?.length
      ? row.state === "running" ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.Collapsed
      : vscode.TreeItemCollapsibleState.None);
    item.id = row.id;
    const elapsed = Math.floor(((row.finished || Date.now()) - row.started) / 1000);
    if (row.operation) {
      const isLast = row.operation.steps.at(-1) === row;
      const phase = isLast ? row.operation.state : "done";
      item.iconPath = new vscode.ThemeIcon({ running: "loading~spin", done: "check", failed: "error", cancelled: "circle-slash" }[phase]);
      item.description = [row.detail, `${elapsed}s`].filter(Boolean).join(" · ");
      item.accessibilityInformation = { label: `${row.label}. ${item.description}` };
      return item;
    }
    item.iconPath = new vscode.ThemeIcon({
      running: "loading~spin", done: "check", failed: "error", cancelled: "circle-slash",
    }[row.state]);
    const states = { running: "Running", done: "Completed", failed: "Failed", cancelled: "Cancelled" };
    const quiet = Math.floor((Date.now() - row.lastUpdate) / 1000);
    const waiting = row.state === "running" && quiet >= 30 ? ` · No update for ${quiet}s` : "";
    item.description = `${states[row.state]} · ${elapsed}s${waiting}`;
    item.accessibilityInformation = { label: `${row.label}. ${item.description}` };
    item.contextValue = row.cancellation ? "activity-cancellable"
      : row.state === "failed" && row.retry ? "activity-retry" : "activity-operation";
    return item;
  }

  dispose() {
    clearInterval(this.clock);
    for (const row of this.operations) row.cancellation?.cancel();
    this.events.dispose?.();
  }
}

module.exports = { ActivityProvider, VIEW_ID };
