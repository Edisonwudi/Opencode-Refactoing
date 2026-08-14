# Operation Translations

Use this file only when a route mentions a source operation and the plain edit mechanics are unclear. Keep actual edits narrow and based on source inspection.

| Source operation | Plain OpenCode edit mechanic |
| --- | --- |
| `direct:edit` | Use one exact oldString/newString replacement for a declaration, block, or insertion anchor after reading the file. |
| `extract:method` | Create the helper manually, pass outer-scope values explicitly, return needed results, and replace the selected block. |
| `introduce:parameter-object` | Create or reuse a holder, change the signature, and update callers to pass the holder. |
| `move:method` | Recreate behavior on the target owner, then rewrite the source/call sites to delegate to that owner. |
| `extract:class` | Create the extracted class, migrate selected state plus maintaining methods, and replace original ownership with a collaborator. |
| `pullUp:method` | Add common behavior to the shared parent and remove equivalent child overrides. |
| `pushDown:method` | Move behavior from a parent to the subclass that owns it, then check whether the parent contract can be narrowed. |
| `inline:method` | Remove a redundant wrapper or replace calls with the inlined expression before deleting the method. |
| `delete:method` | Delete the full declaration only after local reference search is clean. |
| `rename:method` | Rename the declaration and all Java call sites visible in the project. |
| `rename:local-variable` | Rename only inside the lexical scope. |
| `rename:type` | Rename type declaration, constructors, file name when required, imports, and references. |
| `change-signature:method` | Edit the signature and update all callers/body references consistently. |

These mechanics are reusable. Do not repeat them in route files; route files should explain why the route fits and what is different for that route.
