# nested_complexity

## Refactoring intent

Flatten the reported method by extracting named branches and using guard clauses where
behavior allows it.

## Common verification fit

- The reported method should have shallower nesting or lower cognitive complexity.
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

Source operation shape: `extract:method`, `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Extract the nested valid-path branch into a helper.
2. Rewrite the remaining invalid path as an early guard return/throw/continue.
3. Confirm side effects and resource cleanup still happen in the original order.

Verification fit delta: The target method should gain a flatter main path, not only a helper.

Avoid: Do not invert a condition whose else branch changes state needed later.
