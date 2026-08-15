---
name: smell-repair-switch-statements
description: Repair switch-heavy target logic in Java, Python, C, or C++. Use when the task smell is switch_statements, including Python match or type-code dispatch, and the target must move to clear table, strategy, registry, or polymorphic dispatch.
---

# Switch Statements Repair

Replace the target's switch/type-code dispatch with one clearer extensible dispatch model while preserving every branch behavior.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `../_shared/operation-translations.md` only when needed. For the IDEA backend, also load `idea-refactor-cli` and read only its `switch_statements.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route.

## Common workflow

1. Re-anchor the frozen target and inventory all cases, fallthrough/order, default behavior, mutations, exceptions, and side effects.
2. Select dispatch from source shape: lookup table for data mapping, registry for extensibility, focused handlers for commands, or polymorphism for real type-owned behavior.
3. Migrate every case and remove the original switch/type-code chain from the target.
4. Preserve unknown/default handling and evaluation timing; do not eagerly execute every branch while constructing a table.
5. Call `smell_verify`; if another switch remains in the frozen target, use it as the exact next worklist.

## Verification contract

- Do not replace a switch with an equally large if/elif chain or hide it in a helper while the target contract remains.
- Preserve language-specific fallthrough, enum coverage, ownership, and dispatch lifetime.
- Let `smell_verify` run the configured build and tests.
