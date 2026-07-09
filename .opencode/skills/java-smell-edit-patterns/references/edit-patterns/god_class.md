# god_class

## Refactoring intent

Extract one cohesive responsibility from the reported class so the original class remains
valid but smaller and less central.

## Common verification fit

- The original class must still exist and should be smaller or less complex.
- Wrapper-only delegation is not enough if it leaves the same field and method mass in
  place.

## Common avoid

- Deleting or renaming the reported class as the repair.
- Splitting several unrelated responsibilities in one broad edit.

## Routes

### `extract-class-state-and-behavior`

When: primary route when the reported class has a cohesive field/state cluster such as cache,
font, text, image, or geometry state, and the cluster's maintaining behavior can move with
that state

Direct edit target: Extract a field/state cluster and the methods that maintain it into a new class.

Source operation shape: `extract:class`, `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Choose one cohesive state cluster and list fields plus maintaining methods.
2. Create the extracted class and move the state and behavior into it.
3. Replace original class fields with one collaborator and remove private wrappers that
   no longer add behavior.

Verification fit delta: The original class should shrink in fields, methods, size, or complexity.

Avoid: Do not leave all old fields and methods as pass-through wrappers.

### `move-method-cluster-to-owner`

When: secondary route when several methods in the reported class mostly manipulate one existing
collaborator, so those methods can leave the god class and become behavior on that real
owner

Direct edit target: Move several methods that mostly manipulate one existing collaborator to that collaborator.

Source operation shape: `move:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Group methods by the collaborator they primarily access.
2. Move one coherent method cluster by recreating it on the owner and replacing source
   calls.
3. Remove source methods that are now pure wrappers unless callers require a facade.

Verification fit delta: The reported class should lose a real behavior cluster, not only gain delegation.

Avoid: Do not move unrelated methods only because they are in the same large class.

### `insert-type-state-cluster-member-migration`

When: auxiliary route for a field/state cluster only when native extract:class is unavailable or
cannot migrate the needed fields and maintaining methods

Direct edit target: Manually introduce an extracted state type when native class extraction is not available.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Create the new type and constructor from the selected field cluster.
2. Migrate methods that maintain the cluster to the new type.
3. Replace original fields and calls incrementally, re-reading after each edit.

Verification fit delta: The original class should no longer own the migrated state cluster directly.

Avoid: Do not copy state into the new type while leaving the original state authoritative.
