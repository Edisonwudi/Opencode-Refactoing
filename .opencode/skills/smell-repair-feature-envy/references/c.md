# C route

- Before editing, inspect the frozen function, receiver aliases, owning module/header, all receiver reads and writes, callbacks, callers, error exits, and cleanup labels. Confirm candidates in source rather than treating search output as a complete dependency model.
- Choose the smallest complete access-and-effect cluster whose responsibility clearly belongs with the selected state owner; keep unrelated orchestration at the source entrypoint.
- Treat the envied receiver as the selected struct/module state. Prefer a focused operation in the module that owns that state, taking the receiver pointer explicitly.
- Keep orchestration in the source when behavior spans unrelated modules; extract one purpose-named workflow function rather than expanding the receiver API incorrectly.
- Preserve pointer aliasing, ownership, mutation/error order, callbacks, output writes, and partial-failure cleanup.
- Do not cache fields into locals, expose a bulk snapshot struct, or move the same body to another source helper solely to lower access counts.
- Move one coherent cluster and update its declaration, definition, and production caller. Before calling `smell_verify`, search the original function for remaining receiver aliases or accesses, inspect moved error-code and cleanup paths for duplicated or reordered effects, and run the nearest cheap compile or focused behavior check.
