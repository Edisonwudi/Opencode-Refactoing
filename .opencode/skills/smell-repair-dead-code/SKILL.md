---
name: smell-repair-dead-code
description: Remove one frozen unused declaration in Java, Python, C, or C++. Use when the task smell is dead_code and the exact declaration must be safely deleted after checking language-specific dynamic, registration, linkage, and override references.
---

# Dead Code Repair

Delete the exact unused declaration and only the local fallout made stale by that deletion.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `references/operation-translations.md` only when needed. For the IDEA backend, also load `idea-refactor-cli` and read only its `dead_code.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route.

## Common workflow

1. Re-anchor the frozen declaration; do not substitute a same-named declaration in another owner.
2. Search the language-specific reference closure, including ordinary calls and non-call registrations or dynamic entrypoints.
3. Treat any real production reference as a blocker to deletion, not permission to stub the body.
4. Delete the complete declaration once and remove only imports, declarations, or private helpers made unused solely by that deletion.
5. Call `smell_verify`; if the target reappears or relocation is reported, repair only that exact closure.

## Verification contract

- Do not replace the body with an empty, logging, throwing, or placeholder implementation.
- Do not delete public hooks or framework entrypoints without explicit source evidence.
- Exact target deletion, immutable tests, and project-full verification remain controller-owned contracts.
