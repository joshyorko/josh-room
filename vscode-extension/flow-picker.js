"use strict";

const BACK = Symbol("back");
const CANCEL = Symbol("cancel");

function setup(vscode, input, options, value) {
  input.title = options.title;
  input.step = options.step;
  input.totalSteps = options.totalSteps;
  input.ignoreFocusOut = true;
  if (options.step > 1) input.buttons = [vscode.QuickInputButtons.Back];
  return new Promise(resolve => {
    let chosen = CANCEL;
    input.onDidAccept(async () => {
      const selection = value();
      if (options.validateInput) {
        input.validationMessage = await options.validateInput(selection);
        if (input.validationMessage) return;
      }
      if (selection === undefined) return;
      chosen = selection;
      input.hide();
    });
    input.onDidTriggerButton(button => {
      if (button === vscode.QuickInputButtons.Back) {
        chosen = BACK;
        input.hide();
      }
    });
    input.onDidHide(() => {
      input.dispose();
      resolve(chosen);
    });
    input.show();
  });
}

async function flowPick(vscode, items, options) {
  if (!vscode.QuickInputButtons?.Back || !vscode.window.createQuickPick) {
    return (await vscode.window.showQuickPick(items, { ...options, ignoreFocusOut: true })) ?? CANCEL;
  }
  const picker = vscode.window.createQuickPick();
  picker.items = items;
  picker.placeholder = options.placeHolder;
  picker.canSelectMany = Boolean(options.canPickMany);
  if (options.selected) picker.selectedItems = options.selected;
  return setup(vscode, picker, options, () => options.canPickMany ? picker.selectedItems : picker.selectedItems[0]);
}

async function flowInput(vscode, options) {
  if (!vscode.QuickInputButtons?.Back || !vscode.window.createInputBox) {
    return (await vscode.window.showInputBox({ ...options, ignoreFocusOut: true })) ?? CANCEL;
  }
  const input = vscode.window.createInputBox();
  input.value = options.value || "";
  input.prompt = options.prompt;
  input.placeholder = options.placeHolder;
  input.password = Boolean(options.password);
  return setup(vscode, input, options, () => input.value);
}

module.exports = { BACK, CANCEL, flowPick, flowInput };
