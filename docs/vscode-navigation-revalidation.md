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
