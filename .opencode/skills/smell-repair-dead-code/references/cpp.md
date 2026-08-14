# C++ route

- Before editing, inspect the frozen symbol's candidate declarations, definitions, references, overload or template families, virtual overrides, registrations, function pointers, headers, and build ownership. Treat uncertain or indirect matches as items for semantic review, not proof of reachability or completeness.
- Choose the smallest complete deletion unit: the frozen target plus only artifacts whose purpose depends on it.
- Only an independently reachable production use outside the proposed deletion unit blocks deletion. Self/internal references and artifacts reachable solely through the frozen target are candidate deletion fallout and must be reviewed with that unit.
- Delete the exact frozen declaration or definition and its semantically dependent production artifacts; do not replace it with an empty body, forwarding stub, or unused compatibility symbol.
- Check virtual/override families, explicit template instantiations, registries, function pointers, ADL-visible helpers, generated or amalgamated mirrors, and string-addressed factories before declaring the target unreachable.
- Preserve ODR correctness across headers and translation units, and remove now-unused includes, declarations, definitions, and build entries only when they depend on the deleted target.
- Treat externally required ABI symbols or generated ownership as blockers unless the task contract explicitly authorizes their removal.
- Compile the nearest affected targets after deleting the symbol and again after removing dependent build or header entries; run focused tests before broader cleanup.
- Re-search the frozen identity, aliases, registrations, and stale declarations after editing; then call `smell_verify`; it owns the configured full project suite.
