# IDEA Target Admission

Read this reference when preview reports `unsupported_target` or
`needs_selection`, when the dataset location does not resolve to the required
PSI target, or when an extraction needs an explicit selection.

## Target And Caret Discipline

Convert the smell location into the PSI target required by the operation:

- Declaration/member operations need the declaration or a caret resolving to it.
- Selection operations need an explicit valid selection and a selection-aware draft.
- Statement and expression operations need a caret on the relevant statement or expression.
- Member insertion operations need the owning type.
- File/type creation operations need a package, directory, file, or file-level target.

Inspect `target`, `selectionCandidates`, `status`, and `diagnostics` after every
`preview`. If the intended operation is unsupported, do not apply it. Retry the
operation-appropriate target below before recording an IDEA blocker.

| Operation | Primary preview target | Retry targets before blocker | Selection |
| --- | --- | --- | --- |
| `rename:type` | exact type declaration/reference token | class name, reference usage, file entry for a file-backed class | no |
| `rename:method` | exact method declaration/call token | declaration name, call site, override/super reference | no |
| `rename:field` | exact field declaration/reference token | field name, accessor-backed reference | no |
| `rename:local-variable` | exact local declaration/usage token | declaration or usage in the same scope | no |
| `rename:package`, `rename:file`, `rename:directory`, `rename:module` | exact package/file/directory/module target | package declaration, directory, file entry | no |
| `move:method` | source method declaration name | method name, receiver parameter/reference | no |
| `move:member` | member declaration name | method/field name, then owner type for static members | no |
| `move:type`, `copy:type` | type declaration name | containing file, nested type name | no |
| `move:package`, `copy:package` | package declaration/directory | package directory or statement | no |
| `copy:file` | file entry | file-level target | no |
| `extract:method` | complete statement/block selection | body statement with `suggestSelectionsFor=extract:method`, then a returned candidate | yes |
| `introduce:variable` | expression token/selection | smallest expression, then enclosing expression | sometimes |
| `extract:field`, `extract:constant`, `extract:parameter` | expression token/selection | smallest expression, enclosing expression, suggested candidate | sometimes |
| `introduce:parameter-object` | method/constructor name or parameter list | formal parameters, declaration name, parameter list | no |
| `change-signature:method` | method/constructor name or parameter list | formal parameters, declaration name, return-type-to-name area | no |
| `change-signature:class` | type name or type-parameter list | declaration name or type parameter | no |
| `pullUp:method`, `pushDown:method` | hierarchy method declaration name | override, super declaration, method-resolved caret | no |
| `pullUp:field`, `pushDown:field` | hierarchy field declaration name | field-resolved caret, then owner type | no |
| `inline:method` | call site for inline-this-only; declaration for full inline | call token or declaration name | no |
| `invert:boolean` | boolean method/field declaration name | declaration or reference token | no |
| `make-static:method`, `convert-to-instance-method` | method declaration name | method-resolved caret, receiver/parameter token | no |
| `extract:interface`, `extract:superclass`, `extract:class`, `extract:delegate`, `replace-inheritance-with-delegation` | source type declaration name | owner type or nested type name | no |

For parameter operations, try the method or constructor name first, then the
parameter list and relevant formal parameters. Pass parameter subsets as
`parameterNames`; do not use a text selection to choose parameters.

For hierarchy and ownership operations, inspect the receiver and hierarchy
before using direct edits. For `extract:class`, choose a cohesive `memberNames`
set from the source before prepare.

## Extract Method Selection Repair

Use a concrete statement in the method body as the candidate anchor. Do not use
the method declaration, a blank/comment/brace line, or indentation before the
statement.

```text
idea_refactor_preview(
  operation="extract:method",
  file,
  bodyStatementLine,
  statementColumn,
  arguments={newName, visibility}
)
```

If IDEA returns `needs_selection`, choose one `selectionCandidates` entry and
start a fresh preview with its explicit range:

```text
idea_refactor_preview(
  operation="extract:method",
  file,
  line,
  column,
  selection={startLine, startColumn, endLine, endColumn},
  arguments={newName, visibility}
)
```

Do not pass a selection range as a decision. Continue only when the preview
returns `ready`; retry other body statements before declaring candidate
generation blocked.
