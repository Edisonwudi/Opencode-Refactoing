# C route

- Before editing, inspect the frozen prototype and definition plus related callers, function-pointer typedefs, callbacks, macro wrappers, header declarations, and ABI constraints. Confirm each candidate relationship in source; do not treat text search as an authoritative closure.
- Choose the smallest coherent signature family that can migrate as one unit, and group only parameters representing one domain concept.
- Group one coherent subset in a purpose-named struct declared at the narrowest header boundary; document ownership and mutability through types and `const`.
- Migrate prototypes, definitions, function-pointer typedefs, callbacks, macros, and all production callers together.
- Preserve parameter evaluation/order and ABI. Do not use `void *`, untyped arrays, or a generic catch-all config struct.
- Remove the old long signature; report an immutable external ABI blocker rather than keeping it as a Guard-counted wrapper.
- Complete one signature family. Before calling `smell_verify`, search for the old prototype shape and separated arguments across definitions, typedefs, callbacks, macro entrypoints, and callers, then run the nearest cheap compile or focused behavior check; inspect every residual occurrence.
