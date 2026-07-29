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
- Before pulling behavior or state into a parent, inspect every direct and
  transitive non-target child. Keep child-owned fields and overrides in place
  when moving them would change unrelated descendants, reflection/serialization
  shape, framework wiring, or lifecycle side-effect order. In that case, put a
  narrow parameterized helper in the shared parent or package and let both
  original owners call it at the same point in their existing sequence.
- If both target children already declare same-named resource fields, treat
  declaration ownership as part of the existing class contract. Do not remove,
  shadow, or relocate those fields merely to make a parent method compile.
  Extract the duplicated operations into one helper that accepts the child-owned
  resources as parameters.
- Apply the same rule to identity-bearing sentinels, locks, listeners, caches, and
  lifecycle handles. If the original code compares a child-owned object with
  `==`, moving that object to a parent changes identity semantics. Keep one
  sentinel per original owner and pass both the observed value and that owner's
  sentinel to the shared helper.
- The final pair must share a callee, delegate to an existing owner, or inherit one
  parent implementation while preserving behavior and public signatures.
- The smell Oracle proves clone removal and convergence only. Architecture and
  implementation-quality concerns belong to existing project checks and final diff
  review; they must not be turned into sample-specific smell-guard vetoes.
- Treat overload and override dispatch as behavior. If the original loop calls a
  primitive overload or an overridable hook, reflection such as `Array.get`
  produces boxed `Object` values and may select a different method. Keep the typed
  call in each thin wrapper and share only the iteration/control flow, typically
  through an index callback.
- Preserve every side effect in the extracted block, including logging, interrupt
  handling, metrics, and state-write order. Pass an existing logger or narrow
  callback into the helper when it owns the shared catch path.

## Common avoid

- Leaving both clone blocks intact after adding a helper.
- Moving the clone body into two helpers or overloads instead of keeping one shared
  implementation.
- Creating a new abstraction when an existing owner or parent is the intended
  normalization point.
- Changing literals or operators only to evade clone detection.
- Adding a nullable owner dependency and retaining the original clone as a
  fallback. Either establish one reliable owner route and delete the superseded
  body, or choose a different topology.
- Widening a production method from private/protected to public only to make
  delegation convenient. Prefer an existing owner, inheritance, or the narrowest
  package-visible helper that preserves the public API.
- Treating a test that observes declared fields, cleanup order, lazy allocation,
  logging, or exception behavior as an infrastructure blocker. Preserve the
  observable contract and choose a less invasive shared-helper topology.

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
2. Inspect non-target descendants and field/lifecycle contracts. Pull up the
   implementation only when the behavior and required state genuinely belong to
   every affected child.
3. Add the common implementation to the parent with the narrowest usable visibility.
4. Delete child overrides that now inherit the parent behavior.

If child-owned state or lifecycle ordering must remain local, do not move those
fields into the parent. Add a protected helper that accepts the required state,
call it from both existing overrides at the original point, and keep the
surrounding `super` call order unchanged.

For constructor clones, move the shared fields and initialization to the common
parent constructor, then delegate from both child constructors with `super(...)`.
Do not edit neighboring ordinary methods merely because they also look similar.

Verification fit delta: The clone disappears because the duplicated child bodies no longer exist.

Avoid: Do not force a pull-up merely because a common parent exists. A parent
helper is preferable when the full behavior or state is not universal across
the hierarchy.

### `child-owned-lifecycle-helper-clone`

When: sibling lifecycle methods duplicate cleanup or setup logic, but each child
owns the resource fields and must retain its declaration shape and surrounding
lifecycle order

Direct edit target: Keep each child field and override, extract one parameterized
helper to the closest shared parent or package helper, and replace only the
duplicated block with a call.

Source operation shape: `idea_edit`. See
[`operation-translations.md`](operation-translations.md) for reusable mechanics.

Generic shape:

```java
abstract class Parent {
    protected final void cleanupResources(Connection connection, Cache cache) {
        if (connection != null) {
            connection.disconnect();
        }
        if (cache != null) {
            cache.clear();
        }
    }
}

final class FirstChild extends Parent {
    protected Connection connection; // remains declared here
    protected Cache cache;            // remains declared here

    @Override
    public void stop() {
        cleanupResources(connection, cache);
        super.stop(); // remains in its original position
    }
}
```

Apply the same call in the other target child. The two short delegating wrappers
are acceptable: the duplicated algorithm now has one implementation owner, while
the child-specific state and lifecycle contract remain intact.

Route-specific edit steps:

1. Inspect both complete lifecycle methods, their declared fields, all assignments,
   and the position of `super` calls.
2. Add one helper with explicit resource parameters and the narrowest shared
   visibility.
3. Replace only the duplicated operations in each child with the shared call.
4. Confirm the child fields are still declared in their original classes and the
   `super` call and exception/logging order have not moved.

Verification fit delta: The original multi-statement clone is replaced by calls
to one implementation; no state is pulled into the parent.

Avoid: Do not convert this route into `shared-parent-pull-up-clone` after a
no-progress result. A no-progress result means both original clone blocks still
exist; replace those blocks with the helper call rather than moving fields.

### `child-owned-identity-sentinel-helper-clone`

When: two methods duplicate an identity-sentinel unwrap/check, and each original
owner declares its own sentinel instance

Direct edit target: Keep every sentinel declared in its original class and move
only the comparison algorithm to one parameterized helper.

Generic shape:

```java
abstract class Parent {
    protected static <T> T unwrapSentinel(T value, T ownerSentinel) {
        return value == ownerSentinel ? null : value;
    }
}

final class FirstChild extends Parent {
    private final Position nullPosition = new Position() {}; // remains here

    @Override
    public Position getPosition(Key key) {
        return unwrapSentinel(super.getPosition(key), nullPosition);
    }
}

final class SecondChild extends Parent {
    private final Position nullPosition = new Position() {}; // remains here

    @Override
    public Position getPosition(Key key) {
        return unwrapSentinel(super.getPosition(key), nullPosition);
    }
}
```

The two short wrappers preserve dynamic ownership of each sentinel while the
identity comparison has one implementation owner. This is the default route when
tests, reflection, serialization, or other code observe the child declaration.

Route-specific edit steps:

1. Locate every declaration and use of each sentinel. Do not delete, rename, or
   relocate those fields.
2. Add exactly one helper receiving both the retrieved value and the current
   owner's sentinel.
3. Replace each original multi-line unwrap block with one helper call.
4. Run the focused behavior tests that insert or retrieve the sentinel and confirm
   it is still converted using `==`.

Avoid: Do not introduce a parent sentinel, a shared singleton sentinel, or a
virtual sentinel accessor merely to enable the extraction. Those alternatives
change object identity or class shape. Pass the original object explicitly.

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
4. If the targets are in different packages, use the helper's fully qualified
   package/import in both callers and compile immediately. Never delete or move
   the helper while call sites still reference it.
5. Compare the extracted helper against the original block statement by statement.
   Preserve warning/error logging and state-write order, not only return values and
   thrown exception types.

For duplicated state predicates in different inheritance branches, prefer a
shared query helper that returns the result:

```java
final class ViewState {
    static boolean isEnabled(View view) {
        return view != null && view.isEnabled();
    }
}

// Each existing override remains in its own hierarchy.
protected void updateEnabledState() {
    setEnabled(ViewState.isEnabled(getView()));
}
```

The two one-line wrappers are valid because the behavior has one implementation
owner. Do not conclude that different inheritance branches make the clone
unrefactorable when a parameterized query or command helper preserves behavior.

Use the narrowest visibility that reaches both callers. A same-package helper
should normally be package-private; make it public only when callers are in
different packages and no existing shared owner is available. Utility classes
should be `final` with a private constructor and no unused imports.

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
3. Reuse the project's established wiring pattern and replace the non-owner clone
   with a call to that owner.
4. Update affected construction sites and test fixtures as required.

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
4. Confirm there is exactly one algorithm-bearing core. Two overloaded helpers
   with identical bodies are still a clone; replace them with one Object/reflection,
   indexed predicate, comparator, or other type-neutral core as appropriate.

When the loop invokes primitive overloads or overridable hooks, prefer an indexed
core so the wrapper retains the original statically typed call:

```java
private void forEachIndex(int length, IntConsumer action) {
    for (int i = 0; i < length; i++) {
        action.accept(i);
    }
}

void append(boolean[] values) {
    forEachIndex(values.length, i -> appendDetail(values[i]));
}

void append(short[] values) {
    forEachIndex(values.length, i -> appendDetail(values[i]));
}
```

Do not replace `appendDetail(boolean)` or `appendDetail(short)` with
`appendDetail(Array.get(...))`: boxing changes overload selection and can bypass
subclass overrides even when focused tests remain green.

Verification fit delta: The large common body should exist only in the core method.

Avoid: Do not collapse public overloads if callers rely on their narrow types.
Prefer the project's existing typed abstractions when they are available.
