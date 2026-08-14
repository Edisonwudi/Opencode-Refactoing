# C++ route

- Before editing, inspect the envious operation, receiver aliases, accessed state, candidate owners, declarations and definitions, overloads, templates, base or derived overrides, callers, and relevant headers. Use this investigation to form a hypothesis; do not treat search output as an authoritative ownership graph.
- Choose the smallest complete behavior-and-state interaction that can move to one legitimate owner without splitting an invariant.
- Move the cohesive behavior to the class that owns the envied state, or keep it as a free workflow operation when no receiver is a legitimate semantic owner.
- Prefer a real member operation over copying receiver state into a snapshot object merely to reduce access counts.
- Preserve RAII lifetime, aliasing, move/copy behavior, overload resolution, exception safety, and the order of mutations and callbacks.
- Migrate the complete internal call closure. Retain a forwarding entry only for a documented API or ABI need, not as an unneeded second implementation.
- Compile after adding or moving each declaration and definition, then run focused behavior tests before migrating another interaction.
- Re-search the old foreign-access pattern, receiver aliases, forwarding bodies, declarations, and callers after editing; then call `smell_verify`; it owns the configured full project suite.
