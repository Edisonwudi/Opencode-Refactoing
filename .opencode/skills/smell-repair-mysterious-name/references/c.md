# C route

- Before editing, re-anchor the exact declaration and inspect its scope, linkage, prototypes/definitions, direct references, address-taking, macros, registrations, and platform-specific branches. Search provides candidates; confirm symbol identity before renaming.
- Choose the smallest complete symbol unit: one local declaration and its references, or one externally linked declaration/definition family when the source contract permits the rename.
- Rename the exact local, parameter, static function, field, or type selected by its declaration scope.
- Update direct references, prototypes/definitions, designated initializers, function-pointer uses, and related header declarations.
- Check macros, exported symbols, string/config registration, linker-visible names, and platform-specific branches before changing an externally meaningful name.
- Preserve established domain abbreviations and mathematical/iterator roles unless production context proves the name misleading.
- Rename one symbol family. Before calling `smell_verify`, search the old identifier in active code, headers, macros, registrations, and build-visible branches, inspect rather than blindly replace textual matches, and run the nearest cheap compile or focused behavior check.
