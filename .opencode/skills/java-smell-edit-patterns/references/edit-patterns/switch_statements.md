# switch_statements

## Refactoring intent

Replace a large branch dispatcher with the dispatch structure that matches the source shape:
table, handler, registry, classifier, or state-owned behavior.

## Common verification fit

- The reported method should no longer contain the same large switch-style branch
  structure.
- An equally large if/else chain is not a compatible repair.

## Common avoid

- Dropping default, fall-through, EOF, escape, or error behavior.
- Replacing one large dispatcher with another branch list of the same size.

## Routes

### `replace-key-switch-with-table-driven-dispatch`

When: the switch is a central key-to-result dispatcher, and the branching should stay centralized
but be collapsed into a lookup table instead of staying as many terminal cases

Direct edit target: Replace key-to-result cases with a map from key to supplier/function.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Build a table preserving all existing key constants and result expressions.
2. Replace the switch with lookup, null/default handling, and invocation.
3. Keep unknown-key behavior identical.

Verification fit delta: The target method should no longer own the case list.

Avoid: Do not lose null handling or exception behavior.

### `replace-packet-type-switch-with-handler-strategy`

When: the switch is a central dispatcher, but each branch owns a distinct workflow with validation
and side effects, so dispatch stays centralized while the branch behavior moves into
dedicated handler strategies

Direct edit target: Move each packet branch workflow into a dedicated handler strategy.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Define a handler interface around the branch input/output.
2. Create handlers for branch families with validation and side effects.
3. Replace the switch with handler lookup and default/error handling.

Verification fit delta: The dispatcher should select handlers, not contain each workflow body.

Avoid: Do not hide the same branch bodies in anonymous inline lambdas if they stay large.

### `replace-type-code-switch-with-reader-registry`

When: the switch maps stable wire or file-format type codes to small reader functions, so the
external type codes must stay unchanged while dispatch moves into an explicit reader
registry

Direct edit target: Replace stable wire/file type-code cases with an explicit reader registry.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Preserve external type-code constants exactly.
2. Register each reader function/object under its code.
3. Replace switch reading with registry lookup and existing error handling.

Verification fit delta: The method should delegate reading to registered readers.

Avoid: Do not change serialized type-code values.

### `replace-parser-char-switch-with-classifier-table`

When: the switch classifies parser characters or token delimiters, so the branch table should
become an explicit character classifier while preserving escape, EOF, and fall-through
semantics

Direct edit target: Replace parser-character branching with a classifier table or predicate set.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. List delimiter, escape, EOF, and ordinary-character outcomes.
2. Build a classifier method/table that returns those outcomes.
3. Replace the branch with classifier dispatch while preserving fall-through semantics.

Verification fit delta: Character classification should be data/predicate driven rather than a large branch list.

Avoid: Do not collapse escape or EOF cases into ordinary characters.

### `replace-state-switch-with-inner-type-polymorphism`

When: the switch is selecting behavior that already belongs on an inner state type, so the method
should delegate to the state object instead of repeating the per-state branches

Direct edit target: Move state-specific behavior onto the state object/type.

Source operation shape: `idea_edit`. See [`operation-translations.md`](operation-translations.md) for reusable mechanics.

Route-specific edit steps:

1. Find the state type or create a narrow state behavior interface.
2. Move each state branch body into the matching state implementation.
3. Replace the switch with one call to the current state.

Verification fit delta: The target method should delegate to state-owned behavior.

Avoid: Do not keep a parallel branch list after adding state methods.
