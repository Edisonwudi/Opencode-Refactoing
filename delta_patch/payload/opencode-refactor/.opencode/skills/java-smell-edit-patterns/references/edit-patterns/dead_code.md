# dead_code

## Refactoring intent

Remove the reported unused declaration and any stale local fallout without turning reachable
code into a stub.

## Common verification fit

- The reported target should no longer resolve.
- If references remain, migrate them safely or report the blocker instead of deleting
  reachable code.

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
