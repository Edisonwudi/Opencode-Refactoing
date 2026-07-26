# Refused Bequest Capability Contract Design

## Goal

Reject cosmetic removal of `UnsupportedOperationException` when the dataset
requires a capability split, without rejecting legitimate implementations such
as an empty method completed by delegation or a state getter completed from
existing state.

## Decision

Use an explicit, reusable evidence field:

```text
structural_expectation=capability_split
```

This field is only appropriate when the intended repair is to separate an
incompatible parent capability. It is not inferred from sample IDs, project
names, method names, or free-form `refactor_path` text.

The Java semantic model will expose a target profile containing:

- the class that owns the target method;
- its resolved superclass and implemented interfaces;
- whether the target method remains declared by the child;
- whether the reported parent still declares that method.

For `capability_split`, the guard accepts either of these positive structural
outcomes:

1. the target class no longer inherits or implements the reported parent; or
2. the target method has been removed from both the reported parent capability
   and the target child.

Changing a throw expression, moving the exception to a helper/default method,
or returning a placeholder does not satisfy either outcome.

## Scope

The first experiment changes only:

- evidence parsing for `structural_expectation`;
- the reusable Java semantic target profile;
- the refused-bequest guard branch for `capability_split`;
- focused self-check fixtures.

It does not change prompts, sample tests, retry behavior, Maven handling, or
verification modes. It does not add fallback behavior when the semantic profile
cannot be resolved: the guard fails closed with a diagnostic.

## Evaluation

The local self-check must cover:

- unchanged parent relationship: reject;
- exception moved to a parent default method: reject;
- exception hidden behind a helper or field: reject because the capability
  relationship remains;
- parent relationship removed: accept;
- target method removed from both parent and child: accept;
- a non-structural state-getter row: unaffected.

After local RED/GREEN verification, commit and push the change. The clean remote
checkout at `/home/testuser/delivery-acceptance-test` will synchronize it using
`git pull --ff-only`. A candidate image will then rerun sample 16 plus sample 25
as a non-structural control. Automatic status is insufficient: every PASS diff
must be inspected for semantic credibility.

## Keep Or Revert Rule

Keep the experiment only if it blocks the known cosmetic sample-16 shapes,
does not reject sample 25's intended existing-state implementation, and the
model produces a credible capability change. Otherwise revert the experiment
commit and synchronize the revert through the same Git path.
