# dead_code

## Refactoring intent

Remove the reported unused declaration and any stale local fallout without turning reachable
code into a stub.

## Reference-closure fast path

1. Re-anchor the frozen private declaration after line drift and confirm the target Guard
   still reports that declaration as private, unused, and unreferenced in its caller-supplied scope.
2. Search the complete production tree for invocations, method references, overrides,
   annotations, service registration, and reflection strings. Treat a real reference as a
   blocker to safe deletion, not as permission to empty the body.
3. Delete the whole declaration once, then remove only imports and private declarations that
   became unused solely because of that deletion. Keep independently reachable helpers.
4. Search for the frozen declaration identity before a single `smell_verify`. If a residual
   declaration is returned, delete that exact entity; do not delete a same-named declaration
   in another owner or create a no-op replacement.

## Common verification fit

- The reported target should no longer resolve.
- If references remain, migrate them safely or report the blocker instead of deleting
  reachable code.
- Deleting unrelated unused code does not resolve the frozen finding.

## Common avoid

- Replacing a method body with an empty stub.
- Deleting public hooks, reflection targets, or framework callbacks without task evidence.

## Routes

### `safe-delete-unused-private-method`

When: the reported target is a private method with no project method invocations or method
references

Direct edit target: Delete the unused private method after reference search proves it is local dead code.

Source operation shape: `delete:method`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Search for invocations, method references, overrides, annotations, and reflection
   strings.
2. Delete the entire declaration with a narrow edit.
3. Remove stale imports or helpers created only for that declaration.

Verification fit delta: The reported declaration should no longer resolve.

Avoid: Do not stub the body or delete reachable code.
