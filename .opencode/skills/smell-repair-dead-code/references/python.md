# Python route

- Before editing, investigate ordinary calls plus decorators, class registration, signals, routes, command discovery, serialization hooks, reflection strings, `getattr`/`setattr`, and monkey-patch assignments; inspect each plausible match in context.
- Treat dunder methods, framework naming conventions, and externally imported public names as live unless source evidence proves otherwise.
- Only an independently reachable production use outside the proposed deletion unit blocks deletion. Self/internal references and artifacts reachable solely through the frozen target are candidate deletion fallout and must be reviewed with that unit.
- Keep the migration unit to the exact decorated declaration and imports/private helpers made stale solely by its deletion.
- Delete the target first, run the smallest available import/compile and focused discovery checks, then remove only confirmed local fallout.
- Search again for executable and registration references to the old symbol and confirm no stub, alias, or dynamic replacement recreates it.
- Do not replace the target with `pass`, `...`, `NotImplemented`, a warning, or a dynamically assigned equivalent.
