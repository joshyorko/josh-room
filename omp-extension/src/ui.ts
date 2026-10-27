/**
 * Native OMP presentation for Josh Room. Rich components come from OMP's public packages through
 * {@link TuiKit} (wired in index.ts), so the routing logic stays testable without the host runtime.
 */

export interface TuiComponent {
	render(width: number): readonly string[];
	invalidate(): void;
	handleInput?(data: string): void;
	dispose?(): void;
}

export interface PickerItem {
	value: string;
	label: string;
	description?: string;
}

export interface TuiTheme {
	fg(color: "accent" | "border" | "dim" | "muted" | "success" | "warning" | "error" | "toolTitle", text: string): string;
	bold(text: string): string;
}

interface Keybindings {
	matches(data: string, action: string): boolean;
}

interface SelectListComponent extends TuiComponent {
	onSelect?: (item: PickerItem) => void;
	onCancel?: () => void;
}

interface LoaderComponent extends TuiComponent {
	readonly signal: AbortSignal;
}

/** Public OMP TUI primitives: `@oh-my-pi/pi-tui` and `@oh-my-pi/pi-coding-agent` exports. */
export interface TuiKit {
	Container: new () => TuiComponent & { addChild(child: TuiComponent): void };
	Text: new (text: string, paddingX?: number, paddingY?: number) => TuiComponent;
	SelectList: new (items: PickerItem[], maxVisible: number, theme: unknown) => SelectListComponent;
	DynamicBorder: new (color?: (text: string) => string) => TuiComponent;
	BorderedLoader: new (tui: unknown, theme: TuiTheme, message: string) => LoaderComponent;
	getSelectListTheme(): unknown;
	truncateToWidth(text: string, width: number): string;
}

type CustomFactory<T> = (tui: unknown, theme: TuiTheme, keybindings: Keybindings, done: (result: T) => void) => TuiComponent;

export interface UiHost {
	mode?: string;
	hasUI: boolean;
	ui: {
		select(title: string, options: Array<{ label: string; description?: string }>): Promise<string | undefined>;
		notify(message: string, type?: "info" | "warning" | "error"): void;
		setWorkingMessage(message?: string): void;
		custom?<T>(factory: CustomFactory<T>): Promise<T>;
	};
}

const PICKER_HINT = "↑↓ navigate · enter select · esc close";
const MAX_VISIBLE_ROWS = 10;

/** Rich focused components need the interactive TUI; RPC/ACP/headless hosts get dialogs or plain output. */
export function supportsRichUi(ctx: UiHost): boolean {
	return ctx.hasUI && ctx.mode === "tui" && typeof ctx.ui.custom === "function";
}

function createPicker(kit: TuiKit, theme: TuiTheme, keybindings: Keybindings, title: string, items: PickerItem[], done: (value: string | undefined) => void): TuiComponent {
	let settled = false;
	const finish = (value: string | undefined) => {
		if (settled) return;
		settled = true;
		done(value);
	};
	const border = (text: string) => theme.fg("border", text);
	const list = new kit.SelectList(items, Math.min(items.length, MAX_VISIBLE_ROWS), kit.getSelectListTheme());
	list.onSelect = (item) => finish(item.value);
	list.onCancel = () => finish(undefined);
	const container = new kit.Container();
	container.addChild(new kit.DynamicBorder(border));
	container.addChild(new kit.Text(theme.fg("accent", theme.bold(title)), 1, 0));
	container.addChild(list);
	container.addChild(new kit.Text(theme.fg("dim", PICKER_HINT), 1, 0));
	container.addChild(new kit.DynamicBorder(border));
	return {
		render: (width) => container.render(width).map((line) => kit.truncateToWidth(line, width)),
		invalidate: () => container.invalidate(),
		handleInput: (data) => {
			if (keybindings?.matches(data, "app.interrupt")) {
				finish(undefined);
				return;
			}
			list.handleInput?.(data);
		},
	};
}

/**
 * Keyboard-first selector: a native SelectList overlay in the TUI, the host's `select` dialog elsewhere.
 * Resolves to the chosen item's value, or `undefined` when cancelled or when no UI exists.
 */
export async function pick(ctx: UiHost, kit: TuiKit | undefined, title: string, items: PickerItem[]): Promise<string | undefined> {
	if (!ctx.hasUI || items.length === 0) return undefined;
	if (kit && supportsRichUi(ctx) && ctx.ui.custom) {
		return ctx.ui.custom<string | undefined>((_tui, theme, keybindings, done) => createPicker(kit, theme, keybindings, title, items, done));
	}
	// Host dialogs return the chosen label, so duplicate labels (e.g. same-named Rooms) are made unique.
	const used = new Set<string>();
	const labels = items.map(({ label }) => {
		let unique = label;
		for (let n = 2; used.has(unique); n++) unique = `${label} (${n})`;
		used.add(unique);
		return unique;
	});
	const selected = await ctx.ui.select(title, items.map(({ description }, index) => (description ? { label: labels[index], description } : { label: labels[index] })));
	const index = selected === undefined ? -1 : labels.indexOf(selected);
	return index < 0 ? undefined : items[index].value;
}

/**
 * Run a long Josh Room operation with native progress. In the TUI a bordered loader owns Escape and aborts
 * the signal; the result is reported only after the owned child process has settled. Elsewhere the working
 * message is shown and restored on every exit path.
 */
export async function withProgress<T>(ctx: UiHost, kit: TuiKit | undefined, message: string, work: (signal: AbortSignal | undefined) => Promise<T>): Promise<T> {
	if (kit && supportsRichUi(ctx) && ctx.ui.custom) {
		const settled = await ctx.ui.custom<{ ok: true; value: T } | { ok: false; error: unknown }>((tui, theme, _keybindings, done) => {
			const loader = new kit.BorderedLoader(tui, theme, message);
			work(loader.signal).then((value) => done({ ok: true, value }), (error: unknown) => done({ ok: false, error }));
			return loader;
		});
		if (settled.ok) return settled.value;
		throw settled.error;
	}
	ctx.ui.setWorkingMessage(message);
	try {
		return await work(undefined);
	} finally {
		ctx.ui.setWorkingMessage();
	}
}
