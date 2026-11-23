# Josh Room

Native Save, Enter, Serve, and snapshot controls for Josh's disposable Rooms.

The VSIX bootstraps its pinned RCC runtime and uses a private VS Code global
storage home. It acquires the separate JAT Environment Artifact, whose
RCC-built Holotree owns Hauler. It does not require a global Josh Room CLI,
Homebrew, host Hauler, Action Server, or Room of Requirement.

In the Rooms view, choose Connect Storage or Load Existing Storage to begin.
The current Room displays its save state. Use Save Current Room to save its
bound folder, or Save Folder As Room to choose a folder and destination. The
New Room flow creates missing onboarding files in the selected source before
preview. It creates a root `AGENTS.md` by default and can create the selected
VS Code files based on local architecture evidence. Configure
`joshRoom.onboarding.editor` (`vscode` or `none`) and
`joshRoom.onboarding.files` in VS Code Settings; the guide is still generated
when editor mode is `none`. Initialization does not install extensions or run
tasks. The first-run confirmation shows whether discovery is partial, its
observed component/directory counts and main limits, and directs you to the
guide's evidence. Continue remains available; Save still requires its separate
workspace safety check. Existing guides are preserved. See the
[onboarding policy](https://github.com/joshyorko/josh-room/blob/main/docs/workspace-capture-policy.md#initialize-room-onboarding-inspect-and-approve-exclusions)
for discovery bounds and explicit manual replacement of an incomplete guide.
The preview shows the capture scope and any deletions requiring confirmation. Save
open editor buffers first; otherwise the Room remains marked as having changes.
Recovery points show Latest and Open separately; the tooltip contains the full
ID. Settings, recovery and maintenance are available from the relevant
Dimension's context menu. Show Details opens the sanitized Save receipt.

`Josh: Split Workspace into Rooms (Preview Only)` offers an explicit folder
checkbox preview. It does not save, exclude or unload children. Publication is
blocked pending pin retention and crash-consistent orchestration; see the
[composition contract](https://github.com/joshyorko/josh-room/blob/main/docs/room-composition.md).

## Explore an entered Room in 3D

In desktop VS Code with a Linux workspace extension host, right-click the open Room row and select **Explore Room
in 3D…**, or use **Josh: Explore Room in 3D…** in the Command Palette. No global
CLI, Save, or new Logical JAT is required. **Josh: Stop Room 3D Viewer** stops
the session; closing only the browser tab does not. Windows desktop clients can
use a Linux devcontainer/attached-container/SSH workspace through VS Code's
private loopback forwarding (`remote.localPortHost: localhost`). Native Windows
controllers, web hosts and public forwarding URLs remain unsupported. See
[Room View](https://github.com/joshyorko/josh-room/blob/main/docs/room-view.md) for lifecycle and security limits.
