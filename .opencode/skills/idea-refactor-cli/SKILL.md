---
name: idea-refactor-cli
description: Use IDEA native refactors and IDEA-backed oldString/newString edits safely during Java smell repair.
---

## When To Use

Use this skill before executing any planned `idea_native`, `idea_edit`, or
`mixed` Java source step.

Do not use it for ordinary inspection, non-Java files, reflection-only test
repairs, XML/YAML/properties/config updates, or fixture edits. Those are
`direct_edit` routes.

## Refactor Path Examples

When the smell type is known, read `references/refactor-paths/index.md`, then
only the matching smell YAML. Examples are route patterns, not task context.
The current source and `idea_refactor_preview` result remain authoritative.

## Proposal Protocol

Use one explicit proposal lifecycle:

1. `idea_refactor_preview`
2. resolve a requested input or decision against the same `proposalId`
3. `idea_refactor_apply`
4. inspect the changed paths
5. `smell_verify`

`idea_refactor_preview` performs IDEA locate and prepare internally. Do not call
the underlying CLI `locate` or `prepare` during normal agent execution.

Initial preview accepts exactly one target form:

```text
idea_refactor_preview(
  operation,
  file,
  line,
  column,
  selection?,
  arguments?,
  decisions?
)
```

or:

```text
idea_refactor_preview(
  operation,
  target={fqcn?, memberName?, parameterTypes?, filePath?, packageName?, directoryPath?, moduleName?},
  arguments?,
  decisions?
)
```

The response contains a `proposalId`. Every continuation and apply must use
that exact ID:

```text
idea_refactor_preview(
  operation,
  proposalId=<preview.proposalId>,
  arguments=<updated arguments>,
  decisions={"<decision-id>": {"choice": "<choice-value>", "arguments": {}}}
)

idea_refactor_apply(
  proposalId=<preview.proposalId>,
  arguments=<prepared arguments>,
  decisions={"<decision-id>": {"choice": "<choice-value>", "arguments": {}}}
)
```

Never combine `proposalId` with a new target. To abandon a proposal, start a
fresh preview with a target. Never apply one proposal with the ID returned for
another target or operation.

Use `detail="compact"` unless raw IDEA payloads are needed to diagnose a
protocol defect.

When a response contains `nextRequest`, treat its `tool` and `args` as the
canonical continuation shape. Reuse them directly; change only a value that
the response explicitly leaves for selection. The wrapper projects this request
from IDEA's own `nextCliCommandExample`; do not reconstruct it from prose.

## State Routing

Treat `status` and `nextAction` as a state machine:

| Status | Meaning | Required action |
| --- | --- | --- |
| `ready` | target and operation are prepared | `apply` with this `proposalId` |
| `needs_selection` | target is valid but a concrete selection is required | start a fresh preview with one returned selection |
| `needs_input` | required operation input is missing | preview again with the same `proposalId` and requested arguments |
| `needs_decision` | IDEA requires a structured choice | follow `nextAction` using the same `proposalId` |
| `unsupported_target` | operation is unavailable on this target | relocate according to target admission |
| `retryable_failed` | IDEA asks for a corrected retry | correct only the reported condition |
| `stale` | source changed after preview | create a fresh preview; do not reuse the proposal |
| `applied` | IDEA committed the refactor | inspect paths, then verify |
| `outcome_unknown` | an apply request timed out after dispatch, so source may already be changed | do not repeat apply; inspect and call `smell_verify` |
| `failed` | command or refactor failed | inspect diagnostics |

Do not treat transport completion as refactor completion. Only `ready` may
advance to apply. `applied` advances to normal verification;
`outcome_unknown` advances only to diagnostic verification and must never
repeat apply.

When a decision is requested, use
`decisions={"<id>": {"choice": "<value>", "arguments": {...}}}`. Never pass a
bare choice string, and keep decision keys out of operation `arguments`. If a
choice exposes inputs, fill only those inputs. Prefer the returned
`nextRequest.args.decisions` over manually rebuilding the object.

## Target Admission And Selection

The dataset location is an entry point, not guaranteed to be the final PSI
target. Read `references/target-admission.md` before relocating an unsupported
target, selecting an extraction range, or recording an IDEA blocker.

For `extract:method`, preview a concrete statement anchor first. If the result
is `needs_selection`, choose one returned candidate and invoke that candidate's
`nextRequest`. It is a fresh preview with file/caret/selection and intentionally
contains no `proposalId`. Do not add the old proposal ID or encode a selection
as a decision.

For parameter operations, target the method or constructor name first and pass
subsets via `parameterNames`. For hierarchy or ownership operations, target the
member declaration and inspect the receiver/hierarchy before applying.

## Arguments And Apply

Pass every known operation input in the first preview: names, target owners,
parameter sets, visibility, member sets, and flags. Do not omit known required
input merely to discover its schema.

Apply with the same arguments unless IDEA explicitly returns replacements.
After `applied`, inspect:

- `changedFiles`
- `changedFilePaths`
- `postApplyProblems`
- `diagnostics`

Then call `smell_verify`. Do not run Maven or Gradle directly; the verification
contract belongs to `smell_verify`.

Each successful apply mutates PSI state. Start a fresh preview for every
subsequent IDEA operation, even in the same class.

## IDEA-backed Source Edits

Use `idea_edit` for narrow Java source patches:

```text
idea_edit(file, oldString, newString, replaceAll=false)
```

- `oldString` must be exact and unique for ordinary patches.
- Widen context when the block matches more than once.
- `oldString=""` is only for an explicitly planned new-file or whole-file
  replacement.
- Do not use `replaceAll=true` unless every identical occurrence is part of the
  same edit.
- For a new type dependency, write an FQN first unless the import already
  exists. IDEA postprocessing may shorten it and add the import.
- Inspect `postEditProblems`; `smell_verify` remains the acceptance gate.

## Diagnostics

`postApplyProblems` and `postEditProblems` are IDEA-side Java feedback, not
Maven/Gradle results. Common categories include `JAVA_PARSE_ERROR`,
`UNRESOLVED_SYMBOL`, `FILE_REPLACED`, `PACKAGE_CHANGED`,
`PUBLIC_TYPE_FILENAME_MISMATCH`, and
`ANONYMOUS_CLASS_REPLACE_UNSUPPORTED`.

`IMPORTS_POST_PROCESSED` means IDEA committed documents, shortened resolvable
references, formatted files, optimized imports, and saved the changed Java
files.

## Service Check

The runner owns IDEA service startup and readiness. If preview reports that the
service is unavailable, report the wrapper diagnostic as a concrete
infrastructure blocker. Do not call the underlying CLI through bash and do not
repeatedly open IDEA from the agent session.

## Revert Last Apply

If an applied IDEA refactor is structurally wrong:

```text
idea_refactor_revert_last_apply()
```

This only reverts the most recent successful apply. It does not discard a
preview. After reverting, start a fresh preview; old proposal IDs are not
reusable.

## Fallback Rule

Direct OpenCode `edit`/`write` is not an IDEA-backend fallback. For a planned
native step, `idea_edit` is allowed only after a concrete proposal blocker:
unsupported native operation after target admission, no legal selection,
unrecoverable stale proposal, or non-decision failure. It never substitutes for
the required preview/apply lifecycle in a proposal-contract run.

Before using `idea_edit`, record a short `deviation_reason` with that blocker.

## Common Operation Notes

- `introduce:parameter-object` and `change-signature:method` update ordinary
  Java call sites, not reflection calls such as `getDeclaredMethod`.
- Use native `rename:*` for declarations so references migrate.
- Use `idea_edit` for method body/declaration patches that are not a supported
  native operation.
- `pullUp:method`, `pushDown:method`, and `move:method` are valid only when
  ownership remains semantically correct.
