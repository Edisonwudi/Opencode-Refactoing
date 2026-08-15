# long_parameter_list

## Refactoring intent

Replace parameters that travel together with a request or value object and migrate the
complete signature family consistently.

## Complete-signature migration protocol

Build one closure worklist before editing:

1. Resolve the frozen declaration identity after line drift. Record whether it is a
   constructor, static method, instance method, or member of an override family.
2. Search the whole production tree for the exact signature, its overload/override family,
   `this(...)` or `super(...)` constructor chains, method references, factories, and every
   caller. Test references are migration sites only when the controller-frozen policy permits
   test changes; they never define the finding.
3. Select only parameters that form one cohesive request, value, or configuration concept.
   Keep unrelated control flags or services explicit instead of building a catch-all bag.
4. Create one immutable, strongly typed holder, change the declaration and body, migrate the
   complete worklist, and delete the old long signature as one source-level transaction.
5. Search again for the old normalized signature before `smell_verify`. If verification
   reports a lingering declaration or compile error, treat the complete returned declaration
   or diagnostic set as the next exact worklist. Repair it in one pass; do not restore the old
   signature as a delegate.

## Common verification fit

- The frozen declaration must lose enough parameters to fall below the target Guard
  boundary, and the original long-signature entity must be gone.
- All production callers, constructor chains, overrides, implementations, and method
  references in the migration closure must compile against the new signature.
- A lower parameter count elsewhere or a newly introduced holder is not completion while the
  target Guard still reports the frozen finding. `IMPROVED` is a continuation result, not PASS.

## Common avoid

- Constructing a holder inside the method while keeping the long signature.
- Keeping the old method or constructor as a compatibility delegate, including a deprecated
  or single-statement wrapper.
- Changing annotation-, framework-, serialization-, or reflection-bound APIs without first
  finding and migrating their real production contract. If that contract is immutable, report
  the blocker instead of adding a fallback entrypoint.
- Using `Object`, varargs, arrays, maps, or an untyped holder to hide the parameter count.

## Routes

### `static-util-7-params-to-request-object`

When: a static utility signature is long because its parameters form one request and the
complete caller set can migrate to a request object

Direct edit target: Replace a static utility signature with one request object.

Source operation shape: `introduce:parameter-object`. See
[`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Apply the no-delegate shape represented by `keepMethodAsDelegate=false`.
2. Name the request object from the utility purpose and keep it immutable.
3. Migrate every production caller and every policy-permitted test caller.
4. Delete the original long signature and search for its normalized descriptor.

Verification fit delta: The old multi-argument static signature is gone or below threshold.

Avoid: Do not leave a static forwarding overload after introducing the request object.

### `constructor-parameters-to-value-object`

When: a constructor has many values that jointly establish one configuration or domain state,
and its construction sites can migrate together

Direct edit target: Replace the cohesive constructor parameters with one immutable value or
configuration object.

Source operation shape: `introduce:parameter-object`. See
[`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Inspect every `new`, factory, builder, `this(...)`, and `super(...)` path for the frozen
   constructor before changing it.
2. Introduce a holder whose validation and field types preserve the original constructor
   invariants; do not move unrelated services or lifecycle handles into it.
3. Change the constructor and migrate all construction and chaining sites in dependency order.
4. Delete the original long constructor rather than retaining an overloaded delegate.

Verification fit delta: The frozen constructor no longer exposes the long parameter list, and
all construction paths use the typed holder.

Avoid: Do not keep both constructors or hide the old arguments behind a static factory that
still accepts the same long list.

### `instance-operation-parameters-to-request-object`

When: a non-overriding instance method receives one cohesive operation request, while the
receiver remains the correct behavior owner

Direct edit target: Replace the traveling method parameters with a request object without
moving the method away from its owner.

Source operation shape: `introduce:parameter-object`. See
[`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Separate receiver state and injected collaborators from values that belong to the request.
2. Introduce one purpose-named immutable request and update the method body to read it.
3. Migrate calls, method references, forwarding helpers, and policy-permitted test calls.
4. Remove the old long method signature and any trial overloads.

Verification fit delta: The same instance operation remains behaviorally owned by the
receiver, but the frozen long signature disappears.

Avoid: Do not move receiver-owned behavior into the request object or retain a long wrapper.

### `override-family-parameters-to-request-object`

When: the frozen declaration participates in an interface, abstract-method, or override
family whose declarations and callers can be migrated as one API change

Direct edit target: Change the contract and every implementation to one typed request object.

Source operation shape: OpenCode read/search/edit tools only. See
[`operation-translations.md`](../../_shared/operation-translations.md) for Java mechanics, but do not invoke
an unavailable external refactoring tool in the no-IDE agent.

Route-specific edit steps:

1. Inventory the parent/interface declaration, every implementation and override, `super`
   call, method reference, forwarding helper, and caller before editing any one member.
2. Change the highest owned contract first, then implementations, production callers, and
   policy-permitted test overrides/callers. Preserve visibility, generics, annotations, and
   throws clauses.
3. Keep one real holder-based implementation path; remove the separated signature from every
   owned declaration in the family.
4. Search the whole closure for the old descriptor before verification. Batch-repair all
   compile diagnostics from that API migration before calling `smell_verify` again.

Verification fit delta: The override family compiles on the new request type and no owned
declaration retains the frozen long signature.

Avoid: Do not change only one implementation, add a default long-signature bridge, scatter
casts, or preserve an old abstract root as a fallback. If the highest contract is external and
cannot change, report the constraint rather than claiming resolution.
