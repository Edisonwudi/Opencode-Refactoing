# C route

- Before editing, inspect the frozen module's globals/static state, public declarations, lifecycle functions, callbacks, macro-selected paths, callers, and build entries. Use these findings to reason about cohesion; do not claim that a textual search is a complete module graph.
- Select the smallest complete state-and-behavior cluster that owns one invariant or lifecycle and contributes materially to the current Guard deficit.
- Treat the frozen C source module as the target and cluster globals/static state with the functions that maintain one invariant or lifecycle.
- Extract a focused `.c`/`.h` module or an owned state struct; migrate initialization, teardown, callbacks, and callers with the cluster.
- Preserve external symbols and ABI deliberately. Remove obsolete globals/static functions and pass-through wrappers from the original module.
- Do not split functions by size alone or create a second module that still mutates the original module's private state directly.
- After one coherent cluster moves, search the original module for residual direct mutations, old declarations, wrappers, callbacks, and initialization or cleanup paths associated with that responsibility. Before calling `smell_verify`, run the nearest cheap compile or focused behavior check; verify once before the next extraction.
