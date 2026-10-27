# Josh Room

Native Save, Enter, Serve, and snapshot controls for Josh's disposable Rooms.

The VSIX bootstraps its pinned RCC runtime and uses a private VS Code global
storage home. It acquires the separate JAT Environment Artifact, whose
RCC-built Holotree owns Hauler. It does not require a global Josh Room CLI,
Homebrew, host Hauler, Action Server, or Room of Requirement.

In the Rooms view, choose Connect Storage or Load Existing Storage to begin.
The current Room displays its save state. Use Save Current Room to save its
bound folder, or Save Folder As Room to choose a folder and destination. The
preview shows the capture scope and any deletions requiring confirmation. Save
open editor buffers first; otherwise the Room remains marked as having changes.
Recovery points show Latest and Open separately; the tooltip contains the full
ID. Settings, recovery and maintenance are available from the relevant
Dimension's context menu. Show Details opens the sanitized Save receipt.
