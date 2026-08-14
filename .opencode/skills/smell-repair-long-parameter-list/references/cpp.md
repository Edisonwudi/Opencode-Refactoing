# C++ route

- Before editing, inspect the long signature across candidate declarations, definitions, constructors, overloads, overrides, templates, callbacks or function pointers, callers, headers, and build entries. Review indirect and generated cases explicitly; do not claim the investigation is automatically complete.
- Choose the smallest complete signature family that can migrate together without leaving an accidental parallel API.
- Replace the coherent subset with a purpose-named value, request, or context type; choose value, reference, or `const&` passing from ownership and lifetime.
- Migrate every related constructor, factory, overload, override, template, declaration, definition, and production caller as one signature family.
- Preserve defaults, conversions, forwarding, value categories, exception guarantees, and public API/ABI behavior.
- Remove the old long overload rather than retaining a delegating wrapper with the same parameter list. Report an immutable compatibility boundary when removal is not authorized.
- Compile after introducing the parameter type and after migrating each signature family; run focused tests before proceeding to another family.
- Re-search the old signature, declarations, calls, overrides, aliases, and delegating adapters after editing; then call `smell_verify`; it owns the configured full project suite.
