# C route

- Before editing, inspect the Guard's current declarations and trace related prototypes, definitions, function-pointer typedefs, callbacks, macro wrappers, and production callers. Search results are an investigation aid, not proof of a complete closure.
- Select the smallest related declaration family that can migrate together; do not mix same-typed values from unrelated APIs merely because their spellings match.
- Define one purpose-named `struct` in the natural private or public header and choose pointer/value passing from ownership and ABI needs.
- Migrate complete declarations, definitions, function-pointer typedefs, callbacks, macros, and every production caller.
- Keep unrelated parameters outside the struct; do not use `void *`, an untyped array, or a generic options bag.
- Remove old separate-parameter signatures. If a frozen external ABI requires them, report the blocker instead of keeping Guard-counted compatibility wrappers.
- Complete one coherent declaration family. Before calling `smell_verify`, search for the old separated parameter pattern in prototypes, definitions, typedefs, callbacks, and macro-expanded entrypoints, then run the nearest cheap compile or focused behavior check; inspect every residual rather than assuming equivalence.
