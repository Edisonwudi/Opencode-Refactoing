---
name: java-smell-edit-patterns
description: Use when repairing Java code smells without IDEA CLI enhancement.
---

# Java Smell Edit Patterns

Use this skill only for the plain Java refactor agent, or when the task
explicitly disables IDEA CLI enhancement.

The unchanged task input remains the source of truth for the requested project
root, smell type, target location, and frozen finding identity. Verification,
test-change, backend, and loop policy come from the separate stable controller
system context. These references provide direct-edit repair patterns; they do
not replace `smell_verify` or duplicate mutable failure details.

`smell_verify` applies a caller-supplied target Guard to the frozen target and
its explicit analysis/change scope. It does not scan every smell or construct a
project-wide finding catalog. Dataset rows, historical metrics, and free-form
evidence may help locate or explain the target, but they never define the Guard
predicate or verdict.

The reference files are the product's source-edit route library. Route ids and
`when` conditions select one source shape, then operations such as
`extract:method`, `move:method`, and `introduce:parameter-object` are executed
with plain OpenCode read/search/edit steps.

## Loading

After reading the complete task input and identifying the smell type:

1. Read `references/edit-patterns/index.md`.
2. Read only the reference file matching the smell type.
3. Choose the route id whose `when` condition matches the actual source shape.
4. Read `references/edit-patterns/operation-translations.md` only if the route's
   operation shape is unclear.
5. Use the `Verification fit` section only to avoid routes that cannot satisfy
   the target Guard returned by `smell_verify`; do not make metric-only edits.
6. For `refused_bequest`, follow the source-derived hierarchy migration protocol
   in `refused_bequest.md` before choosing a route.

## OpenCode Edit Contract

- Inspect with OpenCode read/search tools before editing.
- Edit Java source with OpenCode edit tools using exact, narrow replacements.
- Do not rewrite Java files with shell text commands.
- After edits, call `smell_verify`. If it fails, read `failure_pack` and the
  frozen finding/checkpoint details before choosing the next narrow edit.
- Obey the controller-owned `allow_test_changes` policy. Test edits are blocked
  by default; when explicitly allowed they are fully audited, baseline test files
  may not be deleted, declared tests must actually execute, and the frozen
  project-full build/test command must pass.

## Reference Shape

Each smell reference has:

- `Refactoring intent`: the code-quality goal.
- `Strategy variants`: product route ids and source-derived feature conditions.
- `Routes`: route-specific `When`, `Direct edit target`, edit steps,
  verification delta, and avoid guidance.
- `operation-translations.md`: shared mechanics for operation shapes, kept
  separate so route files stay concise.
- `Common verification fit` and `Common avoid`: smell-level constraints shared
  by every route in that file.
