# C route

- Before editing, inspect both frozen clone windows, their surrounding declarations, macro branches, shared headers, callers, and any callback-compatible signatures. Treat search hits as candidates and confirm them against the compiled source path.
- Choose the smallest complete sharing boundary: normally one file-local helper, or one focused production module when the endpoints are in different translation units.
- Centralize the duplicate in one `static` helper when both endpoints share a translation unit; otherwise use a focused production module with the narrowest necessary header declaration.
- Pass explicit state and preserve pointer aliasing, ownership, error codes, `errno`, cleanup order, and macro-controlled behavior.
- Update both endpoints and remove both copied windows. Do not replace them with parallel macros or duplicate inline header bodies.
- If callbacks or function pointers constrain the public signatures, keep those entrypoints as thin calls to the one shared implementation.
- Before calling `smell_verify`, search both original windows and macro variants for residual copied bodies or an unused trial helper, then run the nearest cheap compile or focused behavior check. Verify once before broadening the change.
