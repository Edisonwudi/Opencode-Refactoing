# Python route

- Before editing, trace the target's dominant contiguous regions with their inputs, outputs, mutations, exits, exception scopes, and cleanup; confirm candidate boundaries in source rather than extracting by line count.
- Choose the smallest complete slice that has one cohesive purpose and can leave the target as readable orchestration.
- Extract an `async` helper from an async target and a generator helper from a generator path; do not change coroutine/generator timing.
- Preserve exception scope, context managers, `finally` cleanup, closure capture, short-circuit evaluation, and mutation order.
- Prefer explicit parameters/results over a helper that mutates a broad outer closure.
- For multi-output slices, return a purpose-named result object or keep the cohesive state in a small method object rather than an unstructured tuple.
- Extract one slice, then run the smallest available import/compile and focused path checks before attempting another independent slice.
- Search again for copied bodies, now-unused locals, broad closure capture, and a helper that merely contains the original long body before verification.
