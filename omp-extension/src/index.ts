import { BorderedLoader, DynamicBorder, getSelectListTheme, type ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { Container, SelectList, Text, truncateToWidth } from "@oh-my-pi/pi-tui";
import { registerRoomCommand } from "./commands.ts";
import { roomController } from "./controller.ts";
import { registerRoomLifecycle } from "./lifecycle.ts";
import { RoomSessionState } from "./state.ts";
import { registerRoomTools } from "./tools.ts";
import type { TuiKit } from "./ui.ts";

// Public OMP package exports only; the host resolves these to its bundled copies at load time.
const kit = { Container, SelectList, Text, DynamicBorder, BorderedLoader, getSelectListTheme, truncateToWidth } as unknown as TuiKit;

export default function joshRoomExtension(pi: ExtensionAPI): void {
	// Factories are rebound per session, so Room presentation state stays session-local.
	const state = new RoomSessionState();
	registerRoomCommand(pi, roomController, state, kit);
	registerRoomTools(pi, roomController, state, kit);
	registerRoomLifecycle(pi, roomController, state);
}
