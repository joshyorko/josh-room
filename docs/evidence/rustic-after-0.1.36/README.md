# Qualification receipts

The readiness matrix is [the canonical report](../../rustic-production-readiness.md).
These receipts use only generated fixtures and public source/runtime identifiers.

| Receipt | Result and scope |
| --- | --- |
| [benchmark-final.json](benchmark-final.json) | PASS, complete real production-adapter 100k-file sequence from clean `3ab1d56`, workspace overlayfs; measured core source hashes match final code `13d6d7f`. Performance remains a blocker. |
| [benchmark-clustered.json](benchmark-clustered.json) | PASS, earlier tmpfs run changing five directories; source-file hashes bind the precommit run to `56eb5dd`. |
| [benchmark-distributed.json](benchmark-distributed.json) | FAIL, earlier tmpfs run stopped on cold full-check cache diagnostics; its Rustic restore was NOT RUN. |
| [post-fix-integrity-100k.json](post-fix-integrity-100k.json) | PASS, separate real 100k-file integrity/restore reproduction after bypassing optional cache for checks; not substituted for a benchmark. |
| [native-results.json](native-results.json) | Linux PASS; Windows FAIL on the strict read-only assertion. The native product admission guard passed. |
| [windows-readonly.json](windows-readonly.json) | Native capture omitted mode and restored 0444 as 0666; complete synthetic Git restore had 21 mode mismatches and zero byte/set mismatches. Windows product selection is blocked. |
| [managed-linux-x64.json](managed-linux-x64.json) | PASS, installed candidate/RCC/JAT/age, encrypted two-recipient catalogs, Git recovery, portable JAT export and clean-room restore. |
| [managed-win32-x64.json](managed-win32-x64.json) | BLOCKED, installed consumer confirms `store-engine-platform-unsupported`; data/JAT/age composition is NOT RUN after the product admission block. |
| [full-suite.json](full-suite.json) | FAIL, final broad run has 1,997 passed / 16 failed / 12 skipped / 2 deselected; all 16 failing nodes reproduced on exact main. Initial failures and successful owned-cache package retry are retained. |

`13d6d7f` changes Windows admission and verification diagnostics after the clean
benchmark checkpoint. The benchmark receipt retains its original source/tree
and hashes, plus a checked binding for every measured core module and the
unchanged operations-fixture function. No whole-tree benchmark claim is inferred
from a different source commit.

No new PR was created. Automatic approval review denied that optional action as
publication outside the task's authorization. The isolated branch pushes were
authorized and completed; main, PR #108 and staged 0.1.36 remain unchanged.
