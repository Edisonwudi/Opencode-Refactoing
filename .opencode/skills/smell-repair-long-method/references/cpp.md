# C++ route

- Before editing, inspect the frozen method's control flow, state mutations, RAII scopes, exception and return paths, declarations and definitions, overload or template context, and relevant callers. Use the evidence to reason about boundaries rather than assuming a search tool recovered every dependency.
- Choose the smallest complete phase with a coherent purpose and explicit inputs, outputs, ownership, and exit behavior.
- Extract cohesive phases into private members, free helpers, local abstractions, or a method object according to state ownership; avoid helpers that merely mirror arbitrary line ranges.
- Preserve RAII scopes, destruction timing, exception behavior, short-circuiting, overload resolution, templates, and reference/value-category semantics.
- Pass the smallest meaningful state and return explicit results instead of using hidden mutable globals or broad capture lists.
- Keep declarations and definitions synchronized and leave the frozen method as a readable orchestration boundary below the Guard threshold.
- Compile and run focused path tests after each extraction before changing the next phase, especially when scope or destruction timing changes.
- Re-search the frozen method for remaining oversized phases, copied control flow, unused helpers, and stale declarations after editing; then call `smell_verify`; it owns the configured full project suite.
