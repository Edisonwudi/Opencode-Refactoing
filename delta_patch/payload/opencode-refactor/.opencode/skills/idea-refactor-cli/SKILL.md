---
name: idea-refactor-cli
description: Use IDEA native refactors and IDEA-backed oldString/newString edits safely during Java smell repair.
---

## When To Use

Use this skill before executing any planned `idea_native`, `idea_edit`, or `mixed` Java source step.

Do not use this skill for ordinary inspection, non-Java files, reflection-only test repairs, XML/YAML/properties/config updates, or fixture edits. Those are `direct_edit` routes.

## Refactor Path Examples

When IDEA CLI is used and the smell type is known, read
`references/refactor-paths/index.md`, then read only the matching smell YAML
file. Treat examples as route patterns, not as task context or mandatory
instructions. The current source code and
`idea_refactor_locate.availableOperations` remain authoritative.

## Required Chain

Always run IDEA refactoring as a draft lifecycle:

1. `locate`
2. `prepare`
3. `apply`
4. `smell_verify`

Use the OpenCode IDEA tools for this lifecycle when they are available:

- `idea_refactor_locate`
- `idea_refactor_prepare`
- `idea_refactor_apply`
- `idea_refactor_revert_last_apply`

Use `idea_edit` for narrow Java source patches. Ordinary Java source patches
use `file`, an exact unique `oldString`, `newString`, and `replaceAll=false`.
Explicit new-file or whole-file replacement steps may use `oldString=""` only
when the plan/path says so. Java source patches should not use
`replaceAll=true` unless every identical occurrence is intentionally part of the
same edit.

Do not manage or pass `draftId` in normal OpenCode execution. The IDEA CLI keeps a current draft per project root: a successful `locate` replaces the current draft, and `prepare` / `apply` operate on that current draft. If you run another `locate`, you intentionally switch the draft target and must prepare again.

Do not call `apply` before a successful `prepare`.

After `prepare` succeeds and `nextCliCommandExample.action` is `apply`, apply
that prepared draft before any further `locate`. A successful `locate` replaces
the current draft, so locating a second clone target, owner type, helper method,
or diagnostic position at this point discards the prepared operation. If you
need to inspect another target before applying, do it before `prepare`; after
prepare, either `idea_refactor_apply` the prepared draft or run another
`idea_refactor_locate` to abandon that un-applied draft and start over.

For multi-target smells such as code clones, one prepared IDEA operation still
has a single current draft. Apply the first prepared operation, then locate and
prepare any follow-up operation for the second target. Do not treat "the second
target still needs work" as an IDEA blocker for the already prepared operation.

Prefer `nextCliCommandExample` / `nextCliCommandExamples` returned by the CLI when present. Those examples already contain the intended action, executable command, argv, arguments JSON, and decisions JSON.

Use the absolute CLI path from the task input when present. Otherwise pass
`ideaRefactorCli` explicitly when known, or rely on `SMELL_IDEA_REFACTOR_CLI`,
`IDEA_REFACTOR_CLI`, or `idea-refactor` on `PATH`.

Bash `idea-refactor` commands are a diagnostic fallback only. For normal refactoring execution, use the OpenCode IDEA tools and pass operation inputs as structured `arguments` / `decisions`.

## Edit And Apply Diagnostics

`idea_edit` applies an OpenCode-style oldString/newString patch through IDEA's
document layer:

```text
idea_edit(file, oldString, newString, replaceAll=false)
```

- `oldString` must be exact and unique for ordinary Java source patches.
- If the old block matches more than once, widen the oldString context instead
  of setting `replaceAll=true`.
- `oldString=""` is reserved for explicit new-file or whole-file replacement
  scenarios, not ordinary refactoring.
- Inspect `postEditProblems` when present. New local parse or symbol problems
  are repair evidence, but `smell_verify` remains the acceptance gate.

## Apply Diagnostics And Imports

`idea_refactor_apply` and `idea_edit` may return IDEA-side post-change feedback in addition to ordinary `diagnostics`.

- `postApplyProblems` / `postEditProblems` report lightweight Java problems newly observed after the change and IDEA postprocessing. They are not Maven/Gradle build results and do not replace `smell_verify`.
- Common problem categories include `JAVA_PARSE_ERROR`, `UNRESOLVED_SYMBOL`, `FILE_REPLACED`, `PACKAGE_CHANGED`, `PUBLIC_TYPE_FILENAME_MISMATCH`, and `ANONYMOUS_CLASS_REPLACE_UNSUPPORTED`.
- `IMPORTS_POST_PROCESSED` means IDEA ran Java reference shortening, formatting, import optimization, document commit, and save for changed Java files.
- For `idea_edit` snippets that introduce a type dependency, write the reference as a fully qualified name first, such as `java.util.List` or `java.util.Collections`, unless the import already exists.
- If IDEA can resolve that FQN, the postprocessor may shorten the reference and add or optimize the corresponding import. This is the preferred way to avoid a later import-only edit for cases where the class exists but the current usage site lacks an import.
- If post-change diagnostics still report `UNRESOLVED_SYMBOL`, fix the import or reference with another narrow `idea_edit`, or keep the FQN if shortening is not safe.

## Service Check

Batch execution normally starts IDEA before the agent runs. In manual interactive runs, if locate fails because the service is unavailable, check or start the service:

```sh
idea-refactor status --project-root <project-root>
idea-refactor ensure-service --project-root <project-root> --open
```

Do not repeatedly open IDEA from the agent after the runner has already prepared the service.

## Locate

Start from the target file and a concrete caret position:

```text
idea_refactor_locate(file, line, column, expectedOperation?)
```

The response should include `resolvedContext`, `availableOperations`, possibly `nextCliCommandExamples`, and may include raw `draftId` for audit/debug.

`availableOperations` is a hard capability signal for the current located target or selection:

- If the intended operation is present, prepare that operation.
- If the intended operation is absent, the current target/selection does not support that operation.
- Do not force `prepare` for an absent operation.
- Relocate to the correct declaration, method, field, type, or explicit selection and check `availableOperations` again.
- If relocation still does not expose the operation, record an IDEA blocker and only then consider fallback.

Examples:

- `extract:method` absent usually means the current selection is not a complete extractable statement block.
- `move:method` absent usually means the located element is not a movable method for IDEA's native move operation.
- `rename:*` absent usually means the caret is not on the declaration or reference kind IDEA can rename.
- `introduce:parameter-object` absent usually means the located element is not a method/constructor signature where that operation is available.

## Target And Caret Discipline

The dataset smell location is only an entry point. Do not assume it is the correct IDEA refactoring target.

Before every IDEA operation, convert the smell location into the PSI target required by that operation:

- Declaration/member operations need the declaration or a caret that resolves to that member.
- Selection operations need an explicit valid selection and a selection-aware draft.
- Statement operations need a caret on or inside a concrete Java statement.
- Expression operations need a caret on or inside a concrete Java expression.
- Member insertion operations need the owning class/type target.
- File/type creation operations need a package, directory, file, or file-level target.

Always inspect these fields after `locate`:

- `resolvedContext.kind`
- `resolvedContext.selectionKind`
- `resolvedContext.selectionSource`
- `availableOperations`

If the intended operation is absent:

1. Do not call `prepare`.
2. Treat the current `locate` as the wrong target unless diagnostics prove the operation is truly unsupported.
3. Relocate to a more specific PSI element appropriate for the operation family.
4. Only record an IDEA blocker after retrying with the correct target kind and still not exposing the operation.

Operation admission matrix:

| Operation | Primary locate target | Retry targets before blocker | Selection required? |
| --- | --- | --- | --- |
| `rename:type` | exact type declaration or type reference token | class name token, reference usage, file entry for file-backed class rename | no |
| `rename:method` | exact method name declaration or call/reference token | method declaration name token, call site method token, override/super reference token | no |
| `rename:field` | exact field declaration or field reference token | field name token, accessor-backed field reference if available | no |
| `rename:local-variable` | exact local variable declaration or usage token | variable declaration token, usage token inside same scope | no |
| `rename:package`, `rename:file`, `rename:directory`, `rename:module` | exact package/file/directory/module target | package declaration token, package directory, file entry | no |
| `move:method` | method name token in the source declaration | method declaration line at the name, receiver parameter/reference for instance method context | no |
| `move:member` | member declaration name token | method/field declaration name, owner class target for static member context | no |
| `move:type`, `copy:type` | type declaration name token | file entry containing the type, nested type declaration token | no |
| `move:package`, `copy:package` | package declaration or package directory | source package directory, package statement token | no |
| `copy:file` | file entry | file-level target | no |
| `extract:method` | explicit complete statement/block selection inside the method body | concrete body statement caret with `suggestSelectionsFor=extract:method`, then returned candidate selection | yes |
| `introduce:variable` | expression token or explicit expression selection | smallest expression, then enclosing expression if IDEA rejects it | sometimes |
| `extract:field`, `extract:constant`, `extract:parameter` | expression token or explicit expression selection | smallest expression, enclosing expression, expression candidate from `suggestSelectionsFor` when available | sometimes |
| `introduce:parameter-object` | method/constructor name token or parameter list | each clumped formal parameter token, return-type-to-name area, constructor/method declaration name, parameter-list token | no |
| `change-signature:method` | method/constructor name token or parameter list | each formal parameter token, declaration name, return-type-to-name area | no |
| `change-signature:class` | class/type name token or type parameter list | class declaration name, type parameter token | no |
| `pullUp:method`, `pushDown:method` | method declaration name in the relevant hierarchy | override declaration, super declaration, method-resolved caret | no |
| `pullUp:field`, `pushDown:field` | field declaration name in the relevant hierarchy | field-resolved caret, owner class target only after field-name retry | no |
| `inline:method` | method call site for inline-this-only, or method declaration for full inline | call expression token, declaration name token | no |
| `invert:boolean` | boolean method or field declaration name | boolean field/method name token, reference token if declaration is not available | no |
| `make-static:method`, `convert-to-instance-method` | method declaration name token | method-resolved caret, receiver/parameter token for conversion context | no |
| `extract:interface`, `extract:superclass`, `extract:class`, `extract:delegate`, `replace-inheritance-with-delegation` | source class/type declaration name token | owner type declaration, nested class name token | no |

For operations that manipulate method parameters, do not locate at column 1 by default. Try the method or constructor name token first, then the parameter list and relevant formal parameter tokens. Parameter subsets for `introduce:parameter-object` are passed as `parameterNames` in `prepare`/`apply`; do not rely on source text selection to choose those parameters.

If the same operation is absent at one caret position but exposed at another signature position, the first `locate` was the wrong admission target, not proof that IDEA does not support the operation.

## Supported Operations

This skill is the authoritative reference for IDEA CLI operation semantics. The smell context may provide `refactor_paths[].preferred_operations`, but those lists are only smell-level strategy guidance. The current `locate.availableOperations` response is the final admission control for the current target or selection. If an operation below is not present in `availableOperations`, relocate to the correct target and check again; do not force `prepare`.

### Admission Notes

The matrix above is the source of truth for operation-specific locate targets. Keep the detailed rules below in mind for high-risk families:

- `rename:*` requires an exact declaration/reference token. A containing line, body statement, or owner type is not enough evidence to call rename unavailable.
- `extract:method` requires a complete statement/block selection. Start from concrete body statements, request `suggestSelectionsFor=extract:method` when uncertain, then rerun `locate` with the returned selection.
- Expression operations (`introduce:variable`, `extract:field`, `extract:constant`, `extract:parameter`) require an expression caret/selection. Retry the smallest intended expression and then an enclosing expression before falling back.
- Parameter operations (`introduce:parameter-object`, `change-signature:method`) should start at the method/constructor name or parameter list, then retry relevant formal parameter tokens. Use `parameterNames` for `introduce:parameter-object`; do not use source text selection just to choose parameters.
- Hierarchy and ownership operations (`move:*`, `pullUp:*`, `pushDown:*`, `extract:interface`, `extract:superclass`, `extract:delegate`, `replace-inheritance-with-delegation`) must preserve semantic ownership. If the correct declaration does not expose the operation, inspect hierarchy/receiver context before choosing direct edits.
- `extract:class` uses structured arguments such as `className` and `memberNames`; choose the cohesive member set from source inspection before prepare/apply.

## Selection Repair

For `extract:method`, the target line is often not a valid extractable statement block. A smell location on a method declaration is useful for finding the method, but it is not a good candidate anchor.

When asking IDEA for extract candidates:

- First inspect the target method body.
- Place `--line` and `--column` on a real statement inside the method body, usually the first statement of the block you may extract.
- Avoid method declaration lines, blank lines, comments, braces, and indentation before a statement.
- If candidates are empty from a declaration or non-statement location, this is not an IDEA blocker. Retry from one or more concrete body statements.
- Only treat candidate generation as blocked after body-statement anchors still return no usable candidates.

Bad candidate anchor:

```text
idea_refactor_locate(file, methodDeclarationLine, 1, suggestSelectionsFor="extract:method")
```

Good candidate anchor:

```text
idea_refactor_locate(file, bodyStatementLine, statementColumn, suggestSelectionsFor="extract:method")
```

If selection is unclear or `extract:method` is absent, ask IDEA for candidates from a statement caret:

```text
idea_refactor_locate(file, line, column, suggestSelectionsFor="extract:method", expectedOperation="extract:method")
```

If `operationCandidates` are returned, choose one candidate range, then rerun `locate` with that explicit selection range before `prepare`:

```text
idea_refactor_locate(file, line, column, selection={startLine, startColumn, endLine, endColumn}, expectedOperation)
```

Do not send selection ranges as `prepare` decisions. Selection changes are made by rerunning `locate`.

After the selection locate, confirm `resolvedContext.selectionKind` is a statement/block-like selection and the intended operation appears in `availableOperations` before preparing it. If `selectionKind` is empty or the target operation is absent, the selection draft is invalid for that operation; relocate or choose another candidate instead of preparing.

## Prepare

Prepare the exact planned operation:

```text
idea_refactor_prepare(operation, arguments?, decisions?, expectedOperation?)
```

If no arguments are known yet, omit the `arguments` object and inspect the returned input contract, defaults, diagnostics, pending decisions, and `nextCliCommandExample`.

If the response says `needs_decision`, do not guess whether the next command is `prepare` or `apply`. Read the returned `nextCliCommandExample.action`:

- If the action is `prepare`, rerun `idea_refactor_prepare` with the returned or chosen structured `decisions`.
- If the action is `apply`, run `idea_refactor_apply` with the returned or chosen structured `decisions`.
- Do not put decision keys inside `arguments`.
- Do not retry the same operation by only adding more ordinary `arguments` while omitting the requested `decisions`.

When `nextCliCommandExample` contains `argumentsJson` and `decisionsJson`, map them directly to the OpenCode tool fields:

```text
idea_refactor_prepare(
  operation,
  arguments=<nextCliCommandExample.argumentsJson>,
  decisions=<nextCliCommandExample.decisionsJson>,
  expectedOperation
)
```

If you choose a different decision than the recommended example, keep the same object shape. For example, a selection decision must be passed as:

```text
decisions={"selection.extract-method.scope":{"choice":"selection_0","arguments":{}}}
```

Some prepare-patch decisions such as `selection.*`, `retarget.*`, or `convert.*` change the draft target, selection, or operation and therefore require another `prepare`. Other refactoring decisions may continue at `apply`.

If a choice exposes `choice.inputs`, fill only those requested fields. Do not invent unrelated decision arguments.

For Java source patch snippets, use `idea_edit` with exact `oldString` and `newString`.
Use `idea_edit` old/new patches for Java source additions, replacements, and removals.

## Apply

Apply only after prepare succeeds:

```text
idea_refactor_apply(arguments?, decisions?)
```

Use the same prepared arguments unless the prepare response explicitly asks for a changed argument or decision.

After apply, inspect changed source as needed, then call `smell_verify`. Do not run Maven or Gradle build/test commands directly; verification belongs to `smell_verify`.

After every successful `apply`, run a fresh `idea_refactor_locate` before the next IDEA operation. Apply mutates the PSI tree and can make the previous locate context stale, even when the next operation targets the same class or nearby member.

Do not treat `REFRESH_LOCATE_FAILED` as a final blocker until you have re-located the intended declaration, owner type, file, or selection and checked `availableOperations` again. If the refreshed locate exposes the operation, continue with a new `prepare`; only record an IDEA blocker after the fresh locate still cannot admit the operation.

If `apply` returns `needs_more_info`, change the missing input or choose another prepared operation. Do not repeat the same invalid apply command.

## Revert Last Apply

If an IDEA apply creates the wrong structural change, revert the most recent successful `apply` before trying a different route:

```text
idea_refactor_revert_last_apply()
```

`idea_refactor_revert_last_apply` reverts the most recent successful `apply`. It is not a way to clear or discard the current locate/prepare draft.

Do not use it to discard the current locate/prepare draft. If you only want to change targets or abandon an un-applied prepared draft, run a fresh `idea_refactor_locate` for the target you want.

After reverting the last apply, rerun `locate`; reverted drafts and previous locate contexts are not reusable. The OpenCode wrapper intentionally does not expose draft handles.

## Fallback Rule

For planned `idea_native` or `idea_edit` steps, direct OpenCode `edit`/`write` is allowed only after IDEA returns a concrete blocker:

- native operation is unavailable or unsupported
- `idea_edit` cannot apply because the file cannot be opened/resolved or the exact oldString cannot be made unique
- valid selection cannot be found after candidate relocation
- draft is stale and relocation does not recover
- prepare or apply fails with a non-decision diagnostic
- the change is non-IDE-visible, such as reflection, string configuration, or fixture/test entry repair

Before fallback editing, write a short `deviation_reason` explaining the IDEA blocker.

## Common Operation Notes

- `introduce:parameter-object` and `change-signature:method` update ordinary Java call sites, but not reflection calls such as `getDeclaredMethod`.
- Use `idea_edit` for method body/declaration patches. Signature-changing patches may still require native `change-signature:method` or explicit caller migrations.
- `rename:*` should be used for declaration renames so references are migrated.
- `pullUp:method`, `pushDown:method`, and `move:method` are valid only when ownership is semantically correct.
