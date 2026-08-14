# nested_complexity

## Refactoring intent

Flatten the reported method by extracting named branches and using guard clauses where
behavior allows it.

## Complexity-deficit closure

1. Re-anchor the frozen method and record its current target Guard complexity and passing
   boundary. Rank nested branches, loops, catches, and boolean breaks by their contribution;
   physical indentation alone is not the objective.
2. Select the smallest behaviorally cohesive set of guard-clause and extraction steps whose
   combined reduction is projected to put the frozen method below the boundary.
3. Preserve branch effects, loop control, exceptions, resource cleanup, and state-write order
   while completing that set. Do not verify after a cosmetic first extraction when the ledger
   still projects the finding.
4. If verification returns `IMPROVED`, use the current complexity and returned residual
   deficit to choose the next hotspot. Do not rework an already flattened branch or switch to
   an opaque compound condition.

## Common verification fit

- The frozen method must fall below the target Guard complexity boundary. Lower complexity
  while the same finding remains is `IMPROVED`, not PASS.
- Cosmetic extraction without flattening the target method is not enough.

## Common avoid

- Inverting conditions without checking side effects.
- Combining many conditions into one opaque boolean expression.

## Routes

### `extract-deeply-nested-block`

When: one deep inner block is the main source of nesting, and extracting that block is enough even
if the outer control flow shape stays the same

Direct edit target: Extract the deepest meaningful branch so the target method has less nested detail.

Source operation shape: `extract:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Select the inner block that carries most of the nesting.
2. Create a helper named after the branch purpose.
3. Replace the branch body with the helper call while preserving outer loop/condition
   behavior.

Verification fit delta: The target method should show less local nesting after extraction.

Avoid: Do not move the same nested block into a helper and leave a complex wrapper around it.

### `extract-valid-branch-then-flatten-guard`

When: one nested branch can be extracted first, but the real cleanup comes from inverting the
remaining outer condition into a guard clause

Direct edit target: Extract a valid branch, then invert the remaining outer condition into a guard clause.

Source operation shape: `extract:method`, `direct:edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Extract the nested valid-path branch into a helper.
2. Rewrite the remaining invalid path as an early guard return/throw/continue.
3. Confirm side effects and resource cleanup still happen in the original order.

Verification fit delta: The target method should gain a flatter main path, not only a helper.

Avoid: Do not invert a condition whose else branch changes state needed later.
