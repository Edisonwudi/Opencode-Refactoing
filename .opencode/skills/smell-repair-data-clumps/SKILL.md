---
name: smell-repair-data-clumps
description: Repair a frozen repeated parameter group in Java, Python, C, or C++. Use when the task smell is data_clumps and complete declaration families must migrate to a cohesive parameter object or equivalent language abstraction.
---

# Data Clumps Repair

Replace the repeated value group with one cohesive abstraction and migrate enough complete occurrence families to clear the frozen Guard.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `references/operation-translations.md` only when an operation shape is unclear. For the IDEA backend, also load `idea-refactor-cli` and read only its `data_clumps.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route.

## Common workflow

1. Build a declaration ledger from the Guard's current occurrence witness; calls are compile-repair sites, not migrated declarations.
2. Partition declarations by real call, override, or shared-domain relationships. Matching names or types alone do not create one component.
3. Introduce one typed holder per selected semantic component and migrate complete declarations, implementations, overrides, callers, and forwarding helpers.
4. Remove old separated signatures. A compatibility wrapper carrying the same group remains an occurrence unless the Guard explicitly says otherwise.
5. Before verification, project the remaining declaration count and finish enough complete families to cross the Guard boundary.
6. Call `smell_verify`; use its returned occurrences as the only residual worklist.

## Verification contract

- Do not rename parameters, create a generic bag, or inline duplicated business bodies to reduce the count.
- Preserve public compatibility only when it does not retain the frozen smell; otherwise report the immutable API blocker.
- Let `smell_verify` own build/test execution and the test-change policy.
