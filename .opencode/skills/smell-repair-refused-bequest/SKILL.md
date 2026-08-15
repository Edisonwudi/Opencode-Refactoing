---
name: smell-repair-refused-bequest
description: Repair one frozen Java refused-bequest hierarchy finding. Use only when the task language is Java and the smell is refused_bequest; inspect parent contracts, siblings, and callers before implementing, delegating, splitting capabilities, or replacing inheritance.
---

# Refused Bequest Repair

Repair the rejected inheritance contract with real behavior or a justified hierarchy migration. This smell is Java-only in the current product.

## Load the Java route

- Java: read `references/java.md`; for the direct backend, read `../_shared/operation-translations.md` only when an operation shape is unclear. For the IDEA backend, also load `idea-refactor-cli` and read only its `refused_bequest.yaml` route reference.
- Reject Python, C, or C++ tasks as unsupported; do not invent a language route.

## Common workflow

1. Re-anchor the frozen child, parent capability, and rejecting/empty member.
2. Build the source-derived parent/sibling/caller capability matrix before selecting a route.
3. Choose the narrowest correct structure: implement real behavior, delegate to an existing implementation, remove a redundant override, split capabilities, push behavior down, or replace inheritance with composition.
4. Migrate the complete rejection/caller closure. Logging, swallowed exceptions, `null`, constants, and placeholder bodies are not implementations.
5. Call `smell_verify`; use the returned hierarchy rejection set as the exact residual worklist.

## Verification contract

- Preserve subtype and production caller contracts.
- Do not relocate the rejection to another override or capability.
- Let the controller own build/test execution and test-change policy.
