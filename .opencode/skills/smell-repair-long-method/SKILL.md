---
name: smell-repair-long-method
description: Repair one frozen long callable in Java, Python, C, or C++. Use when the task smell is long_method and cohesive control or calculation slices must be extracted while the target remains readable orchestration.
---

# Long Method Repair

Extract the smallest cohesive slices that remove the target's dominant executable mass while preserving behavior and orchestration.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `../_shared/operation-translations.md` only when needed. For the IDEA backend, also load `idea-refactor-cli` and read only its `long_method.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route.

## Common workflow

1. Re-anchor the target and read its current Guard metric rather than estimating from physical lines.
2. Mark the few contiguous loops, branches, or computations contributing most to the metric.
3. Extract a cohesive slice with explicit inputs, output, and side effects; keep the original callable as understandable orchestration.
4. When one extraction cannot clear the finding, complete the preselected independent slices before an expensive verification if their contracts are clear.
5. Call `smell_verify`; on `IMPROVED`, use the returned residual deficit to choose the next hotspot.

## Verification contract

- Do not move the entire body into one equally long helper or extract trivial fragments.
- Preserve exception, cleanup, generator/coroutine, ownership, and public-signature behavior through the selected language route.
- Let the controller own build/test commands and test mutability.
