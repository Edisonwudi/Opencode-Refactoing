# Python route

- Before editing, investigate instance/class state together with the methods, properties, descriptors, construction, serialization, and lifecycle hooks that maintain it; infer cohesion from invariants and behavior, not names alone.
- Choose the smallest complete responsibility cluster whose state ownership, behavior, lifecycle, and callers can move together; plan additional independent clusters only when the Guard profile still requires them.
- Extract a real collaborator or module service; preserve cooperative `super()`, dataclass/attrs/Pydantic construction, serialization, and framework discovery.
- Move state ownership rather than copying attributes into a second object. Remove superseded private methods and valueless delegates from the original class.
- Migrate one coherent cluster, then run the smallest available import/compile and focused lifecycle/serialization checks before continuing.
- Search both the original owner and collaborator for duplicated state ownership, stale methods, valueless delegates, and dynamic access to moved members before verification.
- Keep dynamically accessed public attributes or plugin hooks unless callers and configuration are migrated within policy.
