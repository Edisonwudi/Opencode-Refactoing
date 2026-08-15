---
name: smell-repair-long-parameter-list
description: Repair one frozen long parameter list in Java, Python, C, or C++. Use when the task smell is long_parameter_list and a cohesive request/value object or narrower API must replace the complete excessive signature family.
---

# Long Parameter List Repair

Replace a semantically coherent parameter subset with a typed abstraction and migrate the complete affected signature family.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `../_shared/operation-translations.md` only when needed. For the IDEA backend, also load `idea-refactor-cli` and read only its `long_parameter_list.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route.

## Common workflow

1. Re-anchor the complete frozen declaration and classify parameters by one domain concept rather than count alone.
2. Inspect implementations, overrides, callbacks, factories, and every production caller before choosing the migration boundary.
3. Introduce or reuse a purpose-named value/request abstraction with explicit invariants; keep unrelated parameters separate.
4. Migrate the complete signature family and callers as one transaction, preserving defaults, ordering, ownership, and public contracts.
5. Remove the old excessive signature unless an immutable external contract makes the route impossible; do not keep a Guard-counted wrapper.
6. Call `smell_verify` and repair the returned signature/caller closure before doing unrelated work.

## Verification contract

- Do not group unrelated values, reorder behavior, or hide parameters in generic maps or arrays.
- Respect language-specific ABI, keyword-call, overload, and override constraints.
- Build/test execution and test migration remain controller-owned.
