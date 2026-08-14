# Python route

- Before editing, re-anchor the exact symbol and infer its role from assignments, uses, units, lifetime, and surrounding domain language; investigate keyword calls, comprehensions, closures, annotations, imports, and direct references.
- Check decorators, framework conventions, serialization keys, `getattr`/`setattr`, template/config strings, and monkey patches before changing externally meaningful names; inspect plausible string matches instead of renaming them mechanically.
- Keep the migration unit to the exact symbol and its confirmed semantic references; do not rename same-spelled siblings.
- Apply one focused rename, then run the smallest available parse/import and focused behavior checks for the affected entrypoints.
- Search again for confirmed references to the old identifier, stale aliases, keyword calls, and collisions with the successor before verification.
- Do not rename dunder/protocol identifiers or conventional mathematical/iterator locals without source evidence that the role is unclear.
