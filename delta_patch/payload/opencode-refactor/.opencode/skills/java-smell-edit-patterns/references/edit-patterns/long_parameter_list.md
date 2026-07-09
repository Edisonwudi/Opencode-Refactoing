# long_parameter_list

## Refactoring intent

Replace parameters that travel together with a request or value object and migrate call
sites consistently.

## Common verification fit

- The reported signature must actually lose multiple parameters.
- All local callers of the changed signature must be updated.

## Common avoid

- Constructing a holder inside the method while keeping the long signature.
- Changing annotation, framework, or reflection-bound APIs without task evidence.

## Routes

### `static-util-7-params-to-request-object`

When: a static utility signature is long because all parameters travel together as one request,
and the old multi-arg entrypoint can be removed outright

Direct edit target: Replace a static utility signature with one request object.

Source operation shape: `introduce:parameter-object`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Name the request object from the utility purpose.
2. Move all traveling parameters into immutable fields/accessors.
3. Change the static method and every caller to pass the request object.

Verification fit delta: The old multi-argument signature should be gone or reduced below the threshold.

Avoid: Do not keep the old long method as the primary implementation.
