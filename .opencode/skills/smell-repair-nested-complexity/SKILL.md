---
name: smell-repair-nested-complexity
description: Repair one frozen deeply nested callable in Java, Python, C, or C++. Use when the task smell is nested_complexity and nested branches or loops must be flattened through guards, decomposition, or clearer dispatch without changing effects.
---

# Nested Complexity Repair

Flatten the dominant nested control-flow hotspot while preserving branch ordering, cleanup, and observable behavior.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `references/operation-translations.md` only when needed. For the IDEA backend, also load `idea-refactor-cli` and read only its `nested_complexity.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route.

## Common workflow

1. Re-anchor the target and identify the deepest branch/loop chain contributing to the current Guard metric.
2. Record guards, mutations, exits, exceptions, and cleanup in their original order.
3. Prefer valid early exits when they preserve lifecycle; otherwise extract one cohesive branch or introduce focused dispatch.
4. Remove the original nested implementation rather than copying it into a helper and leaving both paths.
5. Call `smell_verify`; on `IMPROVED`, use the returned remaining depth/complexity hotspot as the next bounded step.

## Verification contract

- Do not exchange nesting for an equally large conditional chain or hide it in a same-target closure.
- Preserve resource cleanup, partial effects, and exception/return semantics described by the language reference.
- Keep verification and test policy controller-owned.
