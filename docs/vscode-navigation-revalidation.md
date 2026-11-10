# VS Code navigation revalidation

Issue #107's audit used a pre-merge checkpoint. The implementation started from
main `2a85f83f8eaa385ade0a4389aa8921ae060dc208`. Its tree still repeated parent
Dimension names, used state as a connection name, hid current Room state and
used recovery point IDs as labels. Room actions were inline, the ready Dimension
context omitted settings and recovery, and the empty tree blocked Welcome.

The current controller uses native Room Store Save for configured R2 and MinIO
Dimensions. Both therefore need the same preview and receipt handling. The
preview and create commands must use the same image selection. The exact
controller-provided deletion token remains the authority for a confirmed plan.
The existing controller remains responsible for capture, encryption and restore.

The audited concerns about Save authority, native R2 receipts and editor buffers
were rechecked in the extension paths. This change keeps the existing binding and
receipt checks, routes configured R2 through native preview and receipt handling,
and keeps editor buffers dirty after Save. Existing link, repair and cold-host
material tests remain regression gates; this document does not assert live
provider or installed-host acceptance. Manual theme, accessibility, remote-host
and screenshot checks require a graphical VS Code host and remain separate from
fixture and source verification.

Release 0.1.39 also reconciles already-open dirty editor buffers with the current
capture policy before a native R2 or MinIO Save. Included unsaved buffers keep
the Room dirty; deliberately excluded files do not prevent a clean Save.
Activation still performs no runtime acquisition or storage access.

## Follow-up on current main

PR #158 merged the navigation and Save presentation changes described above.
Revalidation for #107 starts from main
`f1e77164a41b545060b574dfb07e41043e79fbe2`.

Recovery tree identities now include the parent Room as well as the Dimension
and snapshot. Two Rooms may reference the same snapshot ID without VS Code
confusing their rows. Selection commands still receive the original complete
resource target. Screen-reader labels retain the full row label and state.
Tooltips also retain full IDs; recovery points include their parent Room and Dimension.
This lets narrow sidebars retain readable labels while exposing full identity.

The existing Save, capture, deletion and encryption contracts remain regression
gates. Fixture tests do not close the installed-candidate acceptance requirements
in #107. Fresh Welcome, setup failure/cancellation, ready R2 and MinIO, dirty
states, mixed history, preview/deletion review, recovery/maintenance and JAT
Tools still need screenshots from an installed candidate, bound to its source
SHA, VSIX digest and VS Code version. Light, dark, high contrast, keyboard,
screen-reader and remote/devcontainer behavior require that same host check.
