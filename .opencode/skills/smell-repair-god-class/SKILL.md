---
name: smell-repair-god-class
description: Repair one frozen God Class or C module in Java, Python, C, or C++. Use when the task smell is god_class and cohesive state-behavior responsibilities must be extracted without deleting, renaming, or hollowing the target.
---

# God Class Repair

Extract cohesive responsibilities in dependency order until the original target no longer satisfies its complete Guard profile.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `references/operation-translations.md` only when needed. For the IDEA backend, also load `idea-refactor-cli` and read only its `god_class.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route.

## Common workflow

1. Re-anchor the target class or module and read every current Guard objective.
2. Build a field/global-to-function/method cohesion map using shared invariants, lifecycle, and existing owners.
3. Select the smallest ordered set of real responsibility clusters projected to clear the whole profile.
4. Move each cluster with its state, maintaining behavior, construction, and callers; remove superseded source members and valueless wrappers.
5. Call `smell_verify` after a coherent stage. On `IMPROVED`, recompute only the returned remaining deficit and continue the planned extraction.

## Verification contract

- The frozen target must remain uniquely identifiable unless its Guard explicitly permits another transition.
- Do not rename/delete the target, create empty collaborators, or split unrelated members merely to reduce size.
- Preserve lifecycle, serialization, ABI/API, and test behavior described by the selected language reference.
