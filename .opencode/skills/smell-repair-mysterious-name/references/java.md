# mysterious_name

## Refactoring intent

Rename the reported low-information identifier at the correct Java scope.

## Symbol-closure protocol

1. Re-anchor the frozen symbol by kind, owner, and scope after line drift. Do not select a
   same-spelled declaration in a neighboring scope.
2. Build the complete reference closure before editing: declaration and lexical uses for a
   local/parameter; callers, method references, overload/override declarations, and `super`
   calls for a method; constructors, file name, imports, and type references for a class.
3. Choose one meaningful name from behavior and project vocabulary, then migrate the complete
   production closure in one pass. Update policy-permitted test references only as API
   migration; do not change assertions or expected behavior.
4. Search for the old symbol in its frozen scope before `smell_verify`. If the target Guard still
   returns the finding or compilation returns missed references, use that complete residual
   set as the next exact worklist instead of renaming another nearby symbol.

## Common verification fit

- The exact reported identifier should be renamed at its declaration scope.
- Method, type, and parameter renames require reference updates.
- A rename elsewhere is no progress, and a partially migrated symbol closure is not PASS.

## Common avoid

- Renaming nearby identifiers while leaving the reported name unchanged.
- Changing serialized names or reflection keys unless they intentionally track the Java
  identifier.

## Routes

### `rename-single-char-param`

When: a method parameter name is too short to carry meaning and must be renamed through the method
signature so every caller updates together

Direct edit target: Rename the short parameter through the method signature and body.

Source operation shape: `change-signature:method`. See [`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Infer the parameter role from method body and call sites.
2. Edit the signature and every use in the method body.
3. Update named references or documentation only when they track the Java parameter.

Verification fit delta: The reported parameter name should no longer be low-information.

Avoid: Do not rename only one use of the parameter.

### `rename-generic-method-name`

When: the method body has one clear intent, but the current method name is a generic verb that
hides that intent

Direct edit target: Rename a generic method to describe its real operation.

Source operation shape: `rename:method`. See [`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Choose the name from observable behavior and local vocabulary.
2. Edit the declaration and all Java call sites found by search.
3. Check overloads and imports after the rename.

Verification fit delta: The reported method name should no longer be the vague identifier.

Avoid: Do not pick another broad verb such as handle, process, or doIt.

### `rename-mysterious-local-variable`

When: a local temporary name is vague, but the variable plays one concrete role inside a small
scope, so an in-place local rename is enough

Direct edit target: Rename the vague local variable inside its lexical scope.

Source operation shape: `rename:local-variable`. See [`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Determine the local role from assignments and reads.
2. Edit the declaration and each use in the same scope.
3. Re-read for shadowed identifiers.

Verification fit delta: The reported local identifier should disappear from that scope.

Avoid: Do not rename similarly spelled variables in other scopes.

### `rename-mysterious-class-name`

When: a type name is an opaque abbreviation at module scope, so the right fix is a class rename
rather than only renaming one member

Direct edit target: Rename the opaque type and its Java file/references when required.

Source operation shape: `rename:type`. See [`operation-translations.md`](../../_shared/operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Choose the type name from module/domain responsibility.
2. Rename declaration, constructors, imports, and references.
3. Rename the file when the public Java type requires it.

Verification fit delta: The reported type name should no longer resolve under the old opaque name.

Avoid: Do not rename only a field or method when the smell is the type name.
