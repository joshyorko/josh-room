import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { registerRoomCommand } from "./commands.ts";
import { roomController } from "./controller.ts";
import { registerRoomLifecycle } from "./lifecycle.ts";
import { registerRoomTools } from "./tools.ts";

export default function joshRoomExtension(pi: ExtensionAPI): void {
	registerRoomCommand(pi, roomController);
	registerRoomTools(pi, roomController);
	registerRoomLifecycle(pi, roomController);
}
