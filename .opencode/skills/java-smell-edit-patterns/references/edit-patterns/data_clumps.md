# data_clumps

## Refactoring intent

Replace a repeated parameter group with one coherent holder and migrate the occurrence
family far enough that the same group no longer travels as separate values.

## Common verification fit

- The repeated group must fall below the occurrence threshold across the family, not only
  in the first method.
- Use any returned remaining occurrences as the next bounded worklist.

## Common avoid

- A generic parameter bag that mixes unrelated values.
- Renaming parameters while the same values still travel separately.

## Routes

### `parameter-object-for-occurrence-graph`

When: the reported parameter bundle appears as a same-group occurrence graph; introduce one
parameter object on a single safe anchor signature family, migrate that family with a
coordinated signature migration, then migrate residual ordinary helpers to the existing
holder with exact OpenCode old/new edits

Direct edit target: Introduce one holder for the reported group and migrate the occurrence graph to that holder.

Source operation shape: `introduce:parameter-object`, `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Parse the reported group and search for matching signatures and helper calls.
2. Create or reuse one holder type at the safest signature family.
3. Update residual helpers and callers to pass the same holder instead of the separated
   group.

Verification fit delta: The group occurrence count must drop below threshold across the family.

Avoid: Do not create multiple incompatible holders for the same reported group.
