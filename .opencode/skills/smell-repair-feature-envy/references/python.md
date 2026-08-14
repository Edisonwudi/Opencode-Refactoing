# Python route

- Before editing, inspect every receiver access and alias in the frozen callable together with source/receiver writes, callbacks, exits, and exception order; investigate the receiver type and nearby owners in source.
- Choose the smallest complete responsibility cluster that can move with its required state interaction, owner entrypoint, and source-side delegation or callers.
- Prefer a method on the real receiver class or a focused module operation when Python's data owner is module-based.
- Preserve properties/descriptors, lazy evaluation, `__getattr__`, context-manager behavior, exceptions, and mutation order.
- Keep a source entrypoint only as one narrow delegate when callers require it. Do not assemble a bulk dictionary or tuple of receiver fields in the source.
- Avoid moving application orchestration into a protocol/model type that should remain a stable port; use a purpose-named workflow object/function for that case.
- Move one coherent cluster, then run the smallest available import/compile and focused behavior checks covering effect order before moving another cluster.
- Search again in the source entrypoint for residual receiver aliases/accesses, copied fallbacks, and bulk snapshots; recheck imports and circular dependencies before verification.
