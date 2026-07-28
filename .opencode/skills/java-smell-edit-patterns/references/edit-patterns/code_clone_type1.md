# code_clone_type1

## Refactoring intent

Remove the reported exact clone pair through the topology that fits the two clone sites:
same-file extraction, inheritance normalization, shared helper, existing owner delegation,
or shared core.

## Common verification fit

- Before editing, name one structural route and the shared symbol that will own the
  duplicated behavior.
- The original pair should no longer contain two large identical blocks, and both
  targets must resolve through that one owner: a shared callee, owner delegation, or
  inherited parent implementation.
- Search the changed production files for the original block. It may remain once in
  the shared owner, but must not reappear in two private helpers or in a parent plus
  a leftover child override.
- Compile immediately after the first cohesive structural edit, then run
  `smell_verify`. Repair the exact compiler diagnostic before attempting another
  route.
- Keep the first edit scoped to the reported clone pair. Broaden an overload family
  only when compilation or the structural Oracle proves that the pair cannot be
  centralized independently.
- Before final verification, inspect the complete production diff and delete
  superseded helpers, wrappers, imports, or duplicated adapters left by an earlier
  attempt.

## Common avoid

- Changing literals, operators, statement order, or only one clone side merely to
  break exact-token equality.
- Moving the two blocks into separate helpers, even if the reported target methods
  become short.
- Leaving both clone blocks intact after adding a helper.
- Creating a new abstraction when an existing owner or parent is the intended
  normalization point.
- Using unchecked casts to force `List<A>` and `List<B>` through one helper, or
  routing primitive overloads through an incompatible object/generic functional
  interface.
- Replacing typed primitive access with `java.lang.reflect.Array`, a `Class<?>`
  discriminator, and a type-switch. That trades a local clone for slower, less
  readable runtime dispatch.

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

1. Confirm both clone methods are sibling overrides with the same parent contract.
2. Add the common implementation to the parent with the narrowest usable visibility.
3. Delete child overrides and any shadowing duplicate state that now belongs to the
   parent.
4. Compile-check constructors and every `final` field assignment after the move.

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
3. Preserve each caller's concrete generic type. Prefer typed adapters or a
   caller-supplied projection over unchecked casts.
4. Replace both clone blocks and update imports or visibility.

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

### `type-variant-shared-core-clone`

When: the clones are same-class overloads that differ mainly by primitive or narrow value type, so
keep the public overload API and extract only a private shared core with type-specific
adapters

Direct edit target: Keep public overloads and extract a private shared core for the common algorithm.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Compare overloads and isolate the type-specific conversion points.
2. Create a private core method for the shared control flow. For primitive
   overloads, pass an index callback/predicate that closes over the concrete arrays,
   or keep thin typed adapters; do not box values into an incompatible generic
   interface. The shared core may own bounds, null, and loop control, while the
   callback performs the concrete typed comparison.
3. Rewrite overloads as adapters that call the core with converted values.
4. Refactor only the two reported overloads first. Do not sweep every sibling
   overload merely because it has a similar shape.

Verification fit delta: The large common body should exist only in the core method.

Avoid: Do not collapse public overloads if callers rely on their narrow types.
