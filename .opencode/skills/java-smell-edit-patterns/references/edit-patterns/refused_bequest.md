# refused_bequest

## Refactoring intent

Repair an inheritance mismatch by deleting redundant overrides, implementing required
contracts, delegating to real owners, splitting interfaces, or pushing behavior down.

## Common verification fit

- The reported child/parent contract mismatch should be gone.
- Unsupported-operation throws or empty overrides called out by evidence must be deleted,
  implemented, or delegated.
- The supplied sample test is an immutable project-behavior regression oracle. Keep it
  unchanged and make production code satisfy both the smell guard and that behavior test.
- Use the evidence `refactor_path` as the primary route and inspect the referenced existing
  helper, overload, state field, sibling protocol, or capability boundary before editing.

## Hierarchy migration protocol

Apply this protocol whenever evidence supplies `structural_expectation`, especially
`capability_split`:

1. Before editing, inspect the reported class, its parent and interfaces, sibling
   implementations, and every production call site of the target contract.
2. Treat `refactor_path` as one cohesive hierarchy migration even when the task names one
   target method. A disappearing target declaration is not sufficient by itself.
3. Remove the unwanted capability from classes that refuse it while preserving the
   capability for real implementers.
4. Never relocate an empty, throwing, null-returning, constant, placeholder, or
   compatibility no-op implementation into an ancestor, interface default, or sibling.
5. Change declarations, implementations, production types, and callers consistently.
   Compile after the structural edit and before the final `smell_verify`.
6. When evidence supplies `refactor_group_id`, use it only to identify the shared design
   boundary and related production code. Do not edit or weaken tests.
7. Inspect the complete production diff before acceptance. Reject API widening, scattered
   casts, or compatibility shims unless existing callers make them necessary and behavior
   verification covers them.

## Common avoid

- Hiding refused behavior behind a rename.
- Removing inheritance before checking parent-typed callers and sibling implementations.
- Editing, deleting, skipping, or weakening the sample test.
- Replacing an empty or rejecting override with logging, comments, swallowed exceptions,
  or a placeholder constant.

## Routes

### `remove-redundant-override`

When: the reported override adds no real subclass behavior, so the right fix is to remove the
override entirely rather than keep an empty hook or pure delegation layer

Direct edit target: Delete an override that only repeats parent behavior.

Source operation shape: `inline:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Confirm parent implementation satisfies the same contract.
2. Remove the override declaration.
3. Search for subclass-specific references that depended on the override body.

Verification fit delta: The refusing override should no longer exist.

Avoid: Do not delete required abstract/interface implementations.

### `implement-required-contract-method`

When: the reported method is an empty override required by an interface or abstract parent, and no
same-class overload, existing helper, or sibling protocol pattern can implement it more
specifically

Direct edit target: Fill an empty required override with behavior derived from the contract and nearby
invariants.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Read the parent contract and sibling implementations.
2. Implement the smallest behavior that honors the contract.
3. Keep the required signature intact.

Verification fit delta: The override should no longer be empty or refusing.

Avoid: Do not invent unrelated behavior when a sibling pattern exists.

### `delegate-required-override-to-existing-overload`

When: the reported empty override is required by a parent contract, and another overload in the
same class already implements the same operation with a richer or normalized argument shape

Direct edit target: Delegate a required simple override to an existing richer overload.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Find the overload that already implements the operation.
2. Choose defaults from constants or documented overload behavior.
3. Make the required override call the overload.

Verification fit delta: The required override remains present and performs real behavior.

Avoid: Do not delete the required override.

### `delegate-required-override-to-existing-helper`

When: the reported empty override is required by a parent contract, and a dedicated service/helper
already owns the operation so the override should delegate to that helper

Direct edit target: Delegate a required override to a helper or service that already owns the operation.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Identify the helper/service and its required inputs.
2. Inject, access, or call it using existing project patterns.
3. Replace the empty/refusing body with the delegation.

Verification fit delta: The override should call real behavior rather than remain empty.

Avoid: Do not create a new helper when an existing owner is already present.

### `complete-required-contract-from-sibling-pattern`

When: the reported empty override is required by a parent contract, and sibling methods or
implementations show the request/transport/handler pattern needed to complete it

Direct edit target: Implement the required override by copying the established sibling protocol shape.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Read sibling implementations or analogous subclasses.
2. Adapt the same request/transport/handler pattern to the target class.
3. Keep target-specific names and invariants.

Verification fit delta: The target should follow the project protocol instead of refusing it.

Avoid: Do not paste a sibling body without adapting target fields and errors.

### `split_collection_wrapper_from_property_wrapper`

When: a collection-oriented wrapper inherits bean/property accessor operations that it cannot
support, but the project design can separate collection operations from property-wrapper
operations

Direct edit target: Separate collection-wrapper behavior from unsupported property-wrapper behavior.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Identify which inherited operations are collection-specific and which are
   property-specific.
2. Introduce a narrower interface/base type for supported collection behavior.
3. Migrate callers to the narrow type and remove refusing property operations from the
   collection wrapper.

Verification fit delta: The collection wrapper should no longer inherit operations it cannot support.

Avoid: Do not keep rejecting property methods on the same inherited contract.

### `replace_closed_executor_inheritance_with_closed_executor_guard`

When: a closed or sentinel executor subclass inherits runtime executor operations only to reject
them, and a narrower closed-state guard or sentinel object can express the behavior

Direct edit target: Represent closed/sentinel behavior with a guard or sentinel object instead of inheriting
runtime operations only to reject them.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Find callers that only need closed-state checks.
2. Introduce a narrow closed-state API or sentinel representation.
3. Keep callers that still need runtime execution on the normal executor type; migrate
   only closed-state callers to the new guard/sentinel API.
4. Remove or stop exposing inherited runtime-operation rejection from the sentinel
   subclass after the migrated callers no longer depend on that type.
5. Preserve a factory, adapter, or compatibility entrypoint only when existing public
   callers need it, and make that entrypoint return the narrower supported abstraction.

Verification fit delta: The closed object should no longer refuse inherited runtime operations.

Avoid: Do not keep the same unsupported runtime methods on the closed subclass as the
main public contract.

### `split_executor_wrapping_capability_from_executor_runtime_operations`

When: an executor exposes a wrapper setup method that is not meaningful for one implementation,
and wrapping should be modeled as a separate setup capability

Direct edit target: Split setup/wrapping capability from executor runtime operations.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Identify callers that need wrapping setup versus runtime execution.
2. Create a narrow setup capability interface or collaborator.
3. Move wrapping-only methods to that capability and keep runtime executors focused.

Verification fit delta: Implementations that cannot wrap should not inherit wrapping methods.

Avoid: Do not add no-op wrapping implementations.

### `return_existing_multiple_values_state`

When: the refused getter can be implemented from a state field that is already maintained by
nearby setter or update methods

Direct edit target: Implement a refused getter from state already maintained by setters or update methods.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Find the maintained state field and its update path.
2. Return that state through the required getter with any defensive copy needed.
3. Check callers for null/empty expectations.

Verification fit delta: The getter should return existing state instead of refusing inheritance.

Avoid: Do not synthesize unrelated placeholder values.

### `split_inbound_packet_from_outbound_packet_contract`

When: an outbound-only packet inherits inbound parsing methods from a bidirectional packet
interface, and packet direction can be represented by narrower inbound/outbound contracts

Direct edit target: Split inbound parsing from outbound packet behavior.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Separate inbound parse methods from outbound serialization methods.
2. Create narrower inbound/outbound contracts or adapters.
3. Migrate outbound-only packets away from inherited parse methods.

Verification fit delta: Outbound-only packets should not inherit parse methods they cannot support.

Avoid: Do not keep parse methods that just throw unsupported exceptions.

### `replace_MessageFormat_inheritance_with_composed_extended_formatter`

When: a formatter subclass inherits mutable parent methods that are incompatible with its
extension registry, and composition or a narrower immutable formatting API can preserve
behavior

Direct edit target: Replace incompatible formatter inheritance with composition or a narrower immutable
formatting API.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Identify parent mutable methods that conflict with the extension registry.
2. Wrap a formatter collaborator instead of extending the incompatible mutable type.
3. Expose only the formatting operations the class can support.

Verification fit delta: The subclass should no longer inherit incompatible mutable parent operations.

Avoid: Do not override inherited mutators only to reject them.

### `push-down-misowned-hierarchy-hook`

When: the behavior is real but a concrete parent hook is only meaningful for one subclass, so
ownership should move downward instead of leaving the hook on the parent

Direct edit target: Move a real hook from a parent to the subclass that actually supports it.

Source operation shape: `pushDown:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Confirm the hook is meaningful for one subclass and misleading for siblings.
2. Move the implementation to the owning subclass.
3. Update parent abstraction and callers so unsupported siblings do not inherit the hook.

Verification fit delta: The hook should no longer be inherited by classes that refuse it.

Avoid: Do not leave a parent default that unsupported subclasses must reject.
