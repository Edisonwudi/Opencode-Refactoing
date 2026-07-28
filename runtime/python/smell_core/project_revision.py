"""Authoritative project-revision pinning shared by the real refactor runner and baseline-check.

Both ``run_smell_dataset`` (the real OpenCode refactor entry) and ``self_check_java_baselines``
(baseline-check) resolve the checkout target through this module so that:

* the checked-out commit is always the authoritative ``project_commit`` from
  ``project-revisions.json``;
* the runtime HEAD of a project is NEVER used as an implicit fallback;
* ``actual_commit == project_commit`` and ``actual_tree == expected_tree`` are enforced;
* manifest loading is fail-fast (no silent ``{}`` on missing/corrupt/malformed files);
* every deviation fails fast with an explicit, machine-readable status.

Status codes:

Manifest-level (the file itself is wrong):
* ``PROJECT_REVISIONS_FILE_MISSING``    - manifest file does not exist.
* ``PROJECT_REVISIONS_INVALID``         - manifest is not valid JSON.
* ``PROJECT_REVISIONS_SCHEMA_INVALID``  - wrong structure / missing or empty
  ``project_commit`` or ``tree_hash`` / per-entry not an object.

Project-level (manifest is valid but the specific project/commit/tree is wrong):
* ``PROJECT_REVISION_NOT_FOUND``        - no manifest entry for this project_name.
* ``PROJECT_COMMIT_MISSING``            - the pinned commit is absent from the repo.
* ``PROJECT_COMMIT_MISMATCH``           - checked-out commit != pinned commit, or HEAD unreadable.
* ``PROJECT_TREE_MISMATCH``             - checked-out tree != manifest tree.
* ``PROJECT_TREE_UNREADABLE``           - ``git rev-parse HEAD^{tree}`` failed (non-zero rc / empty).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_REVISIONS_PATH = "/opt/opencode-refactor/project-revisions.json"

# Required per-project fields. project_commit and tree_hash MUST be non-empty; an empty
# value is a schema error (not a "skip tree check" sentinel).
REQUIRED_ENTRY_FIELDS = ("project_commit", "tree_hash")


class ProjectRevisionError(Exception):
    """Raised when an authoritative project revision cannot be honored.

    ``status`` carries one of the PROJECT_* codes so callers can record it verbatim.
    """

    def __init__(self, status: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class ResolvedRevision:
    project_name: str
    project_commit: str
    expected_tree_hash: str
    source_image_id: str
    source_image_tag: str
    delivery_image_tag: str
    revisions_path: str


def load_revisions(path: str | Path = DEFAULT_REVISIONS_PATH) -> dict[str, dict[str, Any]]:
    """Load and STRUCTURE-VALIDATE the manifest. Fail-fast; never returns ``{}``.

    Raises:
        ProjectRevisionError(PROJECT_REVISIONS_FILE_MISSING)   - path is not a file.
        ProjectRevisionError(PROJECT_REVISIONS_INVALID)        - JSON parse failure.
        ProjectRevisionError(PROJECT_REVISIONS_SCHEMA_INVALID) - top-level not an object,
            or any entry not an object, or any entry missing/empty project_commit or tree_hash.
    """
    p = Path(path)
    if not p.is_file():
        raise ProjectRevisionError(
            "PROJECT_REVISIONS_FILE_MISSING",
            f"project revisions file not found: {p}",
            revisions_path=str(p),
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProjectRevisionError(
            "PROJECT_REVISIONS_INVALID",
            f"project revisions file is not valid JSON: {p}: {exc}",
            revisions_path=str(p),
        ) from exc
    if not isinstance(data, dict) or not data:
        raise ProjectRevisionError(
            "PROJECT_REVISIONS_SCHEMA_INVALID",
            f"project revisions top-level must be a non-empty object: {p}",
            revisions_path=str(p),
        )
    # The manifest wraps projects under a top-level "projects" key. Accept both the
    # wrapped schema (preferred) and, for robustness, a legacy flat {name: entry} mapping.
    if isinstance(data.get("projects"), dict):
        projects = data["projects"]
    else:
        projects = data
    if not projects:
        raise ProjectRevisionError(
            "PROJECT_REVISIONS_SCHEMA_INVALID",
            f"project revisions has no non-empty 'projects' mapping: {p}",
            revisions_path=str(p),
        )
    for name, entry in projects.items():
        if not isinstance(entry, dict):
            raise ProjectRevisionError(
                "PROJECT_REVISIONS_SCHEMA_INVALID",
                f"project {name!r} entry must be an object, got {type(entry).__name__}",
                revisions_path=str(p), project_name=name,
            )
        for field in REQUIRED_ENTRY_FIELDS:
            val = entry.get(field)
            if not isinstance(val, str) or not val.strip():
                raise ProjectRevisionError(
                    "PROJECT_REVISIONS_SCHEMA_INVALID",
                    f"project {name!r} field {field!r} is missing or empty in {p}",
                    revisions_path=str(p), project_name=name, field=field,
                )
    return projects


def resolve_revision(
    project_name: str,
    revisions: dict[str, dict[str, Any]],
    revisions_path: str,
) -> ResolvedRevision:
    """Resolve the authoritative revision metadata for a project.

    Raises ``ProjectRevisionError(PROJECT_REVISION_NOT_FOUND)`` when the project has no
    manifest entry. ``revisions`` MUST have been produced by :func:`load_revisions` (which
    guarantees schema validity, including non-empty project_commit / tree_hash).
    """
    entry = revisions.get(project_name)
    if not entry:
        raise ProjectRevisionError(
            "PROJECT_REVISION_NOT_FOUND",
            f"project {project_name!r} has no entry in {revisions_path}",
            project_name=project_name, revisions_path=revisions_path,
        )
    return ResolvedRevision(
        project_name=project_name,
        project_commit=str(entry.get("project_commit") or "").strip(),
        expected_tree_hash=str(entry.get("tree_hash") or "").strip(),
        source_image_id=str(entry.get("source_image_id") or "").strip(),
        source_image_tag=str(entry.get("source_image_tag") or "").strip(),
        delivery_image_tag=str(entry.get("delivery_image_tag") or "").strip(),
        revisions_path=revisions_path,
    )


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    # safe.directory=* avoids "dubious ownership" when the project tree is owned by
    # root but the process runs as a non-root user (e.g. the `smell` runuser).
    # surrogateescape keeps non-UTF-8 paths/messages decodable instead of raising.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(repo), *args],
        capture_output=True, text=True,
        encoding="utf-8", errors="surrogateescape",
    )


def assert_commit_present(repo: Path, project_commit: str) -> None:
    """Raise ``PROJECT_COMMIT_MISSING`` if ``project_commit`` is empty or absent from ``repo``.

    ``project_commit`` emptiness is normally caught as a schema error at load time; this is a
    defensive check at the git boundary.
    """
    if not project_commit:
        raise ProjectRevisionError(
            "PROJECT_COMMIT_MISSING",
            f"project_commit is empty for {repo}",
        )
    proc = _git(repo, ["cat-file", "-e", f"{project_commit}^{{commit}}"])
    if proc.returncode != 0:
        raise ProjectRevisionError(
            "PROJECT_COMMIT_MISSING",
            f"project_commit {project_commit} not present in {repo}: {proc.stderr.strip()}",
            project_commit=project_commit,
        )


def verify_test_oracle(
    checkout: Path,
    test_file: str,
    expected_sha256: str,
) -> dict[str, str]:
    """Verify an optional immutable test file against its dataset content hash.

    The project manifest remains the sole checkout authority.  This check only
    proves that the pinned tree contains the same test oracle that the dataset
    was curated against, avoiding a second per-sample revision path.  The file
    and hash form one declaration: either both are absent or both are required.
    """
    test_file = str(test_file or "").strip()
    expected_sha256 = str(expected_sha256 or "").strip().lower()
    if bool(test_file) != bool(expected_sha256):
        raise ProjectRevisionError(
            "TEST_ORACLE_SCHEMA_INVALID",
            "test_file and test_oracle_sha256 must be declared together",
            test_file=test_file,
            expected_test_oracle_sha256=expected_sha256,
        )
    if not test_file:
        return {
            "test_oracle_alignment": "NOT_DECLARED",
            "test_file": "",
            "expected_test_oracle_sha256": "",
            "actual_test_oracle_sha256": "",
        }
    if len(expected_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in expected_sha256
    ):
        raise ProjectRevisionError(
            "TEST_ORACLE_SCHEMA_INVALID",
            "test_oracle_sha256 must be a 64-character SHA256",
            test_file=test_file,
            expected_test_oracle_sha256=expected_sha256,
        )
    path = checkout / test_file
    if not path.is_file():
        raise ProjectRevisionError(
            "TEST_ORACLE_FILE_MISSING",
            f"declared test oracle is missing from pinned checkout: {test_file}",
            test_file=test_file,
            expected_test_oracle_sha256=expected_sha256,
        )
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ProjectRevisionError(
            "TEST_ORACLE_MISMATCH",
            f"test oracle hash mismatch for {test_file}: "
            f"actual {actual_sha256} != expected {expected_sha256}",
            test_file=test_file,
            expected_test_oracle_sha256=expected_sha256,
            actual_test_oracle_sha256=actual_sha256,
        )
    return {
        "test_oracle_alignment": "ALIGNED",
        "test_file": test_file,
        "expected_test_oracle_sha256": expected_sha256,
        "actual_test_oracle_sha256": actual_sha256,
    }


def verify_checkout(worktree: Path, rev: ResolvedRevision) -> dict[str, str]:
    """Verify the freshly checked-out worktree against the manifest (mandatory tree check).

    Tree validation is UNCONDITIONAL: an empty ``expected_tree_hash`` is a schema error
    (rejected at load time) and is never used to skip the check here. Git return codes are
    inspected, not just stdout.

    Raises PROJECT_COMMIT_MISMATCH / PROJECT_TREE_UNREADABLE / PROJECT_TREE_MISMATCH.
    """
    commit_proc = _git(worktree, ["rev-parse", "HEAD"])
    tree_proc = _git(worktree, ["rev-parse", "HEAD^{tree}"])
    # Fail closed on unreadable HEAD.
    if commit_proc.returncode != 0 or not commit_proc.stdout.strip():
        raise ProjectRevisionError(
            "PROJECT_COMMIT_MISMATCH",
            f"could not read actual commit of {worktree}: rc={commit_proc.returncode} "
            f"err={commit_proc.stderr.strip()}",
            requested_project_commit=rev.project_commit, actual_commit="",
        )
    actual_commit = commit_proc.stdout.strip()
    if actual_commit != rev.project_commit:
        raise ProjectRevisionError(
            "PROJECT_COMMIT_MISMATCH",
            f"actual commit {actual_commit} != pinned {rev.project_commit} in {worktree}",
            requested_project_commit=rev.project_commit, actual_commit=actual_commit,
        )
    # Fail closed on unreadable tree.
    if tree_proc.returncode != 0 or not tree_proc.stdout.strip():
        raise ProjectRevisionError(
            "PROJECT_TREE_UNREADABLE",
            f"could not read actual tree of {worktree}: rc={tree_proc.returncode} "
            f"err={tree_proc.stderr.strip()}",
            expected_tree_hash=rev.expected_tree_hash, actual_tree_hash="",
        )
    actual_tree = tree_proc.stdout.strip()
    # Mandatory tree comparison — no conditional skip.
    if actual_tree != rev.expected_tree_hash:
        raise ProjectRevisionError(
            "PROJECT_TREE_MISMATCH",
            f"actual tree {actual_tree} != expected {rev.expected_tree_hash} in {worktree}",
            expected_tree_hash=rev.expected_tree_hash, actual_tree_hash=actual_tree,
        )
    return {
        "requested_project_commit": rev.project_commit,
        "actual_commit": actual_commit,
        "expected_tree_hash": rev.expected_tree_hash,
        "actual_tree_hash": actual_tree,
        "project_revision_alignment": "ALIGNED",
        "project_revisions_path": rev.revisions_path,
        "source_image_id": rev.source_image_id,
        "source_image_tag": rev.source_image_tag,
        "delivery_image_tag": rev.delivery_image_tag,
        # The final delivery image digest is NOT knowable inside the container; it is
        # provenance-signed externally (image-attestation.json). Do NOT emit a fabricated id.
        "delivery_image_id_source": "external_attestation",
    }
