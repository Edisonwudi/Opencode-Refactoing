# Python route

- Before editing, trace the deepest branch/loop chain and record its predicates, mutations, exits, exceptions, cleanup, and short-circuit order; confirm the hotspot in source.
- Choose the smallest complete control-flow region whose behavior can be flattened or extracted without splitting a dependent effect sequence.
- Prefer guard returns/continues only when they preserve context-manager and `finally` cleanup.
- Extract a complete branch with explicit inputs/results; preserve exception handlers, `else` clauses on loops/try blocks, short-circuit order, and mutation timing.
- Keep async/generator semantics: moving `await` or `yield` can change scheduling or iteration even when values look identical.
- Transform one coherent hotspot, then run the smallest available parse/import and focused branch checks before changing another hotspot.
- Search again for copied nested logic, equivalent nesting hidden in helpers/comprehensions, and stale branches left beside the new path before verification.
- Do not hide the same nesting in a nested function or comprehension that remains equally difficult to follow.
