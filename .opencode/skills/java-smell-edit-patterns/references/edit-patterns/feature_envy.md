# feature_envy

## Refactoring intent

Move receiver-heavy behavior toward the object that owns the data while keeping the source
entrypoint stable when that is the safer no-IDE route.

## Common verification fit

- Close the reported collaboration, rather than shaving a few accesses: after the refactor,
  the target method should interact with the envied field (including stable aliases) through
  at most one semantically named receiver operation. A lower access count is only an
  intermediate result while the target Guard still reports the frozen method.
- In no-IDE mode, keep a source delegate when removing or moving the target method would
  make verification ambiguous.
- Inspect production callers, public/protected entrypoints, receiver ownership, and protocol
  boundaries before choosing the owner boundary. Keep a thin source entrypoint when the frozen
  production API contract requires it. If the source owns an application workflow or the
  receiver is an external or stable port that should not own the orchestration, choose the
  independent workflow/adapter route. These architecture signals guide the edit; they never
  replace the frozen target finding or the Guard verdict.
- Do not infer permission to edit tests from their contents or failures. Follow only the
  controller-frozen `allow_test_changes` policy: when enabled, test migration is accepted only
  when the frozen `project_full` verification contract passes; otherwise test sources remain
  immutable.

## Receiver-operation closure protocol

Before editing, make one compact ledger in your reasoning:

1. Enumerate every access to the envied field or an alias in the target method and group the
   accesses by the receiver-owned operation they jointly implement. Do not start by wrapping
   individual fields.
2. For every affected branch, record observable effects in order: returned value or thrown
   exception, receiver/source state writes, and each outgoing write, callback, or notification.
   The receiver operation may return a typed outcome or ordered effect list when the source
   entrypoint must replay those effects; it must not collapse multiple effects into one result.
3. Choose the operation boundary from ownership and frozen production contracts. Normally,
   implement one cohesive operation on the concrete receiver owner. For read-heavy flows,
   return a purpose-specific immutable result computed by the receiver, not a bulk snapshot
   exposing raw fields. When the receiver is an interface, declare the operation once and
   implement it in the concrete implementation; do not copy the same control flow into a
   default method and an implementation. If the receiver is external, is a stable protocol, or
   should not own cross-collaborator orchestration, use the independent workflow/adapter route
   below instead of adding an unsuitable receiver facade plus a copied source fallback.
4. Replace the entire receiver-access cluster in the source with that single call. Search the
   target method again for the field and its aliases before `smell_verify`; if more than one
   receiver interaction remains, finish the same operation instead of adding another helper.
5. Remove superseded trial helpers, accessors, imports, and duplicated implementations before
   verification. On a test failure, repair the missing branch effect in production code and
   preserve the one-call boundary.
6. Before the first `smell_verify`, re-read the frozen target and every new or changed method
   in the original owner. Confirm that the complete receiver-access cluster has one real owner
   and was not moved into a same-owner helper.
7. If verification returns `IMPROVED`, use the current receiver, access count, and returned
   residual finding identities as the exact next worklist. Finish the same ownership route;
   do not open a parallel facade, add a source fallback, or refactor a different receiver.

## Common avoid

- A source wrapper that still performs all foreign getter traversal before delegating.
- Moving coordinator-only behavior into the receiver.
- Adding one trivial getter, setter, or method-reference wrapper per foreign access; this is
  metric gaming and grows the receiver API without moving a responsibility.
- Returning a raw all-fields snapshot merely to hide foreign reads, duplicating a large
  implementation in an interface and a concrete class, or leaving unused helpers from an
  earlier attempt.
- Keeping a same-source-class fallback that repeats the receiver workflow when the receiver
  boundary cannot own a new operation. Preserve compatibility at the production boundary; do
  not relocate the target Guard finding to another source helper.

## Routes

### `move-static-util-to-correct-owner`

When: a static helper uses no source state and mostly manipulates another type's data, so the
whole method should move to that owner type

Direct edit target: Place a stateless utility on the type that owns the data or domain vocabulary.

Source operation shape: `move:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Confirm the method uses no source instance state and preserve its observable-effect ledger.
2. Add one cohesive static operation to the target owner with appropriate package visibility.
3. Update source and call sites to call the owner operation, or leave only a narrow
   compatibility delegate if required; the source side performs at most one owner call.

Verification fit delta: The reported source method or callers should no longer contain the foreign-centric utility
logic.

Avoid: Do not leave the original method doing the same foreign work behind a new name.

### `move-instance-method-to-storage-owner`

When: an instance method in a coordinator class mostly enforces one collaborator's policy and
storage rules, so the whole method can move intact

Direct edit target: Move collaborator policy or storage behavior onto that collaborator.

Source operation shape: `move:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Identify the collaborator whose state and policy dominate the method and record all branch
   effects that must survive the move.
2. Add one operation to the collaborator and adapt the full access cluster to
   collaborator-owned state.
3. Rewrite the coordinator to call the collaborator operation once, replay any typed ordered
   effects, and update visibility/imports.

Verification fit delta: The coordinator should become a thin caller, not the policy owner.

Avoid: Do not move coordinator-only dependencies into the storage owner.

### `extract-slice-then-move-to-envied-receiver`

When: a receiver-heavy slice inside a larger source method must move to the envied receiver;
extract:method is only a temporary isolation step, and the plan must not stop after
extract:method or leave the helper in the original source class

Direct edit target: Move only the receiver-heavy slice from a larger source method to the receiver.

Source operation shape: `extract:method`, `move:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Select the complete receiver-access cluster and identify source-only inputs and ordered
   observable effects.
2. Create one receiver-side operation for that cluster with explicit inputs and a typed
   outcome when the source must retain orchestration.
3. Replace the whole source cluster with one call to the receiver operation.

Verification fit delta: The source method should retain orchestration but lose the foreign-access cluster.

Avoid: Do not stop after extracting a helper that remains in the source class.

### `receiver-side-helper-replace-source-delegation`

When: the source method must stay as an entrypoint or coordinator, but a receiver-heavy
computation can be expressed as a new helper on the envied receiver and the source can
delegate to it

Direct edit target: Keep the source entrypoint and move its receiver-heavy internals behind a receiver helper.

Source operation shape: `direct:edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Add one receiver-side operation for the complete cohesive computation/query.
2. Replace the source internals with one narrow delegate call, preserving the effect ledger.
3. Search for every remaining source-side access or alias and fold it into the same operation.

Verification fit delta: The source method should be visibly less foreign-object intensive.

Avoid: Do not make a delegate that still assembles all receiver data in the source method.

### `extract-collaboration-workflow-preserve-receiver-api`

When: the source method owns an ordered application workflow, while the envied receiver is an
external or stable protocol/port, or the workflow spans collaborators and does not semantically
belong to the receiver. A new concrete receiver operation would violate that production
ownership boundary.

Direct edit target: Extract the complete collaboration and effect sequence to an independent,
purpose-named workflow/adapter class; keep the existing receiver API and source entrypoint.

Source operation shape: OpenCode read/search/edit tools only. See
[`operation-translations.md`](operation-translations.md) for Java mechanics, but do not invoke
an unavailable external refactoring tool in the no-IDE agent.

Route-specific edit steps:

1. Confirm the boundary signal in production source or diagnostics: external/protocol
   ownership, source-layer orchestration involving ordered effects, or a workflow spanning
   multiple collaborators.
   Do not choose this route merely because adding a receiver operation is inconvenient.
2. Move the entire effect ledger into one new workflow operation in a class outside the
   original source class. Inject the existing receiver/port and call its existing API in the
   same order, preserving return values, exceptions, state writes, notifications, and partial
   failure behavior.
3. Replace the target cluster with one workflow call. Keep only input/result adaptation in
   the source; do not retain a copied fallback and do not add an unstubbed receiver facade.
4. Let `smell_verify` inspect every new or changed method in the bounded diff scope. The workflow must own a
   genuine collaboration/use-case responsibility, not be a renamed dump of the old method or
   a helper created solely to move the finding across a class boundary. Remove trial facades,
   duplicate helpers, and dead compatibility code before verification.
5. On production contract, value, or effect-order failures, compare the effect ledger branch
   by branch and repair the workflow itself; never reintroduce a same-source-class fallback.

Verification fit delta: The original finding disappears, the target has one workflow
interaction, the existing receiver contract remains intact, and no same-owner/same-
receiver finding is relocated within the source class.

Avoid: Do not add a receiver facade that violates the receiver's ownership boundary, mirror
that facade in a source fallback, or create a generic `Helper` that merely copies the original
foreign-access body.
