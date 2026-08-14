# C++ route

- Before editing, inspect the frozen control-flow region, mutations, RAII scopes, exits, exception paths, declarations and definitions, template or overload context, and focused tests. Use this investigation to model behavior; do not assume a syntactic scan captures every semantic path.
- Choose the smallest complete branch or decision phase whose inputs, outputs, ownership, and cleanup behavior can remain explicit.
- Use guard clauses, extracted operations, state dispatch, or standard algorithms to flatten control flow while preserving the original decision structure.
- Preserve RAII scope boundaries, destructor timing, short-circuit evaluation, exceptions, fallthrough, and cleanup on every exit path.
- Avoid moving the same nesting into an immediately invoked lambda, macro, callback, or helper with an equally complex body.
- Keep template and overload behavior intact, and make each extracted branch communicate its result or ownership transfer explicitly.
- Compile and run focused branch tests after each control-flow change before flattening another region.
- Re-search for displaced nesting, duplicated conditions, stale helpers, and altered exit paths after editing; then call `smell_verify`; it owns the configured full project suite.
