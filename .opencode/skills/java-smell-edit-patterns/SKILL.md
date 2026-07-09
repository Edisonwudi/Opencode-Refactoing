---
name: java-smell-edit-patterns
description: Use when repairing Java code smells without IDEA CLI enhancement.
---

# Java Smell Edit Patterns

Use this skill only for the plain Java refactor agent, or when the task
explicitly disables IDEA CLI enhancement.

The task input remains the source of truth for project root, smell type, target
location, evidence, and verification mode. These references provide direct-edit
repair patterns; they do not add hidden context and do not replace
`smell_verify`.

The reference files are the OpenCode read/search/edit counterparts of the
`idea-refactor-cli` refactor path examples. They keep the same route ids and
`when` conditions, then translate operation shapes such as `extract:method`,
`move:method`, and `introduce:parameter-object` into plain OpenCode edit steps.

## Loading

After reading the complete task input and identifying the smell type:

1. Read `references/edit-patterns/index.md`.
2. Read only the reference file matching the smell type.
3. Choose the route id whose `when` condition matches the actual source shape.
4. Read `references/edit-patterns/operation-translations.md` only if the route's
   operation shape is unclear.
5. Use the `Verification fit` section only to avoid routes that cannot satisfy
   `smell_verify`; do not make detector-only edits.

## OpenCode Edit Contract

- Inspect with OpenCode read/search tools before editing.
- Edit Java source with OpenCode edit tools using exact, narrow replacements.
- Do not rewrite Java files with shell text commands.
- After edits, call `smell_verify`. If it fails, read `failure_pack` and the
  failed smell guard details before choosing the next narrow edit.

## Reference Shape

Each smell reference has:

- `Refactoring intent`: the code-quality goal.
- `Strategy variants`: the same route ids and feature conditions as the IDEA
  path library.
- `Routes`: route-specific `When`, `Direct edit target`, edit steps,
  verification delta, and avoid guidance.
- `operation-translations.md`: shared mechanics for operation shapes, kept
  separate so route files stay concise.
- `Common verification fit` and `Common avoid`: smell-level constraints shared
  by every route in that file.
