# C++ route

- Before editing, inspect candidate responsibility clusters through field use, method calls, invariants, construction and destruction, declarations and definitions, inheritance, templates, callers, serialization, and build ownership. Treat the resulting picture as a model judgment, not an automatically complete class graph.
- Choose the smallest complete vertical responsibility slice that includes its owned state, behavior, lifecycle, and necessary callers.
- Extract a coherent state-and-behavior cluster into a focused class or component while leaving the frozen class as a valid definition with a clear remaining responsibility.
- Move fields, methods, invariants, and their tightly coupled helpers together; do not scatter raw pointers or duplicate state across the old and new owners.
- Preserve RAII, copy/move operations, destruction order, serialization, templates, access control, and public API/ABI constraints.
- Keep header declarations, out-of-line definitions, explicit instantiations, and amalgamated sources synchronized, then migrate callers to the new ownership boundary.
- Compile after introducing the new owner and after each lifecycle or caller migration; run focused tests before extracting another responsibility slice.
- Re-search for duplicated state, stale declarations, forwarding-only methods, old ownership paths, and unhandled callers after editing; then call `smell_verify`; it owns the configured full project suite.
