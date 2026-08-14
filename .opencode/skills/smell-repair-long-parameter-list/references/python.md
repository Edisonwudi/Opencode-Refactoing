# Python route

- Before editing, inspect the frozen declaration, implementation, keyword and positional callers, decorators, subclass overrides, factories, dependency injection, and `*args`/`**kwargs` forwarding; confirm the real callable family in source.
- Choose the smallest complete signature family and one coherent parameter subset that represents a domain concept rather than a convenient count.
- Use a purpose-named dataclass/value object for a coherent subset; preserve defaults, annotations, positional-only and keyword-only boundaries.
- Introduce the value object, migrate the complete callable family and callers as one coherent step, then run the smallest available import/type and focused behavior checks.
- Search again for the old excessive signature, keyword forwarding, compatibility wrappers, and unpacking that recreates the same parameter list.
- Do not keep the old long signature as a forwarding wrapper merely for compatibility.
- Do not replace named parameters with an untyped dictionary or opaque `**options` bag.
