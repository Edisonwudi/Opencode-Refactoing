# C++ route

- Before editing, inspect the frozen switch, case dependencies, declarations and definitions, enum or variant ownership, overloads, templates, inheritance, callers, headers, and maintained mirrors. Treat this as candidate evidence for model judgment, not an automatically complete dispatch inventory.
- Choose the smallest complete dispatch family that preserves all cases and one natural ownership boundary.
- Replace the frozen dispatch with the smallest natural abstraction: enum-indexed table, `std::variant` visitation, strategy object, virtual dispatch, or focused lookup.
- Preserve case ordering, intentional fallthrough, default handling, exception behavior, lazy initialization, and object lifetime.
- Keep `constexpr` and compile-time behavior where required, and avoid introducing initialization-order or ODR hazards across headers and translation units.
- Do not relocate the same switch into a helper, lambda, macro, or second source representation; migrate the complete maintained dispatch family.
- Compile after introducing the dispatch abstraction and after migrating each declaration or definition family; run focused case tests before broader cleanup.
- Re-search for the frozen switch, duplicated case mappings, stale declarations, and maintained mirrors after editing; then call `smell_verify`; it owns the configured full project suite.
