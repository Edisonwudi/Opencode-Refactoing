# C++ route

- Before editing, inspect the repeated parameter group across candidate declarations, definitions, constructors, overloads, overrides, templates, callbacks or function-pointer types, callers, headers, and build entries. Review these candidates yourself; do not assume textual search is complete.
- Choose the smallest complete signature family whose related declarations, definitions, and production callers can be migrated together.
- Introduce a purpose-named value or request type at the natural ownership boundary; choose value, reference, or `const&` passing from lifetime and ABI requirements.
- Migrate complete constructor, factory, overload, override, template, declaration, definition, and production-call families in every maintained source representation.
- Preserve defaults, forwarding, conversions, ownership, value categories, and exception behavior. Keep unrelated parameters outside the holder.
- Remove the old separated-parameter overloads. If a public ABI cannot change without retaining Guard-counted wrappers, report that blocker rather than hiding the group behind a compatibility layer.
- Compile after introducing the type and again after migrating each signature family; run focused tests before continuing to another family.
- Re-search for the old parameter sequence, declarations, calls, overrides, aliases, and adapters after editing; then call `smell_verify`; it owns the configured full project suite.
