# Issue 165 deletion evidence

The regression came from #158's navigation change. It moved `joshRoom.remove`
from the inline menu group to the Room context group, and its test explicitly
required that placement. Individual recovery-point rows had no deletion action.
The existing Windows packaged gate exercised Check Workspace, not deletion or
post-deletion navigation. Those checks could pass without proving discoverable
trash controls or fresh catalog state. Deletion also called cached `refresh()`;
that could leave a removed row visible until an authoritative reload.

The repair restores the inline action and adds distinct recovery-point and
whole-Room commands. Deletion remains a controller operation with an exact
Dimension, catalog revision, and ordered inventory. An invalid workspace marker
blocks deletion, including when the binding changes while confirmation is open.
Unknown or incomplete controller receipts never become a success message;
committed cleanup-pending results remain distinct and still reload the catalog.

## Historical captures

These files retain their original pixels and source provenance. They do not
qualify a newer package simply because its version changed.

| Files | Evidence |
| --- | --- |
| 01–03, 05 | Genuine VS Code renderer captures using a modified disposable candidate copy, an in-memory catalog, and stubbed controller calls. Presentation only. |
| 04 | Unmodified 0.1.40 VSIX with disposable loopback MinIO. The actual Delete Room warning was dismissed with Escape; the catalog remained unchanged. |
| 06 | Unmodified 0.1.40 VSIX after the real controller deleted one disposable MinIO recovery point; a fresh list retained the previous point as Latest. |

`metadata.json` names the historical candidate SHA and VSIX digest. Its fixture
harness hash refers to that historical source, not the subsequently repaired
hosted harness. `real-minio-controller-receipt.json` records the separate
controller operations and cleanup. No user or external provider data was used.

## Hosted acceptance

`issue-165-windows-ui-capture.yml` first installs and qualifies the unmodified
VSIX with the existing native Check Workspace harness. It then prepares an
explicitly modified disposable copy for presentation checks. The original VSIX
and public command manifest stay unchanged. Activation seeds the catalog only;
it performs no automatic deletion.

The presentation gate drives the actual menus and production deletion handlers,
checks confirmation scope and cancellation, and confirms scoped point removal,
Latest promotion, distinct Room removal, and the handlers' catalog reloads.
Only its controller boundaries use in-memory fixture state. This is not a live
provider or unmodified-package deletion claim. The workflow uploads separate
pristine-install and presentation receipts, raw renderer captures, measured
control rectangles, and copies annotated after capture with bounding boxes.

The earlier hosted attempts failed because they did not seed the required
catalog and still contained an unconditional unfinished-path exception. Those
failed runs remain part of the record; they are not successful acceptance.

The hosted deletion journey also reproduced a notification-ordering bug: the
controller committed removal, but awaiting a non-modal success/cleanup message
held the catalog reload until that message resolved. Outcome notifications are
now non-blocking. Four regression cases keep those notification promises open
and require a fresh catalog for both deletion commands and both outcomes. The
hosted fixture wraps the real provider reload rather than replacing it.
