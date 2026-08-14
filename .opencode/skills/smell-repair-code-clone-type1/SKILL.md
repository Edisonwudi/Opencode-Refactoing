---
name: smell-repair-code-clone-type1
description: Repair one frozen type-1 code-clone pair in Java, Python, C, or C++. Use when the task smell is code_clone_type1 and the duplicated implementation must be centralized without moving or recreating the clone.
---

# Type-1 Code Clone Repair

Centralize the exact duplicated behavior behind one real implementation while preserving both entry paths.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `references/operation-translations.md` only when an operation shape is unclear. For the IDEA backend, also load `idea-refactor-cli` and read only its `code_clone_type1.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route. Do not load another smell skill or infer a language from file contents when the task already declares it.

## Common workflow

1. Re-anchor both frozen declarations and the exact duplicated token window returned by the target Guard.
2. Identify the narrowest semantic owner shared by both paths: an existing owner, a common base, or a focused helper module/type.
3. Move the behavior once, replace both clone windows with calls or delegation, and remove trial helpers or residual copies.
4. Preserve ordering, exceptions, state changes, ownership, visibility, and every caller contract.
5. Call `smell_verify`. If it returns `IMPROVED`, use only its current endpoints and residual clone witnesses as the next worklist.

## Verification contract

- The frozen pair and exact window, not dataset evidence, define the clone.
- A moved duplicate or two parallel helpers is not resolution.
- Keep tests unchanged unless the controller explicitly allows audited test migration.
- Let `smell_verify` own the configured build and test commands.
