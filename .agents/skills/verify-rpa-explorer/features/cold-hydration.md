# Cold parent and child hydration

## Sub-features
Absent parent destination, pinned parent restore, child entry and content/Git checks.

## How to get to it (user POV)
Open a restored parent, then Josh: Enter Child Room → the saved child.

## Driving it with RCC Browser
After native Save, fixture/cold_restore restores the exact published parent into absent /pilot/cold/parent and asserts no children hydrated. open_cold_parent navigates that editor fixture. Use native Enter Child, then fixture/verify_child corroborates child pin, bytes, Git HEAD/index/status/fsck, unchanged source and absent sibling.

## Gotchas
Parent restore is normal managed CLI, not native cold-discovery UI proof. A missing-parent refusal is not hydration. Child callback without restored files fails.
