# ADR 0004 — age key and recovery model

Production snapshots require two independent age recipients: daily-use and
offline recovery. Private identities are external, permission-checked files and
never logged or committed. Tests use disposable synthetic identities and prove
each identity can decrypt independently.
