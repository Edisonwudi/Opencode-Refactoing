---
name: smell-repair-feature-envy
description: Repair one frozen receiver-heavy callable in Java, Python, C, or C++. Use when the task smell is feature_envy and behavior must move to the data owner or a justified workflow boundary rather than merely hiding member accesses.
---

# Feature Envy Repair

Move a cohesive receiver-access responsibility to its correct owner while preserving ordered observable effects.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `../_shared/operation-translations.md` only when needed. For the IDEA backend, also load `idea-refactor-cli` and read only its `feature_envy.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route.

## Common workflow

1. Re-anchor the frozen callable and receiver identity, then inventory every access and alias in the target.
2. Record branch effects in order: returns or exceptions, source/receiver writes, callbacks, output, and notifications.
3. Choose the real ownership boundary. Prefer the receiver owner; use a focused workflow/adapter only when the receiver is an external or stable protocol or the behavior spans collaborators.
4. Move the complete access cluster and leave at most one semantically named interaction at the source entrypoint.
5. Remove copied fallbacks, bulk snapshots, accessor gaming, and same-owner helper relocation.
6. Call `smell_verify`; if it returns `IMPROVED`, finish the returned receiver/access residual on the same route.

## Verification contract

- Access-count reduction alone is insufficient when the responsibility remains in the source.
- Preserve effect order and public entrypoints required by production callers.
- Follow the frozen test-change and project-full verification policy.
