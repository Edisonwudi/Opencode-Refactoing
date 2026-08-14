# C++ route

- Before editing, inspect both frozen clone endpoints and candidate shared dependencies across declarations, out-of-line definitions, overloads, templates, inheritance, and callers. Treat search hits as evidence to review, not a guaranteed-complete dependency graph.
- Choose the smallest complete consolidation unit that can preserve both endpoint contracts and one legitimate ownership boundary.
- Consolidate the duplicated body into one real implementation: a private member, free helper, base-class operation, or template chosen from the existing ownership boundary.
- Preserve overload resolution, template deduction, value categories, exception guarantees, and RAII lifetime order at both frozen endpoints.
- Keep headers, out-of-line definitions, explicit instantiations, and amalgamated mirrors synchronized; avoid duplicate definitions and ODR violations.
- Delete the copied body rather than moving it into another wrapper or lambda. Thin adapters are acceptable only when they no longer repeat the frozen implementation.
- After introducing the shared implementation, compile the nearest affected targets and run focused tests before broader cleanup.
- Re-search both endpoint bodies and the distinctive duplicated operations after editing; then call `smell_verify`; it owns the configured full project suite.
