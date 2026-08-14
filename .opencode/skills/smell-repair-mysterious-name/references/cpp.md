# C++ route

- Before editing, inspect the frozen declaration identity and candidate semantic references across declarations and definitions, overloads, templates, namespaces, inheritance, macros, registries, strings, headers, and build-facing exports. Treat matches as evidence to classify, not a guarantee that every reference is known.
- Choose the smallest complete rename scope that preserves one symbol identity and any explicitly authorized external contract migration.
- Rename the exact frozen declaration and its references within the validated scope, accounting for namespaces, overloads, templates, shadowing, and ADL.
- Choose a domain-specific name that improves meaning; conventional iterators, coordinates, mathematical symbols, protocol fields, and short local loop indices are not automatically suspicious.
- Preserve macro contracts, serialization keys, reflection or registry strings, exported symbols, and public API/ABI names unless the task explicitly includes their migration.
- Do not create a same-named decoy or rename another declaration to make the frozen target disappear; verification must follow the original declaration identity.
- Compile after changing declarations and definitions, then run focused tests before extending the rename into an authorized external surface.
- Re-search the old name with symbol context, plus stale declarations, specializations, overrides, registry strings, and accidental mixed naming after editing; then call `smell_verify`; it owns the configured full project suite.
