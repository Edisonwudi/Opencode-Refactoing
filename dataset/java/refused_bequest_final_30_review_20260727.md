# Refused Bequest final 30 independent review

## Final decision

`ACCEPT`

The final dataset contains 30 unique method targets grouped into five shared
refactorings:

- Canal IPacket capability split: 15 methods
- H2 Page leaf/non-leaf capability split: 9 methods
- JHotDraw TextAreaEditingTool safe drag: 1 method
- JHotDraw TextEditingTool safe drag: 1 method
- Mindustry ConsumeItemExplode capability split: 4 methods

The method is the experiment/sample unit. `refactor_group_id` remains mandatory
because methods in the same group share one indivisible design change.

## Acceptance contract

Each accepted method has:

1. a pre-refactor rejecting/empty/null smell witness;
2. a pre-refactor behavior Oracle for its supported business behavior;
3. a post-refactor build and behavior Oracle;
4. a post-refactor structural Oracle that removes the rejected capability;
5. a complete production diff review;
6. an independent review verdict.

Removing an unsupported public capability is allowed only when no original
project test or specification freezes that rejection as required behavior.
Supported behavior and the repository's internal compilation must remain
green. Source/binary API risk is reported rather than hidden.

## Independent review findings and fixes

The first review rejected the former Arc `TiledDrawable.draw` sample. Changing
its parent to `BaseDrawable` only changed the behavior from
`UnsupportedOperationException` to an inherited no-op; the nine-argument
`Drawable.draw` capability remained in the public contract. It was removed
from the final dataset.

The two Arc `MixFilter.setInput` samples were also removed because the proposed
parent change made `MixFilter` incompatible with `FxProcessor.addEffect`
without an integration Oracle.

They were replaced by three already-refactored Canal directional packet
methods with exact protocol behavior coverage:

- `BinlogDumpCommandPacket.fromBytes`
- `ClientAuthenticationPacket.fromBytes`
- `AuthSwitchRequestPacket.toBytes`

The H2 Page design was revised after review. Public instance methods exposing
both leaf and non-leaf accessors were removed. Callers now explicitly narrow
with checked `Page.LeafOperations.from(page)` or
`Page.NonLeafOperations.from(page)` calls.

The second independent review accepted all 30 final methods.

## Machine audit

- rows: 30
- unique sample IDs: 30
- unique `(project, location)` pairs: 30
- group `method_count` sum: 30
- Arc rows: 0
- row/group command mismatches: 0
- pre structural witnesses: 30/30 fail as expected
- post structural Oracles: 30/30 pass
- Refused Bequest guard self-check: PASS
- multilanguage runner self-check: PASS
- improvement gate self-check: PASS
- `git diff --check`: PASS

## Pinned Canal Oracle

- commit: `9978359541ed1d3a2a2f9e7fea265c66d4247869`
- tree: `22aba7d6f216522520cfe003ed149393ead16d94`
- `DirectionalPacketBehaviorTest` SHA-256:
  `5a416f7e14470fa2eb3687d1b6d1deaff4c42fa1f675bef2e9144792be4c6a3c`
- `RegisterSlaveCommandPacketTest` SHA-256:
  `8bddc4d5952aaf725ef0c0008320c915ada943393ee1d9fe626af1f034f4e270`
- post driver tests: 41 tests, 0 failures, 0 errors, 1 skipped
- post common tests: 21 tests, 0 failures, 0 errors, 1 skipped
- full reactor compile: 43 modules, success

## Frozen production patches

All patches apply cleanly to their pinned baseline checkout.

- Canal:
  `8174782f310fd22b81fd22c16eb8ee91be8589c1c35c64a8c149f2ee41cf939d`
- H2 Page:
  `cd599b49c834e3fa2d705ce03c708d4b0d8ac7c50f6fc938061775771ee58ca7`
- JHotDraw drag groups:
  `0a855d64981a377a981d85d98d024eb3834fbcb71e1877b4dec51925fe7fd138`
- Mindustry:
  `3d02485957563269672c5068939842eb46781b031cbc1075ebe4848782b2d8d7`

The authoritative method and group files are:

- `refused_bequest_final_30_20260727.csv`
- `refused_bequest_final_groups_5_20260727.csv`
