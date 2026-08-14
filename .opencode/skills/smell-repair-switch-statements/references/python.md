# Python route

- Before editing, inspect the complete frozen `match` or type-code `if`/`elif` chain, including guards, binding, ordering, default/error behavior, mutations, and lazy side effects.
- Treat one complete dispatch target as the minimum migration unit: choose its semantic dispatch owner and account for every branch before replacing the original chain.
- Treat `match` and long type-code `if`/`elif` chains as the language-specific dispatch shapes.
- Use a mapping only for data or callable dispatch with equivalent lazy evaluation; do not call all handlers while constructing the mapping.
- Use class/strategy dispatch only when behavior naturally belongs to types; preserve guards, pattern binding, ordering, and default/error behavior.
- Introduce the destination dispatch and migrate all cases coherently, then run the smallest available parse/import and focused checks for every branch and the default path.
- Search the frozen target and new handlers again for the old chain, duplicated fallback behavior, eager handler evaluation, and an unchanged chain moved into one helper.
