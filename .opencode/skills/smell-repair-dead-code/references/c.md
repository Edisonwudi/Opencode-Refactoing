# C route

- Before editing, re-anchor the exact declaration and investigate ordinary references plus address-taking, callback tables, registration macros, linker sections, exported headers, conditional compilation, and string/config dispatch.
- Treat reference search as candidate evidence: confirm linkage, active preprocessor branches, and the build-selected translation units before deciding that deletion is safe.
- Only an independently reachable production use outside the proposed deletion unit blocks deletion. Self/internal references and artifacts reachable solely through the frozen target are candidate deletion fallout and must be reviewed with that unit.
- Distinguish file-local `static` declarations from externally linked symbols; do not infer unused external API status from one translation unit.
- Delete the complete declaration/definition and clean only includes, prototypes, or static helpers whose sole use disappeared.
- Do not leave an empty function, macro alias, or replacement symbol with the same role.
- Keep the deletion as one minimal unit. Before calling `smell_verify`, search the exact symbol, aliases, registrations, and obsolete prototype for residual or accidental relocation, then run the nearest cheap compile or focused behavior check. Do not delete a secondary candidate in the same unit.
