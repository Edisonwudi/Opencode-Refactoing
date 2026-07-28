# Java Dataset Oracle Alignment Design

## Objective

Repair the mismatch between Java dataset metadata, the delivery image's pinned
project revisions, and sample-test evidence without creating per-smell project
snapshots or rewriting every historical test provenance commit.

After the repair:

- `project-revisions.json` remains the sole authority for project checkout
  revisions.
- Dataset `test_commit` records where a test was curated and is audited as
  provenance; it is not required to equal the authoritative project revision.
- Every declared test file must exist in the pinned checkout and remain inside
  the project.
- Every dataset test command must produce fresh JUnit evidence for the declared
  test files.
- Semicolon-separated test files are verified individually.
- Curated immutable Oracles, currently refused bequest, continue to require
  content hashes. Legacy datasets are not forced to acquire hashes as part of
  this repair.

## Root Cause

The Java runner introduced a strict equality check between dataset
`test_commit` and the authoritative manifest `project_commit`. These fields
serve different purposes: the former records test provenance, while the latter
selects the delivered source tree.

The recent code-clone probes bypassed that check with a temporary CSV that
rewrote commit strings and cleared sample 12 and 17 `test_file` values. This
allowed the experiment to run but removed the sample-level test evidence. It
must not become a supported workflow.

A second defect treats the complete semicolon-separated `test_file` value as
one path when locating JUnit reports. Multi-file samples therefore cannot
provide valid evidence even when their test command succeeds.

## Design

### Revision contract

The runner checks out only the project revision resolved from
`project-revisions.json`. A non-empty dataset `test_commit` is recorded in the
revision audit with one of these provenance states:

- `MATCHES_PROJECT_COMMIT`
- `PRESENT_IN_REPOSITORY`
- `MISSING_FROM_REPOSITORY`
- `NOT_DECLARED`

A mismatch does not stop a run when the declared tests exist and execute in the
pinned checkout. A missing provenance commit is reported but does not replace
the authoritative checkout.

### Test-file contract

The existing `test_file` column remains unchanged. Its value is split on
semicolons into an ordered, non-empty list. Each path must:

- be relative to the project root;
- contain no parent traversal;
- identify an existing regular file in the pinned checkout.

The runner must fail before model execution with an explicit evidence status
when these conditions are not met. It must not silently clear or ignore an
invalid declaration.

### Immutable Oracle contract

The existing single-file `test_oracle_sha256` behavior remains compatible.
For multiple files, hashes use the same semicolon-separated order as
`test_file`. Either no hashes are declared, or the number of hashes equals the
number of test files and every hash is valid SHA-256.

Strict curated datasets may require hashes through their existing structural
contract. Historical datasets may omit them and rely on file existence plus
fresh execution evidence.

### Fresh test-execution evidence

The build/test guard derives one test class name from every declared Java test
file. After running the dataset test command, it searches for fresh JUnit XML
reports for each declared class.

Success requires:

- at least one fresh matching report for every declared test class;
- at least one non-skipped test across those reports;
- no declared class with zero executed tests.

The evidence payload records per-class report paths and executed/skipped
counts, while preserving the aggregate fields used by existing consumers.

### Dataset-wide audit

The existing `scripts/self_check_java_baselines.py` remains the audit entry
point. It will use the same shared parsing and revision-provenance rules as the
runner instead of maintaining a second interpretation.

The audit covers every CSV under `dataset/java/delivery_schema` and reports
separately:

- revision provenance;
- test-file existence;
- immutable hash alignment when declared;
- build success;
- test success and fresh evidence.

Only rows with concrete failures are changed. Historical `test_commit` values
are not mechanically rewritten.

### Remote experiment

After local checks pass, the repository source is synchronized to the Edison
WSL checkout. The experiment uses the repository's canonical code-clone CSV;
no temporary aligned CSV is allowed.

Ten code-clone rows are selected with a fixed recorded sample-ID list. The run
reports strict status, structural guard, build, declared-test evidence, diff
quality, and duration separately. A model or infrastructure failure is not
reported as a dataset-Oracle failure.

## Error handling

New or refined statuses must distinguish:

- project revision unavailable or mismatched;
- test provenance missing;
- declared test file missing or invalid;
- hash schema or content mismatch;
- test command failure;
- test command success without fresh declared-class evidence.

No fallback may silently downgrade a declared dataset test to project-wide
testing or remove the declaration.

## Testing

Implementation follows red-green-refactor:

1. Add failing self-check cases for mismatched-but-present provenance,
   multi-file Oracle hashes, missing files, and per-class fresh JUnit evidence.
2. Implement shared test-file parsing and provenance auditing.
3. Update the runner and guard with the shared functions.
4. Run targeted self-checks, then the complete `npm run check`.
5. Run the dataset-wide audit against the delivery environment.
6. Run ten remote code-clone samples only after the audit is clean for the
   selected rows.

## Non-goals

- Creating one project checkout per smell or sample.
- Rewriting all historical test provenance commits.
- Removing sample-level tests to make experiments run.
- Moving model-guidance mechanisms into the batch runner.
- Replacing the existing runner, guard, or baseline-audit mechanisms.
