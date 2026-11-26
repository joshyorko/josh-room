# Josh Room in VS Code

The extension calls the destination **Storage** and a saved version a
**Recovery Point**. A **Catalog** keeps eligible files and Git history together
unless you split a folder into a **Child Room**. **JAT Tools** contains local
Haul operations.

## Rooms sidebar

The open workspace appears first when it is bound to a Room. Use its Save
button to capture changes. On a Catalog, the Child Rooms appear directly
beneath it. Use the restore button when a child is missing. A changed row
uses your theme's modified-file color.

Storage rows show the provider and whether it needs attention. Expand Storage
to find Catalogs and Rooms. Recovery Points show their local time, with the
exact time and identifiers in the tooltip. The context menu groups inspection,
export, history and deletion by purpose. Deletion requires an explicit review.

## Progress and decisions

The status bar shows the current Room. Click it for the current operation and
available actions such as cancellation, logs or a recovery receipt. Routine
progress stays in the Rooms view and affected rows. The extension uses a
notification when you need to act on a result or resolve a failure.

Catalog Save opens a step-by-step review with Back navigation. By default it
saves eligible files and Git history together. Use the optional folder picker
when a folder needs independent recovery. Restore Child Rooms uses native
checkboxes and a review of the selected folders.

Use **Get started with Josh Room** in the VS Code walkthroughs to connect
Storage, set up encryption and save your first Room and Catalog.
