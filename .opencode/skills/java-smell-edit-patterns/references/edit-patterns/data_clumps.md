# data_clumps

## Refactoring intent

Replace a repeated parameter group with a coherent holder inside each real semantic
occurrence component, and migrate enough components that the same group no longer travels
as separate values above the detector threshold.

## Common verification fit

- The repeated group must fall below the occurrence threshold across the family, not only
  in the first method.
- Use any returned remaining occurrences as the next bounded worklist.
- Counts in dataset evidence are historical audit metadata. They are never the repair
  worklist and must not be used to decide that enough occurrences were migrated. The
  product detector result returned by `smell_verify` is authoritative.

## Acceptance-budget declaration ledger

Before editing, turn the product detector's occurrence count into a declaration budget.
Let `N` be the current number of method or constructor declarations containing the separate
group. Keep a compact ledger of those declarations, partitioned first by semantic connected
component and then by override/signature family; call expressions are compile-repair sites
and never count as migrated detector occurrences. Two declarations are connected only by a
real forwarding call, override relationship, or shared domain invariant. Matching parameter
names/types alone is not an edge.

Use this projection before the first verification:

`projected occurrences = N - migrated old declarations + newly introduced declarations that still take the separate group`

The canonical constructor or factory for a new holder commonly still accepts the separate
values and therefore costs one new occurrence. With the normal passing maximum of two, a
new holder usually means migrating at least `N - 1` old declarations. Update declaration
families first, then repair every caller; do not spend a continuation changing call
expressions while leaving the counted declarations intact.

When compatibility is genuinely required, reserve the budget explicitly. For example, one
holder constructor plus one legacy public wrapper uses the entire budget: move the interface
and implementation family to the holder signature and keep at most one compatibility entry,
rather than retaining the old signature in both interface and implementation. If the
test-visible API requires more legacy declarations than the budget permits, preserve
behavior and report the unresolved constraint; do not disguise the group with `Object...`,
maps, arrays, or an untyped bag.

The checkpoint also freezes the baseline declaration owners and the original group's
parameter slots and types. Renaming values in those same slots to `p0`, `arg1`, or other
aliases does not consume the declaration budget. Unrelated parameters of the same type in
other slots do not count as the old group after a real parameter-object replacement. When
verification reports `legacy_type_signature_group_remains`, migrate the listed production
declaration or remove its compatibility wrapper; do not rename parameters again.

The same checkpoint freezes a small set of source-unique business-body windows from those
detector-owned declarations. Deleting a declaration and pasting its body into multiple
callers is not a parameter-object refactoring. If verification reports
`inlined_body_window_expanded`, restore one shared implementation and route the migrated
signatures through it. Moving a body once is allowed; increasing a frozen window from one
production location to two or more is not.

## Test-visible API compatibility closure

Treat test-compilation errors such as `actual and formal argument lists differ` or `does not
override or implement` as evidence that the old signature is an externally visible boundary,
not as permission to edit tests. Restore the exact modifiers, generic parameters, throws
clause, and separate-parameter signature at the highest shared production boundary for that
override family. Keep its body as a thin adapter to the holder-based implementation. For an
abstract extension point, the holder-based entry may instead delegate to one retained legacy
root so test or downstream subclasses can keep overriding the old signature while migrated
production subclasses override the new entry.

Charge every retained production declaration to the acceptance ledger. Before restoring one,
migrate enough other detector-owned declarations that the projected count, including the
compatibility root, is still at most two. Retain one root per required signature family, never
the same wrapper in every implementation. If distinct externally required method names or
families alone exceed the detector budget, preserve behavior and leave the result `IMPROVED`;
do not evade the detector with varargs, `Object`, arrays, maps, reflection, or renamed aliases.

## Common avoid

- A generic parameter bag that mixes unrelated values.
- Renaming parameters while the same values still travel separately.
- Editing only calls while detector-counted declarations remain, retaining the separated
  signature in every override, or duplicating business logic at callers to make an old
  declaration disappear.

## Routes

### `parameter-object-for-occurrence-graph`

When: the target context identifies a parameter bundle and source inspection confirms that
the bundle travels through an occurrence graph; introduce one parameter object and migrate
the graph as one coherent source-level transaction.

Direct edit target: Introduce one holder for the identified group and migrate enough complete
signature families that the product detector no longer reports the finding.

Source operation shape: OpenCode read/search/edit tools only. See
[`operation-translations.md`](operation-translations.md) for the Java mechanics, but do not
invoke an IDEA tool in the no-IDEA agent.

Route-specific edit steps:

1. Treat the target group names and types only as a selector. Search the whole production
   tree for every declaration whose normalized parameter set contains the group, then trace
   its callers, overrides, constructors, factories, and same-group forwarding helpers. Build
   the acceptance-budget declaration ledger above; do not stop after finding the target
   method or after reaching a dataset-reported count.
2. Build the occurrence graph and split it into semantic connected components. A call or
   override edge is direct evidence; a domain edge must be justified by one shared invariant
   or lifecycle. Never connect unrelated protocols merely because their parameter descriptors
   match. For each component selected for migration, choose its natural domain owner and
   create or reuse exactly one typed holder there; keep unrelated parameters outside it.
3. Migrate a complete family at a time: declaration, overrides/implementations, all
   production callers, and forwarding helpers. Remove the old separated signature unless
   build/test diagnostics prove that the family is an externally visible boundary; in that
   case apply the compatibility closure above at one highest shared root only.
4. Before the first `smell_verify`, migrate enough complete declaration families or whole
   components for the projected global count (including holder constructor/factory costs) to
   be at most two. It is valid to migrate one cohesive component and leave disconnected
   occurrences unchanged when that alone drops the detector below threshold. A one-method
   edit is incomplete whenever the ledger still projects three or more ordinary production
   declarations carrying the group.
5. If verification reports the finding remains, discard the historical count and use the
   detector's current occurrence list as the next exact worklist. Re-search each returned
   owner/signature because line numbers may have moved, migrate the next complete family or
   component, and verify again. Do not create a second holder inside one component and do not
   merge disconnected components into a cross-domain bag.
6. If production compilation fails, repair the coordinated signature migration (including
   generics, imports, overrides, and callers) before doing more smell work. If test compilation
   alone exposes an old API or override contract, apply one budgeted compatibility root, then
   migrate additional internal declarations as needed and verify again without editing tests.

Verification fit delta: The group occurrence count must drop below threshold across the family.

Avoid: Do not create multiple incompatible holders inside one component, share one holder
across disconnected domains, retain duplicate legacy wrappers below a shared boundary, or
count the holder constructor/factory as proof that an old family was migrated.
