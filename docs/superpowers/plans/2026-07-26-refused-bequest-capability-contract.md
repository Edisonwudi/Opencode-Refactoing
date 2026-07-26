# Refused Bequest Capability Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a positive capability-split contract to refused-bequest verification and measure it on one structural target plus one non-structural control.

**Architecture:** Extend the existing Java tree-sitter semantic model with resolved implemented interfaces and a target capability profile. The refused-bequest guard consumes an explicit evidence expectation and fails closed when the required structural change is absent.

**Tech Stack:** Python 3, tree-sitter Java, existing `smell_core` semantic detector and self-check scripts, Git, Docker, SSH/WSL.

---

### Task 1: Evidence Contract

**Files:**
- Modify: `runtime/python/smell_core/detector_utils.py`
- Test: `scripts/self_check_refused_bequest_guard.py`

- [ ] Add a focused self-check asserting that
  `structural_expectation=capability_split` parses exactly and missing evidence
  produces an empty value.
- [ ] Run `python3 scripts/self_check_refused_bequest_guard.py` and confirm it
  fails because the parser does not exist.
- [ ] Add `parse_structural_expectation()` using the existing semicolon-delimited
  evidence parsing convention.
- [ ] Run the focused self-check and confirm the parser checks pass.

### Task 2: Semantic Capability Profile

**Files:**
- Modify: `runtime/python/smell_core/java/semantic_detector.py`
- Test: `scripts/self_check_refused_bequest_guard.py`

- [ ] Add fixtures for a class implementing an interface, an unchanged parent
  default method, a removed parent relation, and a method removed from both the
  parent capability and child.
- [ ] Run the focused self-check and confirm the new profile assertions fail.
- [ ] Extend `ClassRecord` with resolved interface names and collect method
  declarations independently of method bodies.
- [ ] Add `analyze_refused_bequest_target()` returning a deterministic profile
  for the target class, reported parent, and target method.
- [ ] Run the focused self-check and confirm all profile assertions pass.

### Task 3: Positive Guard

**Files:**
- Modify: `runtime/python/smell_core/java/smell_guards.py`
- Test: `scripts/self_check_refused_bequest_guard.py`

- [ ] Add guard fixtures proving unchanged capability relationships fail while
  both allowed split outcomes pass.
- [ ] Run the focused self-check and confirm the guard assertions fail.
- [ ] Apply the positive contract only when the parsed expectation equals
  `capability_split`; preserve existing behavior for all other rows.
- [ ] Fail closed with profile evidence when the target or parent cannot be
  resolved.
- [ ] Run:
  `python3 scripts/self_check_refused_bequest_guard.py`,
  `python3 scripts/self_check_runner_continue.py`, and
  `python3 scripts/self_check_multilanguage_runner.py`.

### Task 4: Dataset Annotation And Git Synchronization

**Files:**
- Modify: `dataset/java/delivery_schema/refused_bequest.csv`

- [ ] Annotate all rows whose curated remedy requires a capability split or
  replacement, based on their documented refactor path; do not annotate
  implementation/delegation/state-getter rows.
- [ ] Run a dataset scan showing annotated and unannotated refactor paths.
- [ ] Commit only the experiment files and push `main`.
- [ ] In `/home/testuser/delivery-acceptance-test`, run
  `git pull --ff-only origin main` and verify local/remote commit equality.

### Task 5: Minimal Remote Experiment

**Files:**
- No additional source files.

- [ ] Build one candidate image from the pulled remote checkout.
- [ ] Run sample 16 and sample 25 with the same model, agent, verification mode,
  loop policy, and immutable tests as the frozen baseline.
- [ ] Inspect `result.json`, `verify.json`, and complete `diff.patch` for both
  samples.
- [ ] Keep the commit only if sample 16 requires a credible capability change
  and sample 25 remains eligible for its existing-state implementation.
- [ ] If the criterion fails, revert the experiment commit, push it, and
  synchronize the remote checkout with `git pull --ff-only`.
