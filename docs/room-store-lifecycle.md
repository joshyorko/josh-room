# Room Store lifecycle

Native logical JAT operations select a Room Store recovery point with
`--snapshot` (default: `latest`) and a configured `--dimension`. The commands
accept `--json` for machine-readable results.

```sh
josh-room snapshot inspect ROOM --snapshot SNAPSHOT --dimension DIMENSION
josh-room snapshot export ROOM --snapshot SNAPSHOT --dimension DIMENSION --output FILE.haul
josh-room snapshot serve ROOM --snapshot SNAPSHOT --dimension DIMENSION [--mode auto|files|registry|both]
josh-room snapshot extract ROOM REFERENCE --snapshot SNAPSHOT --dimension DIMENSION --destination DIRECTORY
josh-room room-store verify --dimension DIMENSION [--read-data | --read-data-subset PERCENT]
josh-room room-store optimize --dimension DIMENSION [--confirm]
josh-room room-store reconcile --dimension DIMENSION
```

`snapshot inspect` reads the selected descriptor and reports logical IDs,
workspace and component references, sizes, and export availability. It does
not restore or export data. `snapshot export`, `serve`, and `extract` are
explicit materialization operations; their results include the selected
logical JAT ID and operation-specific output or extraction metadata.

`room-store verify` checks repository integrity without reading pack data by
default. `--read-data` checks all data; `--read-data-subset` selects a bounded
subset. The result reports the check scope. `room-store optimize` returns a
prune plan by default; `--confirm` executes that plan and reports whether it
completed. `room-store reconcile` compares catalog and descriptor reachability
with the Restic inventory and reports missing references and orphans. It does
not remove data.

```sh
josh-room snapshot copy ROOM --source-dimension SOURCE --snapshot SNAPSHOT \
  --destination-dimension DESTINATION --destination-room NEW_ROOM
josh-room snapshots remove ROOM SNAPSHOT --dimension DIMENSION
josh-room rooms remove ROOM --dimension DIMENSION
```

Native Copy creates a new logical JAT. Within one Dimension it reuses the
existing Room Store references; across Dimensions it restores and re-backs up
workspace and component content into the destination repository. Native
removal first conditionally publishes the catalog update, then rebuilds
reachability before forgetting any Restic snapshots. Results report pending
cleanup and reconciliation receipts when publication or cleanup is uncertain.
Legacy portable snapshots continue through the existing JAT and removal paths.
