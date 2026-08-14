# C route

- Before editing, inspect every case, intentional fallthrough, `default`, macro-selected branch, handler signature, mutation, error code, and cleanup/return path. Confirm the active source shapes instead of assuming search results cover preprocessor variants.
- Choose the smallest complete dispatch unit that preserves one switch contract: its case set, state inputs, outputs, and unknown-value behavior.
- Use constant lookup tables for pure value mapping, function-pointer tables for stable commands, or explicit state-transition tables when the source model supports them.
- Preserve integer/enum coverage, fallthrough, default handling, side-effect order, error codes, and conditional-compilation cases.
- Keep callback signatures and visibility correct across headers and translation units.
- Do not replace the switch with a macro-expanded or equally large `if` chain, and do not eagerly execute handlers during table setup.
- Migrate one complete dispatch. Before calling `smell_verify`, search the frozen target and macro variants for the original switch or type-code chain, inspect every residual handler, default, and fallthrough path, and run the nearest cheap compile or focused behavior check.
