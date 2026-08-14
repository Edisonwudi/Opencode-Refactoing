# data_clumps

## Refactoring intent

Replace a repeated parameter group with a coherent holder inside each real semantic
occurrence component, and migrate enough components that the same group no longer travels
as separate values above the target Guard boundary.

## Common verification fit

- The repeated group must fall below the occurrence threshold across the family, not only
  in the first method.
- Use any returned remaining occurrences as the next bounded worklist.
- A reduced occurrence count is `IMPROVED` only. Complete enough returned declaration
  families to cross the target Guard boundary before treating the route as resolved.
- Counts in dataset evidence are historical audit metadata. They are never the repair
  worklist and must not be used to decide that enough occurrences were migrated. The
  caller-supplied target Guard verdict returned by `smell_verify` is authoritative.

## Acceptance-budget declaration ledger

Before editing, turn the target Guard's scoped occurrence witness into a declaration budget.
Let `N` be the current number of method or constructor declarations containing the separate
group. Keep a compact ledger of those declarations, partitioned first by semantic connected
component and then by override/signature family; call expressions are compile-repair sites
and never count as migrated Guard occurrences. Two declarations are connected only by a
real forwarding call, override relationship, or shared domain invariant. Matching parameter
names/types alone is not an edge.

Use this projection before the first verification:

`projected occurrences = N - migrated old declarations + newly introduced declarations that still take the separate group`

A canonical constructor for a real holder is excluded only when the holder owns matching
typed fields and directly initializes every member of the group. An empty or generic holder
constructor remains an occurrence. Update declaration families first, then repair every
caller; do not spend a continuation changing call expressions while leaving counted
declarations intact.

Every retained declaration that still carries the separate group remains a Guard
occurrence, including deprecated one-statement delegates. Source compatibility is not a
Guard exemption. Migrate all callers covered by the frozen policy and remove the old
signature; if an immutable external contract makes that impossible, report the blocker rather
than recording a false resolution.

The checkpoint also freezes the baseline declaration owners and the original group's
parameter slots and types. Renaming values in those same slots to `p0`, `arg1`, or other
aliases does not consume the declaration budget. Unrelated parameters of the same type in
other slots do not count as the old group after a real parameter-object replacement. When
verification reports `legacy_type_signature_group_remains`, migrate the listed production
declaration and its callers; do not rename parameters or preserve a wrapper.

The same checkpoint freezes a small set of source-unique business-body windows from those
frozen occurrence declarations. Deleting a declaration and pasting its body into multiple
callers is not a parameter-object refactoring. If verification reports
`inlined_body_window_expanded`, restore one shared implementation and route the migrated
signatures through it. Moving a body once is allowed; increasing a frozen window from one
production location to two or more is not.

## Common avoid

- A generic parameter bag that mixes unrelated values.
- Renaming parameters while the same values still travel separately.
- Editing only calls while Guard-counted declarations remain, retaining the separated
  signature in every override, or duplicating business logic at callers to make an old
  declaration disappear.

## Routes

### `parameter-object-for-occurrence-graph`

When: the target context identifies a parameter bundle and source inspection confirms that
the bundle travels through an occurrence graph; introduce one parameter object and migrate
the graph as one coherent source-level transaction.

Direct edit target: Introduce one holder for the identified group and migrate enough complete
signature families that the target Guard no longer reports the frozen finding.

Source operation shape: OpenCode read/search/edit tools only. See
[`operation-translations.md`](operation-translations.md) for the Java mechanics, but do not
invoke an unavailable external refactoring tool in the no-IDE agent.

Route-specific edit steps:

1. Treat the target group names and types only as a selector. Use ordinary source search to
   build the explicit occurrence scope from declarations whose normalized parameter set
   contains the group, then trace its callers, overrides, constructors, factories, and
   same-group forwarding helpers. Build the acceptance-budget declaration ledger above; do
   not stop after finding the target
   method or after reaching a dataset-reported count.
2. Build the occurrence graph and split it into semantic connected components. A call or
   override edge is direct evidence; a domain edge must be justified by one shared invariant
   or lifecycle. Never connect unrelated protocols merely because their parameter descriptors
   match. For each component selected for migration, choose its natural domain owner and
   create or reuse exactly one typed holder there; keep unrelated parameters outside it.
3. Migrate a complete family at a time: declaration, overrides/implementations, all
   production callers, and forwarding helpers. Remove the old separated signature. If the
   frozen policy permits test changes, migrate only affected test callers while preserving
   assertions; otherwise leave tests unchanged and let full verification expose a real
   compatibility blocker.
4. Before the first `smell_verify`, migrate enough complete declaration families or whole
   components for the projected scoped count of ordinary declarations to be at most two. It
   is valid to migrate one cohesive component and leave disconnected
   occurrences unchanged when that alone drops the target Guard below threshold. A one-method
   edit is incomplete whenever the ledger still projects three or more ordinary production
   declarations carrying the group.
5. If verification reports the finding remains, discard the historical count and use the
   target Guard's current bounded occurrence witness as the next exact worklist. Re-search
   each returned owner/signature because line numbers may have moved, migrate the next complete family or
   component, and verify again. Do not create a second holder inside one component and do not
   merge disconnected components into a cross-domain bag.
6. If compilation fails, repair the coordinated signature migration (including generics,
   imports, overrides, production callers, and policy-permitted test callers) before doing
   more smell work. Do not restore the old signature as a Guard-exempt adapter.

Verification fit delta: The group occurrence count must drop below threshold across the family.

Avoid: Do not create multiple incompatible holders inside one component, share one holder
across disconnected domains, retain duplicate legacy wrappers below a shared boundary, or
count the holder constructor/factory as proof that an old family was migrated.
