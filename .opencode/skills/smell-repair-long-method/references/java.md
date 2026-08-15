# long_method

## Refactoring intent

Shrink the reported method by extracting the dominant cohesive slice while preserving the
original method as readable orchestration.

## AST-NCSS fast-path closure

1. Re-anchor the frozen method after line drift and read its current target Guard
   AST-NCSS. Do not estimate length from physical lines.
2. Mark the few contiguous loops, branches, or calculations that contribute most of the
   executable statements. Select the smallest cohesive set whose removal is projected to put
   the target method below the Guard boundary.
3. When one route clearly supplies the required reduction, complete that extraction and call
   `smell_verify` once. Do not spend a continuation on a tiny preliminary helper.
4. When several independent slices are required, extract the preselected slices before the
   first verification when their inputs and effects are clear. If verification returns
   `IMPROVED`, use the returned current AST-NCSS and residual deficit as the exact next
   worklist; do not repeat the previous extraction shape blindly.

## Common verification fit

- The frozen method itself must fall below the target Guard AST-NCSS boundary. A material
  reduction that leaves the finding present is `IMPROVED`, not PASS.
- If it still reports, extract the next preselected cohesive slice that covers the returned
  residual deficit instead of repeating the same edit.

## Common avoid

- Moving the entire body into one equally unclear helper.
- Changing public signatures as part of a local extraction.

## Routes

### `extract-loop-body-from-long-method`

When: the long method is dominated by one bulky loop body or control block, and that block can be
extracted while the caller keeps orchestration

Direct edit target: Extract the bulky loop body or control block that dominates the long method.

Source operation shape: `extract:method`. See [`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

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

Source operation shape: `extract:method`. See [`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

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

Source operation shape: `extract:method`. See [`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Identify all locals written by the slice and read afterward.
2. Create a small helper/result object that owns the multi-output computation.
3. Replace the slice with construction/call and read results from the helper object.

Verification fit delta: The original method should lose the bulky multi-output block.

Avoid: Do not force a helper with many out parameters or mutable arrays.
