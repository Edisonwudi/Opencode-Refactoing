# feature_envy

## Refactoring intent

Move receiver-heavy behavior toward the object that owns the data while keeping the source
entrypoint stable when that is the safer no-IDE route.

## Common verification fit

- The reported method should perform less direct foreign-object work.
- In no-IDE mode, keep a source delegate when removing or moving the target method would
  make verification ambiguous.

## Common avoid

- A source wrapper that still performs all foreign getter traversal before delegating.
- Moving coordinator-only behavior into the receiver.

## Routes

### `move-static-util-to-correct-owner`

When: a static helper uses no source state and mostly manipulates another type's data, so the
whole method should move to that owner type

Direct edit target: Place a stateless utility on the type that owns the data or domain vocabulary.

Source operation shape: `move:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Confirm the method uses no source instance state.
2. Add the static helper to the target owner with appropriate package visibility.
3. Update source and call sites to call the owner method, or leave only a narrow
   compatibility delegate if required.

Verification fit delta: The reported source method or callers should no longer contain the foreign-centric utility
logic.

Avoid: Do not leave the original method doing the same foreign work behind a new name.

### `move-instance-method-to-storage-owner`

When: an instance method in a coordinator class mostly enforces one collaborator's policy and
storage rules, so the whole method can move intact

Direct edit target: Move collaborator policy or storage behavior onto that collaborator.

Source operation shape: `move:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Identify the collaborator whose state and policy dominate the method.
2. Add the method to the collaborator and adapt field access to collaborator-owned state.
3. Rewrite the coordinator to call the collaborator method and update visibility/imports.

Verification fit delta: The coordinator should become a thin caller, not the policy owner.

Avoid: Do not move coordinator-only dependencies into the storage owner.

### `extract-slice-then-move-to-envied-receiver`

When: a receiver-heavy slice inside a larger source method must move to the envied receiver;
extract:method is only a temporary isolation step, and the plan must not stop after
extract:method or leave the helper in the original source class

Direct edit target: Move only the receiver-heavy slice from a larger source method to the receiver.

Source operation shape: `extract:method`, `move:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Select the contiguous receiver-heavy slice and identify source-only inputs.
2. Create a receiver-side method for that slice with explicit parameters.
3. Replace the source slice with a call to the receiver method.

Verification fit delta: The source method should retain orchestration but lose the foreign-access cluster.

Avoid: Do not stop after extracting a helper that remains in the source class.

### `receiver-side-helper-replace-source-delegation`

When: the source method must stay as an entrypoint or coordinator, but a receiver-heavy
computation can be expressed as a new helper on the envied receiver and the source can
delegate to it

Direct edit target: Keep the source entrypoint and move its receiver-heavy internals behind a receiver helper.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Add a receiver-side helper for the cohesive computation/query.
2. Replace the source internals with one narrow delegate call.
3. Search for source-side foreign access that should move with the helper.

Verification fit delta: The source method should be visibly less foreign-object intensive.

Avoid: Do not make a delegate that still assembles all receiver data in the source method.
