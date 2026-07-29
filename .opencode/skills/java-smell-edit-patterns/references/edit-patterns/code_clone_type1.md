# code_clone_type1

## Refactoring intent

Remove the reported exact clone pair through the topology that fits the two clone sites:
same-file extraction, inheritance normalization, shared helper, existing owner delegation,
or shared core.

## Common verification fit

- Prefer the smallest production diff that gives both reported clone sites one shared
  implementation owner.
- Resolve the exact methods or constructors containing both line anchors before
  editing. Do not refactor a nearby similar method when the labeled target is a
  constructor.
- Choose one fitting route, edit only the reported pair, and call `smell_verify`
  before broadening the change.
- The final pair must share a callee, delegate to an existing owner, or inherit one
  parent implementation while preserving behavior and public signatures.

## Common avoid

- Leaving both clone blocks intact after adding a helper.
- Moving the clone body into two helpers or overloads instead of keeping one shared
  implementation.
- Creating a new abstraction when an existing owner or parent is the intended
  normalization point.
- Changing literals or operators only to evade clone detection.
- Expanding to unrelated overloads or using unchecked casts when a small typed
  adapter can preserve the reported pair's API.
- Adding a nullable owner dependency and retaining the original clone as a
  fallback. Either establish one reliable owner route and delete the superseded
  body, or choose a different topology.
- Widening a production method from private/protected to public only to make
  delegation convenient. Prefer an existing owner, inheritance, or the narrowest
  package-visible helper that preserves the public API.
- Replacing explicit dependency injection with a global application-context or
  service-locator lookup. Follow the project's established constructor/field
  injection and update construction sites narrowly when required.

## Routes

### `same-file-exact-clone`

When: the clones are exact whole methods in the same file, so one extraction with duplicate
replacement can collapse both at once

Direct edit target: Extract the identical block once in the same file and replace every duplicate with the
shared call.

Source operation shape: `extract:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Read both clone sites and choose the smallest exact shared block.
2. Add a private helper near the clone owner with explicit parameters and return value.
3. Replace both clone bodies with the helper call and search for the same block again.

Verification fit delta: Both reported sites must stop containing the same duplicate block.

Avoid: Do not edit only the first occurrence when the second is still identical.

### `shared-parent-pull-up-clone`

When: the clones are identical sibling overrides under a real shared parent, so the right
normalization is pull-up rather than a new helper

Direct edit target: Move the common behavior to the real shared parent and remove duplicate child overrides.

Source operation shape: `pullUp:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Confirm both clone methods share the same parent contract. Follow intermediate
   parent classes as needed; siblings do not have to name the common owner directly.
2. Add the common implementation to the parent with the narrowest usable visibility.
3. Delete child overrides that now inherit the parent behavior.

For constructor clones, move the shared fields and initialization to the common
parent constructor, then delegate from both child constructors with `super(...)`.
Do not edit neighboring ordinary methods merely because they also look similar.

Verification fit delta: The clone disappears because the duplicated child bodies no longer exist.

Avoid: Do not introduce a helper if inheritance already provides the correct owner.

### `different-parent-shared-helper-clone`

When: the clones live under different parent branches with no good superclass target, so the
shared behavior should move to an external helper

Direct edit target: Create an external helper because the clone sites have no good shared superclass owner.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Pick the closest existing utility or package-local helper location shared by both
   callers.
2. Move the common block into a helper with explicit inputs and outputs.
3. Replace both clone blocks and update imports or visibility.

Verification fit delta: Both clone sites should call the same helper rather than retain parallel bodies.

Avoid: Do not force a fake parent relationship just to remove the clone.

### `existing-owner-delegation-clone`

When: one clone target already has a semantically owned implementation in a dedicated service,
utility, or domain owner, so the other clone should delegate to that owner instead of
creating a new helper

Direct edit target: Delegate to the existing semantic owner instead of creating a second helper.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Identify the clone side or nearby service that already owns the behavior.
2. Expose or reuse a narrow owner method for the duplicated operation.
3. Replace the non-owner clone with a call to that owner.

Verification fit delta: The non-owner target should no longer duplicate owner logic.

Avoid: Do not create a parallel helper when the domain owner already exists.
Do not keep the old body behind a null check, feature flag, exception catch, or
other fallback after adding the owner call; that preserves the smell.

### `type-variant-shared-core-clone`

When: the clones are same-class overloads that differ mainly by primitive or narrow value type, so
keep the public overload API and extract only a private shared core with type-specific
adapters

Direct edit target: Keep public overloads and extract a private shared core for the common algorithm.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Compare overloads and isolate the type-specific conversion points.
2. Create a private core method for the shared algorithm, using small typed adapters
   when the overload value types differ.
3. Rewrite overloads as adapters that call the core with converted values.

Verification fit delta: The large common body should exist only in the core method.

Avoid: Do not collapse public overloads if callers rely on their narrow types, and
do not replace typed array access with reflection or an `Object` type dispatcher.
Do not use `IntFunction<Object>` or boxed primitive wrappers for a per-element hot
loop. Prefer `IntPredicate`, a primitive comparator, or another adapter whose
callback returns the final decision/value without boxing each array element.
