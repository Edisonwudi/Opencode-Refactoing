---
name: smell-repair-mysterious-name
description: Repair one frozen unclear identifier in Java, Python, C, or C++. Use when the task smell is mysterious_name and the exact parameter, local, method/function, class/type, or field must be renamed consistently without changing a protocol-defined name.
---

# Mysterious Name Repair

Rename the exact frozen symbol to a role-revealing name and migrate its complete language-specific reference closure.

## Load one language route

- Java: read `references/java.md`; for the direct backend, read `references/operation-translations.md` only when needed. For the IDEA backend, also load `idea-refactor-cli` and read only its `mysterious_name.yaml` route reference.
- Python: read `references/python.md`.
- C: read `references/c.md`.
- C++: read `references/cpp.md`.

Read exactly one language route. The Java IDEA backend adds one mechanics reference; it does not replace the Java semantic route.

## Common workflow

1. Re-anchor the exact declaration identity, owner/container, kind, and declaration slot.
2. Infer the new name from production semantics, not length alone; reject protocol, mathematical, iterator, or domain-conventional names unless the source proves them unclear.
3. Check conflicts and update the complete reference closure with the language-appropriate rename mechanism.
4. Keep the rename focused; do not combine it with unrelated cleanup or rename a same-named sibling.
5. Call `smell_verify`; use any returned stale reference or successor diagnostic as the exact repair worklist.

## Verification contract

- The new name must be genuinely descriptive, not another placeholder.
- Preserve reflection/configuration strings, keyword calls, macros, overload sets, and public API only as required by the selected language route.
- Let project-full verification decide behavior preservation.
