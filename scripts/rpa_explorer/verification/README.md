# Receiver fence check scope

The immutable `SessionFence` is a final admission guard for one task-owned editor.
Its binding contains session id, generation, exact VSIX SHA256, retained
observation SHA256 and the immutable attempt set. It does not dispatch input or
attest to retention/cleanup. The caller owns those effects.

Two shapes were considered. A mutable global session registry would require
shared-writer coordination and could let queued work see a replacement target.
The chosen frozen value travels with the one Workbench owner. Replacing it after
retained observation or closure makes old requests reject. No daemon, scheduler,
transfer mechanism or new provider authority is introduced.

Run `python3 scripts/rpa_explorer/verification/check.py --output "$TASK_ROOT/fence-check.json"`.
Read `.agents/skills/verify-rpa-explorer-safety/SKILL.md` for model configs,
mutation expectations, concrete code correspondence and explicit assumptions.

The morning design bundle was read privately after verifying its supplied
SHA256. Only the final receiver check, exact evidence/attempt-set identity and
retained uncertain-spend patterns were applied. The large generic design was
not copied. The small model describes this code seam; it is not proof of browser
focus, provider mutation safety or successful process disposal.
