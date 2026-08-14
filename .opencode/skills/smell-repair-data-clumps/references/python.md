# Python route

- Before editing, inspect the Guard occurrences in source and investigate related declarations, overrides/protocols, keyword callers, factories, decorators, and `*args`/`**kwargs` forwarding; confirm relationships semantically rather than treating text matches as one family.
- Choose the smallest complete declaration family that shares one domain concept and can be migrated end to end.
- Use a purpose-named `dataclass`, immutable value object, typed tuple-like object, or existing domain type; do not use a generic dictionary solely to reduce parameters.
- Preserve default values, positional/keyword call behavior, `*`/`**` forwarding, decorators, inheritance, and framework construction hooks.
- Introduce the holder, migrate one complete declaration family and its callers, then run the smallest available import/type and focused behavior checks before taking another family.
- Search again for the separated group in signatures and forwarding wrappers; an old wrapper retaining the separate arguments remains a Guard occurrence.
- Keep unrelated optional parameters outside the holder and validate invariants at the natural ownership boundary.
- If a public keyword signature is immutable, report the compatibility blocker instead of silently changing callers outside the frozen policy.
