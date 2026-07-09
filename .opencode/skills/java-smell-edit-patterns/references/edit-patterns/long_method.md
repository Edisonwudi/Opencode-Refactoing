# long_method

## Refactoring intent

Shrink the reported method by extracting the dominant cohesive slice while preserving the
original method as readable orchestration.

## Common verification fit

- The reported method itself should become materially shorter.
- If it still reports, extract another cohesive slice instead of repeating the same edit.

## Common avoid

- Moving the entire body into one equally unclear helper.
- Changing public signatures as part of a local extraction.

## Routes

### `extract-loop-body-from-long-method`

When: the long method is dominated by one bulky loop body or control block, and that block can be
extracted while the caller keeps orchestration

Direct edit target: Extract the bulky loop body or control block that dominates the long method.

Source operation shape: `extract:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Select the loop body or branch block with clear inputs and side effects.
2. Create a helper that receives loop variables and returns any needed result.
3. Replace the body with the helper call and keep loop orchestration in the original
   method.

Verification fit delta: The target method should shrink because the dominant block moved out.

Avoid: Do not extract only the loop header or a tiny statement sequence.

### `extract-flat-computation-from-long-method`

When: the long method contains one contiguous, mostly straight-line calculation slice with a
single derived result, so a plain helper extraction is clean

Direct edit target: Extract a straight-line calculation slice with one clear result.

Source operation shape: `extract:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Find a contiguous calculation block with a meaningful output.
2. Create a helper named after the derived concept.
3. Replace the block with assignment from the helper result.

Verification fit delta: The method should become shorter without changing calculation order.

Avoid: Do not split a calculation if doing so obscures required intermediate state.

### `extract-method-object-for-multi-output-block`

When: the candidate slice is cohesive but writes several locals that are still needed later, so
plain extract:method stalls and the path must switch to method object

Direct edit target: Use a small helper object when a cohesive slice writes several locals needed later.

Source operation shape: `extract:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Identify all locals written by the slice and read afterward.
2. Create a small helper/result object that owns the multi-output computation.
3. Replace the slice with construction/call and read results from the helper object.

Verification fit delta: The original method should lose the bulky multi-output block.

Avoid: Do not force a helper with many out parameters or mutable arrays.
