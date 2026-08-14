# C route

- Before editing, trace the deepest branch/loop chain together with mutations, short-circuit conditions, error codes, `errno`, loop exits, labels, callbacks, and every cleanup obligation.
- Choose the smallest complete control-flow unit that can be flattened or extracted without separating an effect from its guard; source inspection, not a generated list, establishes that boundary.
- Use guard exits only when they preserve all required cleanup; consolidate cleanup through existing labels rather than leaking resources.
- Extract one complete branch/loop with explicit state and error propagation. Preserve short-circuit order, mutations, `errno`, fallthrough, and partial effects.
- A state table or function-pointer dispatch is appropriate only when it makes the transition model explicit.
- Do not copy nested logic into macros or a helper while leaving the original path active.
- Transform one coherent hotspot, then re-read all affected exits and cleanup labels and search for the original nested body or condition chain left active in macros or helpers. Before calling `smell_verify`, run the nearest cheap compile or focused behavior check; verify once before the next transformation.
