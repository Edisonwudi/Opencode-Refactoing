# C route

- Before editing, trace the target's control and data flow around candidate slices: inputs, outputs, pointer aliases, error codes, `errno`, labels, `goto` cleanup, callbacks, and allocation/release order.
- Choose the smallest contiguous semantic slice with an explicit contract; do not assume a search or line range captures every effect without reading its branches and exits.
- Extract a file-local helper when possible; use explicit parameters/results and a purpose-named result struct for genuine multi-output slices.
- Preserve pointer aliasing, ownership, error codes, `errno`, labels, `goto` cleanup, and the exact order of allocation/release.
- Do not turn local state into unsafe globals or add many out-parameters that merely move the same complexity.
- Keep callback-compatible or externally visible target signatures unchanged unless the selected route migrates their full production contract.
- Extract one coherent slice, then re-read every changed exit and cleanup path and search for copied residual logic, unused helpers, or complexity merely relocated into a macro. Before calling `smell_verify`, run the nearest cheap compile or focused behavior check; verify once before the next extraction.
