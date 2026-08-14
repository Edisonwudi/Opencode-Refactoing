# Python route

- Before editing, inspect both frozen callables, the exact clone windows, their inputs, decorators, state access, exits, and exception behavior; use searches as evidence and confirm matches in source.
- Choose the smallest complete migration unit: one purpose-named shared implementation plus replacement of both copied windows. Do not broaden the edit to unrelated similarities.
- Prefer one purpose-named module function, class method, or mixin implementation that both frozen callables can invoke.
- Preserve `async`/generator shape, decorator order, exception timing, mutable-default behavior, and bound-method semantics.
- Pass explicit values instead of closing over unrelated owner state. A nested helper is valid only when both clone endpoints can genuinely share it.
- Add the shared implementation, migrate both endpoints as one coherent step, then run the smallest available import/compile and focused behavior checks for both paths.
- Search again for distinctive statements from both original windows, parallel helpers, and dynamically recreated copies; recheck imports and circular dependencies before verification.
