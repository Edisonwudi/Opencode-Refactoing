# god_class

## Refactoring intent

Extract cohesive responsibilities from the reported class in ordered stages so the original
class remains valid and its target Guard profile no longer reports it.

## Profile-closure protocol

Plan the complete resolution before making the first extraction:

1. Re-anchor the frozen class identity and record every current target Guard objective, such as
   fields, methods, complexity, size, and foreign-data access. Dataset metrics and evidence are
   not verdict inputs or a work estimate; use the current scoped Guard snapshot.
2. Build a field-method cohesion map from production source. Form candidate clusters only
   from state and behavior that share one invariant, lifecycle, or existing domain owner.
3. Estimate which objectives each cluster will remove from the original class. Choose the
   smallest ordered set of cohesive clusters whose combined removal is projected to make the
   complete target Guard profile false. An estimate chooses work; only `smell_verify` accepts it.
4. Migrate one cluster at a time in dependency order, including its state, maintaining
   methods, internal callers, construction, and ownership. Remove superseded source members
   and pass-through wrappers before starting the next planned cluster.
5. When independent clusters are clear, finish the planned set before the first expensive
   verification. If ownership or compilation becomes uncertain, verify after the completed
   cluster rather than broadening the edit.
6. If verification returns `IMPROVED`, read the current Guard objectives, recompute only
   the remaining deficit, and continue with the next cohesive cluster. Do not stop after one
   extraction and do not replace the plan with an unrelated broad split.

## Common verification fit

- The original class must still exist, and PASS requires the complete versioned God Class
  target Guard profile to stop reporting it. A smaller class that remains a finding is
  `IMPROVED` only.
- Wrapper-only delegation is not enough if it leaves the same field and method mass in
  place.
- Each extracted cluster must own real state or behavior, and the full closure must migrate
  callers and construction consistently.

## Common avoid

- Deleting or renaming the reported class as the repair.
- Splitting several unrelated responsibilities in one broad edit.
- Calling `smell_verify` after each individual caller rewrite or adding empty collaborators
  solely to reduce counts.

## Routes

### `extract-class-state-and-behavior`

When: primary route when the reported class has a cohesive field/state cluster such as cache,
font, text, image, or geometry state, and the cluster's maintaining behavior can move with
that state

Direct edit target: Extract a field/state cluster and the methods that maintain it into a new class.

Source operation shape: `extract:class`, `direct:edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Choose one cohesive state cluster and list fields plus maintaining methods.
2. Create the extracted class and move the state and behavior into it.
3. Replace original class fields with one collaborator and remove private wrappers that
   no longer add behavior.

Verification fit delta: This stage removes one real field-method cluster. Continue with the
next planned cluster while the complete target Guard profile still reports the original class.

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

Verification fit delta: This stage removes one real behavior cluster. Continue with the next
planned cluster while the complete target Guard profile still reports the original class.

Avoid: Do not move unrelated methods only because they are in the same large class.

### `insert-type-state-cluster-member-migration`

When: auxiliary route for a field/state cluster only when native extract:class is unavailable or
cannot migrate the needed fields and maintaining methods

Direct edit target: Manually introduce an extracted state type when native class extraction is not available.

Source operation shape: `direct:edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Create the new type and constructor from the selected field cluster.
2. Migrate methods that maintain the cluster to the new type.
3. Replace original fields and calls incrementally, re-reading after each edit.

Verification fit delta: The original class no longer owns this state cluster directly. Continue
with the next planned cluster while the complete target Guard profile still reports the class.

Avoid: Do not copy state into the new type while leaving the original state authoritative.
