# data_clumps

## Refactoring intent

Replace a repeated parameter group with one coherent holder and migrate the occurrence
family far enough that the same group no longer travels as separate values.

## Common verification fit

- The repeated group must fall below the occurrence threshold across the family, not only
  in the first method.
- Use any returned remaining occurrences as the next bounded worklist.
- Counts in dataset evidence are historical audit metadata. They are never the repair
  worklist and must not be used to decide that enough occurrences were migrated. The
  product detector result returned by `smell_verify` is authoritative.

## Common avoid

- A generic parameter bag that mixes unrelated values.
- Renaming parameters while the same values still travel separately.

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
   its callers, overrides, constructors, factories, and same-group forwarding helpers. Do
   not stop after finding the target method or after reaching a dataset-reported count.
2. Partition the occurrences by signature/override family and choose a domain owner that is
   shared by the values. Create or reuse exactly one typed holder there; keep unrelated
   parameters outside it.
3. Migrate a complete family at a time: declaration, overrides/implementations, all
   production callers, and forwarding helpers. Remove the old separated signature instead
   of retaining a compatibility overload or delegate.
4. Before the first `smell_verify`, migrate all directly connected families that can be
   changed without inventing conversions or weakening an API contract. A one-method edit is
   incomplete whenever other ordinary production signatures still carry the group.
5. If verification reports the finding remains, discard the historical count and use the
   detector's current occurrence list as the next exact worklist. Re-search each returned
   owner/signature because line numbers may have moved, migrate the next complete family,
   and verify again. Do not create a second holder.
6. If compilation fails, repair the coordinated signature migration (including generics,
   imports, overrides, and callers) before doing more smell work.

Verification fit delta: The group occurrence count must drop below threshold across the family.

Avoid: Do not create multiple incompatible holders, keep the legacy separated signature as
a wrapper, or count the holder constructor/factory as proof that an old family was migrated.
