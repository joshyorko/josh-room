# Storage refresh — 0.1.24 local candidate

The native Refresh command and Save/New completion invoked the cache-only tree
redraw method. They now invoke the existing authoritative storage reload path.
Passive redraw and activation remain free of implicit storage reads.

Save/New and explicit Refresh regressions failed before correction. They now
verify that a newly returned Room and JAT are present in the actual provider
tree. A failed post-save reload preserves the successful save result, performs
no second snapshot write, and warns to retry Refresh rather than save again.
The provider retains its error state. Bounded Luna review found no remaining
issue in this delta.

Final full Node suite passed; optional managed-runner fixtures remain skipped.
Python: 562 passed, 6 skipped. Ruff, diff checks, source/template parity,
VSIX integrity, packaged-code parity, and extracted-package secret scanning
passed. The 0.1.24 package and runtime version declarations match.

Candidate filename: josh-room-0.1.24-candidate.vsix.
The prior 0.1.23 candidate was retained. Installed Devsy/MinIO refresh has not
been verified by the agent; it remains the user's candidate check. No live
storage operations or configuration changes were made. This is not a published
release or issue-close acceptance.
