#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.loop_policy import (  # noqa: E402
    LoopPolicy,
    parse_command_policy,
    resolve_command_payload,
)
from smell_core.location import split_location_descriptors  # noqa: E402
from smell_core.target_context import parse_target_context_json  # noqa: E402
from smell_core.project_revision import (  # noqa: E402
    DEFAULT_REVISIONS_PATH,
    ProjectRevisionError,
    audit_test_commit,
    assert_commit_present,
    load_revisions,
    resolve_revision,
    verify_checkout,
    verify_test_oracle,
)


OPENCODE_BATCH_API_KEY_ENV = "SMELL_OPENCODE_API_KEY"
ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_PROVIDER_MODELS: dict[str, Any] = {
    "glm-4.7": {
        "name": "GLM-4.7",
        "limit": {"context": 200000, "output": 131072},
        "modalities": {"input": ["text"], "output": ["text"]},
        "tool_call": True,
        "reasoning": True,
        "temperature": True,
        "status": "active",
        "options": {"thinking": {"type": "disabled"}},
    },
    "glm-4.5-air": {
        "name": "GLM-4.5-Air",
        "limit": {"context": 98304, "output": 16384},
        "modalities": {"input": ["text"], "output": ["text"]},
        "tool_call": True,
        "reasoning": True,
        "temperature": True,
        "status": "active",
    },
}

FINAL_VERIFICATION_MODES = {"sample_optimized", "project_full"}

@dataclass(frozen=True)
class Sample:
    sample_id: str
    language: str
    smell: str
    project_name: str
    project_root: Path
    location: str
    evidence: str
    raw: dict[str, str]
    target_context: dict[str, Any] = field(default_factory=dict)
    test_location: str = ""
    test_command: str = ""
    verification_mode: str = ""
    canonical_project_root: Path | None = None
    sibling_revision_audit: tuple[dict[str, str], ...] = ()


def _run(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=stdout,
        stderr=stderr,
        timeout=timeout,
        check=False,
    )


def _git(project_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-c", "safe.directory=*", *args], project_root)


def _idea_refactor_cli() -> str:
    return (
        os.environ.get("SMELL_IDEA_REFACTOR_CLI", "").strip()
        or os.environ.get("IDEA_REFACTOR_CLI", "").strip()
        or "idea-refactor"
    )


def _sanitize_idea_service_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    server = sanitized.get("server")
    if isinstance(server, dict):
        sanitized_server = dict(server)
        sanitized_server.pop("token", None)
        sanitized["server"] = sanitized_server
    sanitized.pop("token", None)
    return sanitized


def _prepare_idea_service(project_root: Path, sample_dir: Path) -> dict[str, Any]:
    precheck_timeout = max(30, min(600, int(os.environ.get("SMELL_IDEA_PRECHECK_TIMEOUT", "300"))))
    proc = _run(
        [
            _idea_refactor_cli(),
            "ensure-service",
            "--project-root",
            str(project_root),
            "--open",
            "--timeout",
            str(precheck_timeout),
            "--poll-interval",
            "1",
        ],
        project_root,
        timeout=precheck_timeout + 60,
    )
    try:
        decoded = json.loads(proc.stdout)
        payload = decoded if isinstance(decoded, dict) else {"status": "failed"}
    except json.JSONDecodeError:
        payload = {
            "status": "failed",
            "diagnostics": [{"code": "IDEA_PRECHECK_INVALID_JSON", "summary": proc.stderr or proc.stdout}],
        }
    payload = _sanitize_idea_service_payload(payload)
    payload["returncode"] = proc.returncode
    payload["stderr"] = proc.stderr
    (sample_dir / "idea-preflight.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    return payload


def _close_idea_project(project_root: Path, sample_dir: Path) -> None:
    try:
        proc = _run(
            [_idea_refactor_cli(), "close-project", "--project-root", str(project_root)],
            project_root,
            timeout=60,
        )
        try:
            decoded = json.loads(proc.stdout)
            result = decoded if isinstance(decoded, dict) else {"status": "unknown"}
        except json.JSONDecodeError:
            result = {"status": "invalid_json"}
        payload = {
            "returncode": proc.returncode,
            "result": _sanitize_idea_service_payload(result),
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        payload = {"returncode": 124, "result": {"status": "timeout"}, "stderr": ""}
    (sample_dir / "idea-close-project.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


def _dataset_evidence(row: dict[str, str | None]) -> str:
    """Preserve the dataset evidence verbatim as audit-only metadata."""
    return str(row.get("evidence") or "").strip()


def _dataset_target_context(row: dict[str, str | None]) -> dict[str, Any]:
    """Load explicit selector identity without consulting oracle evidence."""
    return parse_target_context_json(row.get("target_context_json"))


def _load_samples(dataset: Path) -> list[Sample]:
    with dataset.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"sample_id", "language", "smell_type", "project_name", "project_path", "location"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{dataset} is missing columns: {', '.join(sorted(missing))}")
        samples: list[Sample] = []
        for row in reader:
            smell = str(row["smell_type"] or "").strip()
            location = str(row["location"] or "").strip()
            if smell in {
                "long_method",
                "long_parameter_list",
                "nested_complexity",
                "switch_statements",
            } and ":method=" not in location:
                raise ValueError(
                    f"{smell} dataset location must contain an explicit method selector: "
                    f"{location!r}"
                )
            verification_mode = str(row.get("verification_mode") or "").strip()
            samples.append(
                Sample(
                    sample_id=str(row["sample_id"]),
                    language=str(row["language"] or "java"),
                    smell=smell,
                    project_name=str(row["project_name"]),
                    project_root=Path(row["project_path"]).expanduser().resolve(),
                    location=location,
                    evidence=_dataset_evidence(row),
                    raw={str(k): str(v) for k, v in row.items()},
                    target_context=_dataset_target_context(row),
                    test_location=str(row.get("test_file") or "").strip(),
                    test_command=str(row.get("test_command") or "").strip(),
                    verification_mode=verification_mode,
                )
            )
    return samples


def _filter_samples(samples: list[Sample], args: argparse.Namespace) -> list[Sample]:
    sample_ids = set(args.sample_id or [])
    projects = set(args.project or [])
    selected: list[Sample] = []
    for sample in samples:
        if args.smell and sample.smell != args.smell:
            continue
        if projects and sample.project_name not in projects:
            continue
        if sample_ids and sample.sample_id not in sample_ids:
            continue
        selected.append(sample)
    if args.offset:
        selected = selected[args.offset :]
    if args.limit is not None:
        selected = selected[: args.limit]
    return selected


def _effective_verification_mode(sample: Sample, args: argparse.Namespace) -> str:
    cli_mode = str(args.verification_mode or "project_full").strip() or "project_full"
    if cli_mode not in FINAL_VERIFICATION_MODES:
        raise ValueError(
            f"Unsupported verification mode '{cli_mode}'. Expected one of: "
            f"{', '.join(sorted(FINAL_VERIFICATION_MODES))}."
        )
    sample_mode = str(sample.verification_mode or "").strip()
    requested = sample_mode or cli_mode
    # Once tests may change, a sample-only command can be edited together with
    # the implementation and is no longer an independent behavior oracle.
    # Use the project's complete frozen test command for every such task,
    # regardless of the dataset row's ordinary optimization hint.
    if getattr(args, "allow_test_changes", False):
        requested = "project_full"
    if requested == "sample_optimized" and not sample.test_location.strip():
        raise ValueError(
            "SAMPLE_ORACLE_TEST_FILE_MISSING: sample_optimized verification requires "
            f"sample {sample.sample_id} to declare test_file"
        )
    if requested == "sample_optimized" and not sample.test_command.strip():
        raise ValueError(
            "SAMPLE_ORACLE_TEST_COMMAND_MISSING: sample_optimized verification requires "
            f"sample {sample.sample_id} to declare test_command"
        )
    if requested not in FINAL_VERIFICATION_MODES:
        raise ValueError(
            f"Unsupported verification mode '{requested}'. Expected one of: "
            f"{', '.join(sorted(FINAL_VERIFICATION_MODES))}."
        )
    return requested


def _sanitize(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return cleaned.strip("-") or "sample"


def _remap_text(value: str, source_root: Path, target_root: Path) -> str:
    if not value:
        return value
    source = str(source_root)
    target = str(target_root)
    return value.replace(source, target)


def _restore_worktree_build_wrappers(canonical_root: Path, worktree: Path) -> None:
    """Copy gitignored/untracked build-wrapper bootstrap files the worktree lacks.

    The isolated Git checkout only contains tracked files, so build wrappers
    whose bootstrap artifacts are gitignored (e.g. Maven Wrapper's
    ``.mvn/wrapper/maven-wrapper.jar`` or Gradle's
    ``gradle/wrapper/gradle-wrapper.jar``) are missing in the worktree and break
    offline builds. Restore them from the canonical working tree so the worktree
    is buildable. Idempotent: only copies files that are absent in the worktree.
    """
    for wrapper_rel in (".mvn/wrapper", "gradle/wrapper"):
        src_dir = canonical_root / wrapper_rel
        if not src_dir.is_dir():
            continue
        dst_dir = worktree / wrapper_rel
        for src_file in src_dir.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src_dir)
            dst_file = dst_dir / rel
            if dst_file.exists():
                continue
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)


def _remove_worktree_checkout(canonical_root: Path, worktree: Path) -> None:
    """Remove either a legacy linked worktree or the current isolated clone."""
    if (worktree / ".git").is_file():
        _git(canonical_root, ["worktree", "remove", "--force", str(worktree)])
    shutil.rmtree(worktree, ignore_errors=True)


def _execution_checkout_run_dir(run_dir: Path) -> Path:
    """Keep executable Git metadata on the container's native filesystem."""
    return Path(tempfile.gettempdir()) / "opencode-refactor-worktrees" / run_dir.name


def _declared_sibling_projects(sample: Sample) -> list[str]:
    raw = str(sample.raw.get("checkout_sibling_projects") or "").strip()
    return [
        name.strip()
        for name in re.split(r"[|,;]", raw)
        if name.strip()
    ]


def _prepare_worktree(
    sample: Sample,
    run_dir: Path,
    *,
    target_commit: str,
    revisions: dict[str, dict[str, Any]] | None = None,
) -> Sample:
    """Create an isolated checkout pinned to ``target_commit``.

    ``target_commit`` is REQUIRED: there is no HEAD fallback. Callers (both the real
    refactor runner and baseline-check) must resolve the authoritative project_commit
    via :mod:`smell_core.project_revision` and pass it here.
    """
    if not target_commit:
        raise RuntimeError(
            "_prepare_worktree requires a non-empty target_commit; HEAD fallback is forbidden"
        )
    canonical_root = sample.project_root.resolve()

    worktree = run_dir / "worktrees" / f"sample-{_sanitize(sample.sample_id)}" / canonical_root.name
    if worktree.exists():
        _remove_worktree_checkout(canonical_root, worktree)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    # Keep a regular .git directory in each isolated checkout. Some build
    # plugins (notably MyBatis' mycila license plugin through JGit) treat the
    # .git pointer file created by `git worktree` as a bare repository. A
    # shared local clone preserves isolation without duplicating Git objects
    # and works with both command-line Git and those plugins.
    proc = _git(canonical_root, ["clone", "--shared", "--no-checkout", str(canonical_root), str(worktree)])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"failed to create isolated checkout at {worktree}")
    # Pin to the authoritative target_commit. Verified against the manifest tree by the
    # caller via smell_core.project_revision after the worktree is materialized.
    verify_proc = _git(canonical_root, ["cat-file", "-e", f"{target_commit}^{{commit}}"])
    if verify_proc.returncode != 0:
        _remove_worktree_checkout(canonical_root, worktree)
        raise RuntimeError(
            f"target_commit {target_commit} does not exist in {canonical_root}: "
            f"{verify_proc.stderr or verify_proc.stdout}"
        )
    checkout_proc = _git(worktree, ["checkout", "--detach", target_commit])
    if checkout_proc.returncode != 0:
        _remove_worktree_checkout(canonical_root, worktree)
        raise RuntimeError(checkout_proc.stderr or checkout_proc.stdout or f"failed to check out {target_commit} at {worktree}")
    filemode_proc = _git(worktree, ["config", "core.filemode", "false"])
    if filemode_proc.returncode != 0:
        raise RuntimeError(filemode_proc.stderr or filemode_proc.stdout or "failed to configure worktree filemode")

    _restore_worktree_build_wrappers(canonical_root, worktree)

    sibling_audits: list[dict[str, str]] = []
    sibling_projects = _declared_sibling_projects(sample)
    if sibling_projects and revisions is None:
        raise RuntimeError("sibling project checkout requires the authoritative revisions manifest")
    for project_name in sibling_projects:
        sibling_root = canonical_root.parent / project_name
        if not sibling_root.is_dir():
            raise ProjectRevisionError(
                "SIBLING_PROJECT_ROOT_MISSING",
                f"declared sibling project {project_name} is missing beside {canonical_root}",
                project_name=project_name,
                project_root=str(sibling_root),
            )
        sibling_revision = resolve_revision(project_name, revisions or {}, "in-memory revisions")
        assert_commit_present(sibling_root, sibling_revision.project_commit)
        sibling_checkout = worktree.parent / sibling_root.name
        if sibling_checkout.exists():
            shutil.rmtree(sibling_checkout)
        clone_proc = _git(
            sibling_root,
            ["clone", "--shared", "--no-checkout", str(sibling_root), str(sibling_checkout)],
        )
        if clone_proc.returncode != 0:
            raise RuntimeError(
                clone_proc.stderr
                or clone_proc.stdout
                or f"failed to create sibling checkout at {sibling_checkout}"
            )
        checkout_proc = _git(
            sibling_checkout,
            ["checkout", "--detach", sibling_revision.project_commit],
        )
        if checkout_proc.returncode != 0:
            shutil.rmtree(sibling_checkout, ignore_errors=True)
            raise RuntimeError(
                checkout_proc.stderr
                or checkout_proc.stdout
                or f"failed to check out sibling {project_name}"
            )
        _restore_worktree_build_wrappers(sibling_root, sibling_checkout)
        sibling_audit = verify_checkout(sibling_checkout, sibling_revision)
        sibling_audits.append(
            {
                "project_name": project_name,
                "canonical_project_root": str(sibling_root),
                "execution_project_root": str(sibling_checkout),
                **sibling_audit,
            }
        )

    return replace(
        sample,
        project_root=worktree.resolve(),
        canonical_project_root=canonical_root,
        sibling_revision_audit=tuple(sibling_audits),
        location=_remap_text(sample.location, canonical_root, worktree.resolve()),
        evidence=_remap_text(sample.evidence, canonical_root, worktree.resolve()),
        test_location=_remap_text(sample.test_location, canonical_root, worktree.resolve()),
        test_command=_remap_text(sample.test_command, canonical_root, worktree.resolve()),
    )


def _copy_tree_item(source: Path, target: Path) -> None:
    if source.is_dir():
        if target.exists() or target.is_symlink():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            destination = target / relative
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif item.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                link_target = os.readlink(item)
                try:
                    destination.symlink_to(link_target, target_is_directory=item.resolve().is_dir())
                except OSError:
                    resolved = item.resolve()
                    if resolved.is_file():
                        shutil.copyfile(resolved, destination)
            elif item.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, destination)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _bootstrap_opencode(project_root: Path, sample_dir: Path) -> None:
    """Install the runner's .opencode config into the project root.

    Idempotent: if the target already has our plugin (smell.ts), skip the
    entire bootstrap. This prevents repeated calls (e.g. from retry attempts)
    from trying to move/overwrite an already-bootstrapped directory, which
    fails on read-only filesystems or permission-restricted volumes.
    """
    source = ROOT / ".opencode"
    target = project_root / ".opencode"
    if (target / "plugins" / "smell.ts").exists():
        # Already bootstrapped by a previous call — skip.
        return
    if target.exists() or target.is_symlink():
        backup = sample_dir / "original-opencode"
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(target), str(backup))

    target.mkdir(parents=True, exist_ok=True)
    for name in ("agents", "commands", "plugins", "skills", "package.json", "package-lock.json", ".gitignore"):
        item = source / name
        if item.exists():
            _copy_tree_item(item, target / name)

    node_modules = source / "node_modules"
    if node_modules.exists():
        try:
            (target / "node_modules").symlink_to(node_modules, target_is_directory=True)
        except OSError:
            shutil.copytree(node_modules, target / "node_modules", symlinks=True)


def _provider_id_from_model(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def _model_id(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def _auth_json_paths(args: argparse.Namespace) -> list[Path]:
    raw = str(args.opencode_auth_json or "auto").strip()
    if raw in {"", "none", "disabled"}:
        return []
    if raw != "auto":
        return [Path(raw).expanduser()]
    paths: list[Path] = []
    if os.environ.get("OPENCODE_AUTH_JSON"):
        paths.append(Path(os.environ["OPENCODE_AUTH_JSON"]).expanduser())
    if os.environ.get("XDG_DATA_HOME"):
        paths.append(Path(os.environ["XDG_DATA_HOME"]).expanduser() / "opencode" / "auth.json")
    home = Path(os.environ.get("HOME", "~")).expanduser()
    paths.extend([home / ".local" / "share" / "opencode" / "auth.json", home / ".config" / "opencode" / "auth.json"])
    return list(dict.fromkeys(paths))


def _api_key_from_args(args: argparse.Namespace, provider_id: str) -> tuple[str, str, str]:
    if args.opencode_api_key:
        return args.opencode_api_key, OPENCODE_BATCH_API_KEY_ENV, "argument"
    if args.opencode_api_key_env and os.environ.get(args.opencode_api_key_env):
        return os.environ[args.opencode_api_key_env], args.opencode_api_key_env, "env"
    if provider_id:
        for path in _auth_json_paths(args):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            auth = data.get(provider_id) if isinstance(data, dict) else None
            if isinstance(auth, dict) and str(auth.get("key") or "").strip():
                return str(auth["key"]).strip(), OPENCODE_BATCH_API_KEY_ENV, f"auth_json:{path}"
    return "", "", ""


def _validate_model_auth(args: argparse.Namespace) -> None:
    """Fail before creating run artifacts when a model credential is absent."""
    if bool(args.dry_run) or bool(args.checkout_only):
        return
    provider_id = _provider_id_from_model(str(args.model or ""))
    if not provider_id:
        raise ValueError(
            "MODEL_PROVIDER_MISSING: --model must use provider/model-id format"
        )
    api_key, _, _ = _api_key_from_args(args, provider_id)
    if api_key:
        return
    requested_env = str(args.opencode_api_key_env or "").strip()
    env_hint = (
        f" Set and export {requested_env}."
        if requested_env
        else " Set --opencode-api-key-env or provide OPENCODE_AUTH_JSON."
    )
    raise ValueError(
        f"MODEL_AUTH_MISSING: no API key is configured for provider {provider_id!r}."
        f"{env_hint}"
    )


def _write_opencode_config(sample_dir: Path, args: argparse.Namespace) -> tuple[Path | None, dict[str, str], dict[str, Any]]:
    provider_id = _provider_id_from_model(args.model)
    api_key, api_key_env, api_key_source = _api_key_from_args(args, provider_id)
    base_url = args.opencode_base_url or (ZAI_DEFAULT_BASE_URL if provider_id == "zai" else "")
    metadata = {
        "provider": provider_id,
        "api_key_configured": bool(api_key),
        "api_key_env": api_key_env,
        "api_key_source": api_key_source,
        "base_url_configured": bool(base_url),
    }
    if not provider_id or (not api_key and not base_url):
        return None, {}, metadata

    model_id = _model_id(args.model)
    model_config = ZAI_PROVIDER_MODELS.get(model_id, {"name": model_id, "tool_call": True, "status": "active"})
    provider_config: dict[str, Any] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": provider_id.upper(),
        "options": {},
        "models": {model_id: model_config},
    }
    if api_key:
        provider_config["options"]["apiKey"] = f"{{env:{api_key_env}}}"
    if base_url:
        provider_config["options"]["baseURL"] = base_url

    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": args.model,
        "enabled_providers": [provider_id],
        "provider": {provider_id: provider_config},
        # Batch runs are headless: a subagent (e.g. the built-in explore agent)
        # reading outside the session directory would otherwise hit an
        # external_directory "ask" that can never be answered, hanging the
        # session until the sample deadline. The primary agents already grant
        # this in their frontmatter; extend the same grant to every agent in
        # this isolated, per-sample runtime config.
        "permission": {"external_directory": "allow"},
    }
    path = sample_dir / "opencode.runtime.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    runtime_env = {api_key_env: api_key} if api_key and api_key_env else {}
    metadata["config_path"] = str(path)
    return path, runtime_env, metadata


def _prepare_opencode_home(sample_dir: Path) -> dict[str, str]:
    # Use a fixed isolated home under sample_dir so the command session and its
    # native agent loop share one opencode.db without leaking cross-sample state.
    home = sample_dir / "opencode-home"
    home.mkdir(parents=True, exist_ok=True)
    config = home / ".config" / "opencode"
    config.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json", ".gitignore"):
        source = ROOT / ".opencode" / name
        if source.is_file():
            shutil.copyfile(source, config / name)
    node_modules = ROOT / ".opencode" / "node_modules"
    if node_modules.exists():
        target = config / "node_modules"
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        try:
            target.symlink_to(node_modules, target_is_directory=True)
        except OSError:
            shutil.copytree(node_modules, target, symlinks=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "NPM_CONFIG_OFFLINE": "true",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
    }


def _failure_category_from_verify_payload(payload: dict[str, Any]) -> str:
    """Read failure_pack.failure_category from a verify payload.

    The single source of truth is smell_bridge.py::_classify_failure_pack; the
    category string is carried in verify.json's failure_pack.failure_category.
    Returns "" when the payload has no classifiable failure_pack.
    """
    if not isinstance(payload, dict):
        return ""
    pack = payload.get("failure_pack")
    if not isinstance(pack, dict):
        return ""
    return str(pack.get("failure_category") or "").strip()


def _compute_status(opencode_returncode: int, verify_returncode: int, verify_payload: dict[str, Any]) -> str:
    """Return the one authoritative status produced by the final bridge verify.

    The model process return code remains execution metadata. In particular, a
    model timeout must not create a second PASS status when the final verifier
    can independently accept the worktree.
    """
    verify_status = str(verify_payload.get("status") or "") if isinstance(verify_payload, dict) else ""
    if verify_status == "PASS":
        if (
            verify_returncode == 0
            and verify_payload.get("resolution") == "resolved"
            and verify_payload.get("success") is True
            and verify_payload.get("accepted") is True
        ):
            return "PASS"
        return "VERIFY_FAILED"
    return verify_status or "VERIFY_FAILED"


OPENCODE_SHUTDOWN_GRACE_SECONDS = 60
OPENCODE_FATAL_PROVIDER_RETURN_CODE = 86


def _fatal_provider_error(log_text: str) -> str:
    """Classify non-retryable provider quota failures found in OpenCode logs."""
    text = str(log_text or "")
    lowered = text.casefold()
    if "token plan 用量上限" in lowered or "已达到 token plan" in lowered:
        return "MINIMAX_TOKEN_PLAN_EXHAUSTED"
    if (
        "insufficient_quota" in lowered
        or "billing_hard_limit_reached" in lowered
        or "credit balance is too low" in lowered
    ):
        return "PROVIDER_INSUFFICIENT_QUOTA"
    return ""


def _opencode_timeout_seconds(sample_deadline: int) -> int:
    """Derive the runner hard stop from the one public sample budget."""
    return sample_deadline + OPENCODE_SHUTDOWN_GRACE_SECONDS


def _is_accepted_status(status: object) -> bool:
    return status == "PASS"


def _attempt_artifact_path(sample_dir: Path, name: str, attempt_suffix: str) -> Path:
    """Return the per-attempt artifact path. attempt_suffix is '' for attempt 0."""
    return sample_dir / f"{name}{attempt_suffix}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _controller_context_manifest(
    command_loop_state: dict[str, Any] | None,
    refactoring_backend: str,
) -> dict[str, Any]:
    state = command_loop_state if isinstance(command_loop_state, dict) else {}
    policy = state.get("policy") if isinstance(state.get("policy"), dict) else {}
    loop = policy.get("loop") if isinstance(policy.get("loop"), dict) else {}
    identity = policy.get("identity") if isinstance(policy.get("identity"), dict) else {}
    return {
        "schema_version": 1,
        "source": "controller_command_state",
        "identity": {
            key: identity.get(key)
            for key in ("project_root", "language", "smell", "location")
        },
        "policy": {
            "verification_mode": policy.get("verification_mode"),
            "allow_test_changes": policy.get("allow_test_changes"),
            "refactoring_backend": refactoring_backend,
            "checkpoint_required": policy.get("checkpoint_required"),
            "loop_mode": loop.get("mode"),
            "max_continuations": loop.get("max_continuations"),
            "no_progress_limit": loop.get("no_progress_limit"),
            "allowed_failure_groups": loop.get("allowed_failure_groups"),
            "sample_deadline_seconds": loop.get("sample_deadline_seconds"),
        },
        "excluded_mutable_fields": [
            "continuation_count",
            "failure_category",
            "failure_pack",
            "loop_instruction",
            "next_action",
        ],
    }


def _append_synthetic_message_event(sample_dir: Path, event: dict[str, Any]) -> None:
    path = sample_dir / "synthetic-events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def _task_prompt(
    sample: Sample,
    args: argparse.Namespace,
    verification_mode: str,
) -> str:
    target_count = len(split_location_descriptors(sample.location))
    lines = [
        f"Project root: {sample.project_root}",
        f"Language: {sample.language}",
        f"Smell type: {sample.smell}",
        f"Target location: {sample.location}",
    ]
    lines.extend(
        [
            f"Refactoring backend: {getattr(args, 'refactoring_backend', 'direct')}",
            f"Verification mode: {verification_mode}",
            (
                "Test changes: explicitly allowed; all changed test files are SHA-audited and the frozen build/test contract remains mandatory."
                if getattr(args, "allow_test_changes", False)
                else "Test changes: forbidden for this dataset run."
            ),
        ]
    )
    lines.append("")
    if target_count > 1:
        lines.append(
            f"Repair this grouped {sample.language} smell across all {target_count} listed "
            "target methods in one cohesive refactoring. Partial target removal is not accepted. "
            "Preserve behavior. Call smell_verify as the final acceptance gate."
        )
    else:
        lines.append(
            f"Repair this one {sample.language} smell from the dataset row. "
            "Preserve behavior. Call smell_verify as the final acceptance gate."
        )
    return "\n".join(lines)


def _command_arguments(task: str, args: argparse.Namespace, verification_mode: str) -> str:
    options = [
        f"--verification-mode={verification_mode}",
        f"--loop-mode={args.loop_mode}",
        f"--loop-max={args.loop_max}",
        f"--loop-no-progress-limit={args.loop_no_progress_limit}",
        f"--loop-on={args.loop_on}",
        f"--sample-deadline={args.sample_deadline}",
    ]
    if getattr(args, "allow_test_changes", False):
        options.append("--allow-test-changes")
    # The shared command parser intentionally consumes the free-form
    # instruction as the final option, so no controller flag may follow it.
    options.append(f"--loop-instruction={args.loop_instruction}")
    return " ".join(options) + " -- " + task


def _initial_command_loop_state(
    sample: Sample,
    args: argparse.Namespace,
    verification_mode: str,
    *,
    started_at_ms: int | None = None,
) -> dict[str, Any]:
    """Freeze trusted v3 state before the first OpenCode process starts.

    A verify-required reminder runs in a new OpenCode process.  The first
    model turn may have made no ``smell_verify`` call, so there may be no tool
    metadata from which to recover state.  Resolve it here through the same
    Python policy/identity authority used by the command hook.
    """

    task = _task_prompt(sample, args, verification_mode)
    payload = resolve_command_payload(
        _command_arguments(task, args, verification_mode),
        defaults={
            "project_root": str(sample.project_root),
            "project_override_root": (
                str(sample.canonical_project_root)
                if sample.canonical_project_root
                else None
            ),
            "language": sample.language,
            "smell": sample.smell,
            "location": sample.location,
            "target_context_json": (
                json.dumps(
                    sample.target_context,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if sample.target_context
                else None
            ),
            "sample_test_location": sample.test_location,
            "sample_test_command": sample.test_command,
        },
        started_at_ms=started_at_ms,
    )
    state = payload.get("command_loop_state")
    if not isinstance(state, dict):
        raise ValueError("INVALID_COMMAND_LOOP_STATE: resolver returned no initial state")
    return state


def _copy_verify_artifacts(sample_dir: Path, verify_payload: dict[str, Any], attempt_suffix: str = "") -> None:
    artifacts = verify_payload.get("artifacts") if isinstance(verify_payload, dict) else None
    if not isinstance(artifacts, dict):
        return
    for key, filename in (("diff", "diff.patch"), ("diff_stat", "diff.stat")):
        source = artifacts.get(key)
        if source and Path(str(source)).is_file():
            dst = _attempt_artifact_path(sample_dir, filename, attempt_suffix)
            shutil.copyfile(str(source), str(dst))


def _persist_verify_payload(
    sample_dir: Path,
    verify_payload: dict[str, Any],
    attempt_suffix: str = "",
) -> None:
    verify_path = _attempt_artifact_path(sample_dir, "verify.json", attempt_suffix)
    verify_path.write_text(
        json.dumps(verify_payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    _copy_verify_artifacts(sample_dir, verify_payload, attempt_suffix)


def _baseline_failure_status(returncode: int, payload: dict[str, Any]) -> str:
    """Return the precise setup status for a failed explicit c000 capture."""
    if (
        returncode == 0
        and payload.get("success") is True
        and payload.get("status") == "BASELINE_CAPTURED"
    ):
        return ""
    detail = str(payload.get("error") or payload.get("status") or "").strip()
    known = (
        "BASELINE_FINDING_NOT_FOUND",
        "TARGET_AMBIGUOUS",
        "DETECTOR_PROFILE_MISMATCH",
        "CHECKPOINT_RECAPTURE_REQUIRED",
        "CHECKPOINT_POLICY_MISMATCH",
        "CHECKPOINT_BASELINE_IDENTITY_MISMATCH",
        "CHECKPOINT_BASELINE_CAPTURE_FAILED",
        "CHECKPOINT_NOT_SUPPORTED",
    )
    for status in known:
        if status in detail:
            return status
    return "BASELINE_CAPTURE_FAILED"


def _run_capture_baseline(
    sample: Sample,
    sample_dir: Path,
    args: argparse.Namespace,
    verification_mode: str,
) -> tuple[int, dict[str, Any]]:
    """Freeze the Java product finding before the model can edit the checkout."""
    cmd = [
        sys.executable,
        str(ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"),
        "capture-baseline",
        "--output-detail",
        "decision",
        "--project-root",
        str(sample.project_root),
        "--smell",
        sample.smell,
        "--location",
        sample.location,
        "--language",
        sample.language,
        "--verification-mode",
        verification_mode,
    ]
    canonical = sample.canonical_project_root
    if canonical and canonical != sample.project_root:
        cmd.extend(["--project-override-root", str(canonical)])
    if args.projects:
        cmd.extend(["--projects", args.projects])
    if sample.target_context:
        cmd.extend([
            "--target-context-json",
            json.dumps(sample.target_context, separators=(",", ":"), sort_keys=True),
        ])
    if sample.test_location:
        cmd.extend(["--sample-test-location", sample.test_location])
    if sample.test_command:
        cmd.extend(["--sample-test-command", sample.test_command])
    if getattr(args, "allow_test_changes", False):
        cmd.append("--allow-test-changes")

    env = os.environ.copy()
    env["SMELL_ALLOW_TEST_CHANGES"] = "1" if getattr(args, "allow_test_changes", False) else "0"
    proc = _run(cmd, ROOT, env=env, timeout=args.sample_deadline)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {
            "success": False,
            "error": "BASELINE_OUTPUT_PARSE_FAILED",
            "stdout": proc.stdout,
        }
    artifact = {
        "returncode": proc.returncode,
        "command": cmd,
        "payload": payload,
        "stderr": proc.stderr,
    }
    (sample_dir / "baseline-capture.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return proc.returncode, payload


def _run_verify(
    sample: Sample,
    sample_dir: Path,
    args: argparse.Namespace,
    verification_mode: str,
    attempt_suffix: str = "",
    baseline_seal: str = "",
) -> tuple[int, dict[str, Any]]:
    cmd = [
        sys.executable,
        str(ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"),
        "verify",
        "--output-detail",
        "decision",
        "--project-root",
        str(sample.project_root),
        "--smell",
        sample.smell,
        "--location",
        sample.location,
        "--language",
        sample.language,
        "--verification-mode",
        verification_mode,
        "--artifact-root",
        str(sample_dir / f"artifacts{attempt_suffix}"),
    ]
    canonical = sample.canonical_project_root
    if canonical and canonical != sample.project_root:
        cmd.extend(["--project-override-root", str(canonical)])
    if args.projects:
        cmd.extend(["--projects", args.projects])
    if sample.target_context:
        cmd.extend([
            "--target-context-json",
            json.dumps(sample.target_context, separators=(",", ":"), sort_keys=True),
        ])
    if sample.test_location:
        cmd.extend(["--sample-test-location", sample.test_location])
    if sample.test_command:
        cmd.extend(["--sample-test-command", sample.test_command])
    if baseline_seal:
        cmd.extend(["--baseline-seal", baseline_seal])

    env = os.environ.copy()
    env["SMELL_REQUIRE_BUILD_TEST"] = "1"
    env["SMELL_ALLOW_TEST_CHANGES"] = "1" if getattr(args, "allow_test_changes", False) else "0"
    proc = _run(cmd, ROOT, env=env, timeout=args.sample_deadline)
    payload: dict[str, Any]
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"success": False, "status": "VERIFY_OUTPUT_PARSE_FAILED", "stdout": proc.stdout, "stderr": proc.stderr}
    _persist_verify_payload(sample_dir, payload, attempt_suffix)
    return proc.returncode, payload


def _parse_session_id_from_json_events(events_text: str) -> str:
    """Extract the session id from a --format json stdout event stream.

    opencode run --format json emits one JSON object per line on stdout. Each
    event carries the session id at the TOP LEVEL as ``sessionID`` (not nested
    under properties). We scan every line and return the first non-empty
    sessionID found, so this works even if the session.created event is not the
    first one emitted.
    """
    for raw in (events_text or "").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = ev.get("sessionID") or ev.get("session_id") or ""
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
        # Fallback: nested form (SSE-style properties.sessionID or properties.info.id).
        props = ev.get("properties")
        if isinstance(props, dict):
            sid2 = props.get("sessionID") or ""
            if not sid2 and isinstance(props.get("info"), dict):
                sid2 = props["info"].get("id") or ""
            if isinstance(sid2, str) and sid2.strip():
                return sid2.strip()
    return ""


def _verification_trace(events_text: str) -> dict[str, Any]:
    """Summarize completed smell_verify calls from one OpenCode JSON stream."""
    calls = 0
    tools_after_last_verify = 0
    last_payload: dict[str, Any] | None = None
    last_decision = ""
    last_status = ""
    last_cap_recovery_used = False
    last_command_loop_state: dict[str, Any] | None = None
    tool_sequence: list[str] = []
    attempted_tool_sequence: list[str] = []
    direct_idea_cli_calls = 0
    direct_edit_calls = 0
    for raw in (events_text or "").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "tool_use":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        state = part.get("state")
        if not isinstance(state, dict):
            continue
        tool_name = str(part.get("tool") or "")
        if tool_name:
            attempted_tool_sequence.append(tool_name)
        state_input = state.get("input")
        if tool_name == "bash" and isinstance(state_input, dict):
            command = str(state_input.get("command") or "")
            if re.search(r"(?:^|[\s/])idea-refactor(?:\s|$)", command):
                direct_idea_cli_calls += 1
        if tool_name in {"edit", "write", "patch", "apply_patch"}:
            direct_edit_calls += 1
        if state.get("status") != "completed":
            continue
        if tool_name:
            tool_sequence.append(tool_name)
        if part.get("tool") != "smell_verify":
            if calls:
                tools_after_last_verify += 1
            continue
        calls += 1
        tools_after_last_verify = 0
        # A malformed newer verify must not leave an older payload reusable.
        last_payload = None
        metadata = state.get("metadata")
        if isinstance(metadata, dict):
            meta_loop = metadata.get("loop")
            if isinstance(meta_loop, dict):
                last_decision = str(meta_loop.get("decision") or "")
                last_cap_recovery_used = meta_loop.get("cap_recovery_used") is True
            command_loop_state = metadata.get("command_loop_state")
            if isinstance(command_loop_state, dict):
                last_command_loop_state = command_loop_state
            auto = metadata.get("auto_continuation")
            if isinstance(auto, dict):
                last_status = str(auto.get("status") or "")
        output = state.get("output")
        if not isinstance(output, str):
            continue
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            last_payload = payload
    loop = last_payload.get("loop") if isinstance(last_payload, dict) else None
    if not last_decision and isinstance(loop, dict):
        last_decision = str(loop.get("decision") or "")
        last_cap_recovery_used = loop.get("cap_recovery_used") is True
    if not last_status and isinstance(last_payload, dict):
        last_status = str(last_payload.get("status") or "")
    return {
        "smell_verify_calls": calls,
        "tools_after_last_verify": tools_after_last_verify,
        "last_loop_decision": last_decision,
        "last_status": last_status,
        "last_output_parsed": last_payload is not None,
        "last_payload": last_payload,
        "last_cap_recovery_used": last_cap_recovery_used,
        "command_loop_state": last_command_loop_state,
        "tool_sequence": tool_sequence,
        "attempted_tool_sequence": attempted_tool_sequence,
        "idea_refactor_preview_calls": tool_sequence.count("idea_refactor_preview"),
        "idea_refactor_apply_calls": tool_sequence.count("idea_refactor_apply"),
        "idea_edit_calls": tool_sequence.count("idea_edit"),
        "direct_idea_cli_calls": direct_idea_cli_calls,
        "direct_edit_calls": direct_edit_calls,
    }


def _idea_protocol_contract(controller_attempts: list[dict[str, Any]]) -> dict[str, Any]:
    sequence = [
        str(tool)
        for attempt in controller_attempts
        for tool in attempt.get("tool_sequence", [])
        if str(tool)
    ]
    attempted_sequence = [
        str(tool)
        for attempt in controller_attempts
        for tool in attempt.get("attempted_tool_sequence", [])
        if str(tool)
    ]
    preview_positions = [index for index, tool in enumerate(sequence) if tool == "idea_refactor_preview"]
    apply_positions = [index for index, tool in enumerate(sequence) if tool == "idea_refactor_apply"]
    verify_positions = [index for index, tool in enumerate(sequence) if tool == "smell_verify"]
    ordered = bool(
        preview_positions
        and apply_positions
        and verify_positions
        and min(preview_positions) < min(apply_positions) < max(verify_positions)
    )
    direct_idea_cli_calls = sum(int(attempt.get("direct_idea_cli_calls") or 0) for attempt in controller_attempts)
    direct_edit_calls = sum(int(attempt.get("direct_edit_calls") or 0) for attempt in controller_attempts)
    violations = []
    if not preview_positions:
        violations.append("IDEA_PREVIEW_MISSING")
    if not apply_positions:
        violations.append("IDEA_APPLY_MISSING")
    if not verify_positions:
        violations.append("SMELL_VERIFY_MISSING")
    if preview_positions and apply_positions and verify_positions and not ordered:
        violations.append("IDEA_PROTOCOL_ORDER_INVALID")
    if direct_idea_cli_calls:
        violations.append("DIRECT_IDEA_CLI_USED")
    if direct_edit_calls:
        violations.append("DIRECT_EDIT_USED")
    return {
        "success": not violations,
        "protocol": "idea-proposal-v1",
        "tool_sequence": sequence,
        "attempted_tool_sequence": attempted_sequence,
        "preview_calls": len(preview_positions),
        "apply_calls": len(apply_positions),
        "verify_calls": len(verify_positions),
        "idea_edit_calls": sequence.count("idea_edit"),
        "direct_idea_cli_calls": direct_idea_cli_calls,
        "direct_edit_calls": direct_edit_calls,
        "violations": violations,
    }


def _runner_closure_action(
    trace: dict[str, Any],
    *,
    reminder_used: bool,
    continuations_dispatched: int,
    max_continuations: int,
) -> str:
    """Return the next synchronous runner action for a completed OpenCode turn."""
    if int(trace.get("smell_verify_calls") or 0) == 0:
        return "stop" if reminder_used else "verify_required"
    # The plugin owns all semantic continuation policy across UI and batch.
    # This is only a transport safety bound. The extra transport is available
    # only when the plugin explicitly persisted that its one cap recovery was
    # consumed; a bare `continue` must never manufacture an extra retry.
    transport_limit = max_continuations + (
        1 if trace.get("last_cap_recovery_used") is True else 0
    )
    if (
        trace.get("last_loop_decision") == "continue"
        and continuations_dispatched < transport_limit
    ):
        return "continue"
    return "stop"


def _runner_continuation_prompt(
    action: str,
    continuation: int,
    max_continuations: int,
    instruction: str,
    *,
    allow_test_changes: bool = False,
    failure_category: str = "",
) -> str:
    # Policy and failure details already live in the stable controller context
    # and latest smell_verify result. This message only resumes transport.
    _ = instruction, allow_test_changes, failure_category
    if action == "verify_required":
        return "\n".join(
            [
                "[runner-resume verify-required]",
                "Resume the existing task in this session and call smell_verify now.",
                "The controller policy is unchanged; use the current source state.",
            ]
        )
    return "\n".join(
        [
            f"[runner-resume continue {continuation}/{max_continuations}]",
            "Resume the existing task in this session.",
            "Read the latest smell_verify tool result and follow its loop.instruction.",
            "After one narrow corrective edit, call smell_verify again.",
        ]
    )


def _opencode_run_command(args: argparse.Namespace, agent: str, session_id: str = "") -> list[str]:
    cmd = [args.opencode_bin, "run"]
    if session_id:
        cmd.extend(["--session", session_id])
    else:
        command = {
            "java-refactor-agent": "java-refactor-run",
            "smell-refactor-agent": "smell-refactor-run",
        }[agent]
        cmd.extend(["--command", command])
    cmd.extend([
        "--agent", agent,
        "--model", args.model,
        "--dangerously-skip-permissions",
        "--format", "json",
        "--print-logs",
    ])
    return cmd


def _select_agent(sample: Sample, args: argparse.Namespace) -> str:
    if args.agent:
        return args.agent
    if sample.language == "java":
        return "java-refactor-agent"
    return "smell-refactor-agent"


def _run_opencode(
    sample: Sample,
    sample_dir: Path,
    args: argparse.Namespace,
    agent: str,
    verification_mode: str,
    *,
    session_id: str = "",
    continuation_prompt: str = "",
    command_loop_state: dict[str, Any] | None = None,
    attempt_suffix: str = "",
    hard_timeout_seconds: int | None = None,
    baseline_seal: str = "",
) -> tuple[int, str]:
    """Run one initial or same-session OpenCode turn."""
    config_path, runtime_env, auth_meta = _write_opencode_config(sample_dir, args)
    task = _task_prompt(sample, args, verification_mode)
    command_arguments = _command_arguments(task, args, verification_mode)
    stdin_payload = continuation_prompt if session_id else command_arguments
    task_path = _attempt_artifact_path(sample_dir, "task.txt", attempt_suffix)
    task_path.write_text(stdin_payload + "\n", encoding="utf-8")
    raw_input_path = _attempt_artifact_path(sample_dir, "raw-user-input.txt", attempt_suffix)
    raw_input_path.write_text(stdin_payload, encoding="utf-8")
    controller_source_path = _attempt_artifact_path(
        sample_dir, "controller-context.json", attempt_suffix
    )
    controller_source_path.write_text(
        json.dumps(
            _controller_context_manifest(
                command_loop_state,
                getattr(args, "refactoring_backend", "direct"),
            ),
            indent=2,
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    controller_system_path = _attempt_artifact_path(
        sample_dir, "controller-system.txt", attempt_suffix
    )
    message_manifest_path = _attempt_artifact_path(
        sample_dir, "message-manifest.json", attempt_suffix
    )

    env = os.environ.copy()
    env.update(_prepare_opencode_home(sample_dir))
    env.update(runtime_env)
    if config_path:
        env["OPENCODE_CONFIG"] = str(config_path)
    # The custom command owns the native in-session loop. Disable the legacy
    # session.idle mechanism so there is exactly one controller.
    env["SMELL_BATCH_RUN"] = "1"
    if session_id and command_loop_state:
        env["SMELL_COMMAND_LOOP_STATE_JSON"] = json.dumps(
            command_loop_state, separators=(",", ":"), sort_keys=True
        )
    else:
        env.pop("SMELL_COMMAND_LOOP_STATE_JSON", None)
    env["SMELL_BRIDGE_FILE"] = str(ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py")
    env["SMELL_PROJECT_ROOT"] = str(sample.project_root)
    if sample.canonical_project_root:
        env["SMELL_CANONICAL_PROJECT_ROOT"] = str(sample.canonical_project_root)
    env["SMELL_LANGUAGE"] = sample.language
    env["SMELL_SMELL"] = sample.smell
    env["SMELL_LOCATION"] = sample.location
    env.pop("SMELL_EVIDENCE", None)
    if sample.target_context:
        env["SMELL_TARGET_CONTEXT_JSON"] = json.dumps(
            sample.target_context, separators=(",", ":"), sort_keys=True
        )
    else:
        env.pop("SMELL_TARGET_CONTEXT_JSON", None)
    env["SMELL_VERIFICATION_MODE"] = verification_mode
    env["SMELL_SAMPLE_TEST_LOCATION"] = sample.test_location
    env["SMELL_SAMPLE_TEST_COMMAND"] = sample.test_command
    env["SMELL_ALLOW_TEST_CHANGES"] = "1" if getattr(args, "allow_test_changes", False) else "0"
    refactoring_backend = getattr(args, "refactoring_backend", "direct")
    env["SMELL_REFACTORING_BACKEND"] = refactoring_backend
    baseline_context_path = sample_dir / "baseline-capture.json"
    if baseline_context_path.is_file():
        env["SMELL_BASELINE_CONTEXT_FILE"] = str(baseline_context_path)
    else:
        env.pop("SMELL_BASELINE_CONTEXT_FILE", None)
    env["SMELL_CONTROLLER_CONTEXT_AUDIT_FILE"] = str(controller_system_path)
    if refactoring_backend == "idea":
        env["SMELL_ENABLE_IDEA_TOOLS"] = "1"
        env["SMELL_IDEA_PREPARED"] = "1"
        env["SMELL_IDEA_PROJECT_ROOT"] = str(sample.project_root)
    else:
        env.pop("SMELL_ENABLE_IDEA_TOOLS", None)
        env.pop("SMELL_IDEA_PREPARED", None)
        env.pop("SMELL_IDEA_PROJECT_ROOT", None)
    if baseline_seal:
        env["SMELL_BASELINE_SEAL"] = baseline_seal
    else:
        env.pop("SMELL_BASELINE_SEAL", None)
    # Agent-triggered verifies are loop feedback. The runner performs a fresh
    # final bridge verify after the model process exits.
    agent_artifact_root = sample_dir / "agent-artifacts"
    agent_artifact_root.mkdir(parents=True, exist_ok=True)
    env["SMELL_ARTIFACT_ROOT"] = str(agent_artifact_root)
    if args.projects:
        env["SMELL_PROJECTS"] = args.projects
    env["SMELL_REQUIRE_BUILD_TEST"] = "1"

    # --format json: raw JSON events on stdout (for session-id parsing).
    # --print-logs: human-readable logs on stderr (written to run.log).
    cmd = _opencode_run_command(args, agent, session_id)
    command_payload = {
        "cmd": cmd,
        "command_arguments_transport": "stdin",
        "command_arguments_length": len(stdin_payload),
        "session_id_requested": session_id,
        "is_continuation": bool(session_id),
        "cwd": str(sample.project_root),
        "agent": agent,
        "auth": {**auth_meta, "api_key_source": "configured" if auth_meta.get("api_key_configured") else ""},
        "verification_mode": verification_mode,
        "loop_policy": parse_command_policy(command_arguments).loop.to_dict(),
        "time_budget": {
            "source": "sample-deadline",
            "sample_deadline_seconds": args.sample_deadline,
            "opencode_shutdown_grace_seconds": OPENCODE_SHUTDOWN_GRACE_SECONDS,
            "opencode_hard_timeout_seconds": hard_timeout_seconds or _opencode_timeout_seconds(args.sample_deadline),
            "final_verify_timeout_seconds": args.sample_deadline,
            "final_verify_mode": "runner_final",
            "idle_watchdog_enabled": False,
        },
    }
    command_path = _attempt_artifact_path(sample_dir, "command.json", attempt_suffix)
    command_path.write_text(json.dumps(command_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    log_path = _attempt_artifact_path(sample_dir, "run.log", attempt_suffix)
    events_path = _attempt_artifact_path(sample_dir, "run.events.jsonl", attempt_suffix)
    with log_path.open("w", encoding="utf-8") as log, events_path.open("w", encoding="utf-8") as events_file:
        proc = subprocess.Popen(
            cmd,
            cwd=str(sample.project_root),
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            start_new_session=True,
        )
        # OpenCode already supports reading the message from stdin. Keep the
        # command arguments as one exact string so yargs cannot coerce numeric
        # evidence tokens into numbers before run.ts formats the message.
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin_payload)
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()
        # Read stdout (JSON events) incrementally, write to events file, and
        # detect session id.
        detected_sid = ""

        def _drain_stdout():
            nonlocal detected_sid
            assert proc.stdout is not None
            for line in proc.stdout:
                events_file.write(line)
                events_file.flush()
                if not detected_sid:
                    sid = _parse_session_id_from_json_events(line)
                    if sid:
                        detected_sid = sid

        reader = threading.Thread(target=_drain_stdout, daemon=True)
        reader.start()
        deadline = time.monotonic() + (
            hard_timeout_seconds or _opencode_timeout_seconds(args.sample_deadline)
        )
        timeout_code = 0
        log_scan_offset = 0
        log_scan_tail = ""
        while proc.poll() is None:
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as log_reader:
                    log_reader.seek(log_scan_offset)
                    log_chunk = log_reader.read()
                    log_scan_offset = log_reader.tell()
            except OSError:
                log_chunk = ""
            provider_failure = _fatal_provider_error(log_scan_tail + log_chunk)
            log_scan_tail = (log_scan_tail + log_chunk)[-256:]
            if provider_failure:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
                timeout_code = OPENCODE_FATAL_PROVIDER_RETURN_CODE
                provider_failure_path = _attempt_artifact_path(
                    sample_dir, "provider.failure.json", attempt_suffix
                )
                provider_failure_path.write_text(
                    json.dumps(
                        {
                            "failure_category": "PROVIDER_QUOTA_FAILED",
                            "provider_failure": provider_failure,
                            "retryable": False,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                break
            if time.monotonic() > deadline:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
                timeout_code = 124
                break
            time.sleep(1)
        reader.join(timeout=5)
        rc = timeout_code if timeout_code else int(proc.returncode or 0)
        if not detected_sid:
            # Fallback: re-parse the full events file (thread may have set it
            # after the poll loop checked).
            try:
                detected_sid = _parse_session_id_from_json_events(events_path.read_text(encoding="utf-8"))
            except OSError:
                pass
        # If still no sid and the reader thread is alive (proc was killed before
        # EOF), drain it fully then re-parse to avoid losing the session id.
        if not detected_sid and reader.is_alive():
            reader.join(timeout=10)
            try:
                detected_sid = _parse_session_id_from_json_events(events_path.read_text(encoding="utf-8"))
            except OSError:
                pass
        message_manifest = {
            "schema_version": 1,
            "provenance": "controller_resume" if session_id else "user_command",
            "session_id_requested": session_id,
            "session_id_observed": detected_sid or session_id,
            "user_parts_mutated_by_plugin": False,
            "raw_user_input": {
                "path": str(raw_input_path),
                "bytes": raw_input_path.stat().st_size,
                "sha256": _sha256_file(raw_input_path),
            },
            "controller_context_source": {
                "path": str(controller_source_path),
                "sha256": _sha256_file(controller_source_path),
            },
            "controller_system_context": {
                "path": str(controller_system_path),
                "captured": controller_system_path.is_file(),
                "sha256": (
                    _sha256_file(controller_system_path)
                    if controller_system_path.is_file()
                    else ""
                ),
            },
        }
        message_manifest_path.write_text(
            json.dumps(message_manifest, indent=2, ensure_ascii=True, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return rc, detected_sid or session_id


def _append_result(results_path: Path, row: dict[str, Any]) -> None:
    fieldnames = [
        "sample_id",
        "smell",
        "project_name",
        "project_root",
        "execution_project_root",
        "location",
        "verification_mode",
        "agent",
        "status",
        "resolution",
        "accepted",
        "progress",
        "termination_reason",
        "opencode_returncode",
        "opencode_timed_out",
        "opencode_failure_category",
        "verify_returncode",
        "duration_seconds",
        "sample_dir",
        "note",
    ]
    exists = results_path.is_file()
    with results_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fieldnames})


def _checkout_only_sample(sample: Sample, run_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Diagnostic mode: create a REAL isolated checkout pinned to project_commit, verify it
    against the manifest, and record the revision audit — but do NOT invoke opencode/verify.

    Used to prove the real refactor entry pins project_commit (no HEAD fallback) without
    calling the model or performing a refactor.
    """
    started = time.time()
    sample_dir = run_dir / "samples" / f"sample-{_sanitize(sample.sample_id)}-{_sanitize(sample.project_name)}"
    sample_dir.mkdir(parents=True, exist_ok=True)
    canonical_root = sample.project_root.resolve()
    row: dict[str, Any] = {
        "sample_id": sample.sample_id,
        "smell": sample.smell,
        "project_name": sample.project_name,
        "project_root": str(sample.project_root),
    }
    try:
        revisions = load_revisions(args.project_revisions)
        rev = resolve_revision(sample.project_name, revisions, args.project_revisions)
        assert_commit_present(canonical_root, rev.project_commit)
        prepared = _prepare_worktree(
            sample,
            _execution_checkout_run_dir(run_dir),
            target_commit=rev.project_commit,
            revisions=revisions,
        )
        audit = verify_checkout(prepared.project_root, rev)
        audit.update(
            audit_test_commit(
                canonical_root,
                sample.raw.get("test_commit", ""),
                rev.project_commit,
            )
        )
        if prepared.sibling_revision_audit:
            audit["sibling_checkouts"] = list(prepared.sibling_revision_audit)
        audit.update(
            verify_test_oracle(
                prepared.project_root,
                prepared.test_location,
                sample.raw.get("test_oracle_sha256", ""),
            )
        )
        row.update({
            "status": "CHECKOUT_OK",
            "execution_project_root": str(prepared.project_root),
            **audit,
        })
        _remove_worktree_checkout(canonical_root, prepared.project_root)
        for sibling in prepared.sibling_revision_audit:
            sibling_checkout = str(sibling.get("execution_project_root") or "").strip()
            if sibling_checkout:
                shutil.rmtree(sibling_checkout, ignore_errors=True)
    except ProjectRevisionError as exc:
        row.update({
            "status": exc.status,
            "execution_project_root": "",
            "requested_project_commit": exc.extra.get("project_commit", "") or (rev.project_commit if "rev" in dir() else ""),
            "actual_commit": "",
            "expected_tree_hash": rev.expected_tree_hash if "rev" in dir() else "",
            "actual_tree_hash": "",
            "project_revision_alignment": exc.status,
            "project_revisions_path": args.project_revisions,
            "error_message": exc.message,
        })
    except Exception as exc:  # noqa: BLE001
        row.update({"status": "CHECKOUT_ERROR", "execution_project_root": "",
                    "project_revision_alignment": "CHECKOUT_ERROR", "error_message": str(exc)})
    row["duration_seconds"] = f"{time.time() - started:.1f}"
    (sample_dir / "checkout_audit.json").write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    return row


def _run_sample(sample: Sample, run_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    dataset_audit = {
        "evidence": sample.evidence,
        "target_context": sample.target_context,
    }
    sample_dir = run_dir / "samples" / f"sample-{_sanitize(sample.sample_id)}-{_sanitize(sample.project_name)}"
    sample_dir.mkdir(parents=True, exist_ok=True)
    # Clean any stale opencode-home from a previous run of the same sample to
    # avoid opencode.db session residue / corruption. The home is recreated by
    # _prepare_opencode_home on the first _run_opencode call.
    stale_home = sample_dir / "opencode-home"
    if stale_home.exists():
        shutil.rmtree(stale_home, ignore_errors=True)
    agent = _select_agent(sample, args)
    verification_mode = _effective_verification_mode(sample, args)

    # One isolated checkout is used for the complete command-owned native loop.
    # The checkout is ALWAYS pinned to the authoritative project_commit from
    # project-revisions.json (resolved via smell_core.project_revision). There is no
    # HEAD fallback: if the manifest entry / commit / tree cannot be honored, the
    # sample fails fast with an explicit PROJECT_* status.
    revision_audit: dict[str, Any] = {}
    revisions_path = getattr(args, "project_revisions", DEFAULT_REVISIONS_PATH)
    try:
        revisions = load_revisions(revisions_path)
        rev = resolve_revision(sample.project_name, revisions, revisions_path)
        assert_commit_present(sample.project_root.resolve(), rev.project_commit)
        execution_sample = (
            _prepare_worktree(
                sample,
                _execution_checkout_run_dir(run_dir),
                target_commit=rev.project_commit,
                revisions=revisions,
            )
            if args.worktree
            else replace(sample, canonical_project_root=sample.project_root)
        )
        if args.worktree:
            revision_audit = verify_checkout(execution_sample.project_root, rev)
            if execution_sample.sibling_revision_audit:
                revision_audit["sibling_checkouts"] = list(
                    execution_sample.sibling_revision_audit
                )
        revision_audit.update(
            audit_test_commit(
                sample.project_root.resolve(),
                sample.raw.get("test_commit", ""),
                rev.project_commit,
            )
        )
        revision_audit.update(
            verify_test_oracle(
                execution_sample.project_root,
                execution_sample.test_location,
                sample.raw.get("test_oracle_sha256", ""),
            )
        )
    except ProjectRevisionError as exc:
        # Fail fast: record the deviation and abort this sample without running
        # opencode/verify or touching runtime HEAD.
        revision_audit = {
            "requested_project_commit": rev.project_commit if "rev" in dir() else "",
            "actual_commit": "",
            "expected_tree_hash": rev.expected_tree_hash if "rev" in dir() else "",
            "actual_tree_hash": "",
            "project_revision_alignment": exc.status,
            "project_revisions_path": revisions_path,
        }
        row = {
            "sample_id": sample.sample_id,
            "smell": sample.smell,
            "project_name": sample.project_name,
            "project_root": str(sample.project_root),
            "execution_project_root": "",
            "location": sample.location,
            "verification_mode": verification_mode,
            "agent": agent,
            "status": exc.status,
            "opencode_returncode": -1,
            "verify_returncode": -1,
            "duration_seconds": f"{time.time() - started:.1f}",
            "sample_dir": str(sample_dir),
            "note": f"project_revision_error: {exc.status}: {exc.message}",
        }
        result_summary = {
            **row,
            "attempts": [],
            "revision_audit": revision_audit,
            "dataset_audit": dataset_audit,
        }
        (sample_dir / "result.json").write_text(
            json.dumps(result_summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
        )
        return row
    (sample_dir / "sample.json").write_text(
        json.dumps({**sample.raw, "execution_project_root": str(execution_sample.project_root)}, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    baseline_capture: dict[str, Any] | None = None
    baseline_seal = ""
    if execution_sample.language == "java":
        baseline_returncode, baseline_capture = _run_capture_baseline(
            execution_sample,
            sample_dir,
            args,
            verification_mode,
        )
        baseline_status = _baseline_failure_status(
            baseline_returncode,
            baseline_capture,
        )
        if baseline_status:
            baseline_error = str(
                baseline_capture.get("error")
                or baseline_capture.get("status")
                or "baseline capture failed"
            )
            row = {
                "sample_id": sample.sample_id,
                "smell": sample.smell,
                "project_name": sample.project_name,
                "project_root": str(sample.project_root),
                "execution_project_root": str(execution_sample.project_root),
                "location": execution_sample.location,
                "verification_mode": verification_mode,
                "agent": agent,
                "status": baseline_status,
                "opencode_returncode": -1,
                "verify_returncode": -1,
                "duration_seconds": f"{time.time() - started:.1f}",
                "sample_dir": str(sample_dir),
                "note": f"baseline_capture_failed: {baseline_error}",
            }
            (sample_dir / "result.json").write_text(
                json.dumps(
                    {
                        **row,
                        "attempts": [],
                        "controller_attempts": [],
                        "revision_audit": revision_audit,
                        "dataset_audit": dataset_audit,
                        "baseline_capture": baseline_capture,
                    },
                    indent=2,
                    ensure_ascii=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return row
        baseline_seal = str(baseline_capture.get("baseline_seal") or "").strip()
        if not baseline_seal:
            row = {
                "sample_id": sample.sample_id,
                "smell": sample.smell,
                "project_name": sample.project_name,
                "project_root": str(sample.project_root),
                "execution_project_root": str(execution_sample.project_root),
                "location": execution_sample.location,
                "verification_mode": verification_mode,
                "agent": agent,
                "status": "BASELINE_SEAL_MISSING",
                "opencode_returncode": -1,
                "verify_returncode": -1,
                "duration_seconds": f"{time.time() - started:.1f}",
                "sample_dir": str(sample_dir),
                "note": "baseline_capture_failed: controller baseline seal missing",
            }
            (sample_dir / "result.json").write_text(
                json.dumps({**row, "attempts": [], "baseline_capture": baseline_capture}, indent=2)
                + "\n",
                encoding="utf-8",
            )
            return row

    if getattr(args, "refactoring_backend", "direct") == "idea":
        idea_preflight = _prepare_idea_service(execution_sample.project_root, sample_dir)
        if idea_preflight.get("status") != "ok" or idea_preflight.get("returncode") != 0:
            row = {
                "sample_id": sample.sample_id,
                "smell": sample.smell,
                "project_name": sample.project_name,
                "project_root": str(sample.project_root),
                "execution_project_root": str(execution_sample.project_root),
                "location": execution_sample.location,
                "verification_mode": verification_mode,
                "refactoring_backend": "idea",
                "agent": agent,
                "status": "IDEA_PRECHECK_FAILED",
                "opencode_returncode": -1,
                "verify_returncode": -1,
                "duration_seconds": f"{time.time() - started:.1f}",
                "sample_dir": str(sample_dir),
                "note": "IDEA service did not become ready for the execution worktree; see idea-preflight.json",
            }
            (sample_dir / "result.json").write_text(
                json.dumps(
                    {
                        **row,
                        "attempts": [],
                        "controller_attempts": [],
                        "revision_audit": revision_audit,
                        "dataset_audit": dataset_audit,
                        "baseline_capture": baseline_capture,
                        "idea_preflight": idea_preflight,
                    },
                    indent=2,
                    ensure_ascii=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return row

    # Bootstrap .opencode once before starting the command-owned loop.
    _bootstrap_opencode(execution_sample.project_root, sample_dir)

    # Batch `opencode run` exits as the session becomes idle, so a fire-and-forget
    # plugin promptAsync cannot reliably create another turn. Keep the policy in
    # one OpenCode session, but synchronously resume that session from the runner
    # when the completed event stream proves verification closure is missing.
    model_deadline = time.monotonic() + _opencode_timeout_seconds(args.sample_deadline)
    controller_attempts: list[dict[str, Any]] = []
    session_id = ""
    continuation_prompt = ""
    command_loop_state: dict[str, Any] | None = _initial_command_loop_state(
        execution_sample,
        args,
        verification_mode,
    )
    continuations_dispatched = 0
    reminders_dispatched = 0
    reminder_used = False
    attempt_index = 0
    opencode_returncode = 0
    while True:
        remaining = int(model_deadline - time.monotonic())
        if remaining <= 0:
            opencode_returncode = 124
            break
        attempt_suffix = "" if attempt_index == 0 else f".continue-{attempt_index}"
        opencode_returncode, detected_session_id = _run_opencode(
            execution_sample,
            sample_dir,
            args,
            agent,
            verification_mode,
            session_id=session_id,
            continuation_prompt=continuation_prompt,
            command_loop_state=command_loop_state,
            attempt_suffix=attempt_suffix,
            hard_timeout_seconds=remaining,
            baseline_seal=baseline_seal,
        )
        if detected_session_id:
            session_id = detected_session_id
        events_path = _attempt_artifact_path(sample_dir, "run.events.jsonl", attempt_suffix)
        try:
            trace = _verification_trace(events_path.read_text(encoding="utf-8"))
        except OSError:
            trace = _verification_trace("")
        trace_summary = {
            key: value
            for key, value in trace.items()
            if key != "last_payload"
        }
        controller_attempts.append(
            {
                "attempt": attempt_index,
                "suffix": attempt_suffix,
                "opencode_returncode": opencode_returncode,
                "session_id": session_id,
                **trace_summary,
            }
        )
        restored_state = trace.get("command_loop_state")
        if isinstance(restored_state, dict):
            command_loop_state = restored_state
        if opencode_returncode != 0 or not session_id:
            break
        action = _runner_closure_action(
            trace,
            reminder_used=reminder_used,
            continuations_dispatched=continuations_dispatched,
            max_continuations=args.loop_max if args.loop_mode == "verify-failure" else 0,
        )
        if action == "stop":
            break
        if action == "verify_required":
            reminders_dispatched += 1
            reminder_used = True
        else:
            continuations_dispatched += 1
            reminder_used = False
        continuation_prompt = _runner_continuation_prompt(
            action,
            continuations_dispatched,
            args.loop_max,
            args.loop_instruction,
            allow_test_changes=bool(getattr(args, "allow_test_changes", False)),
            failure_category=str(trace.get("last_failure_category") or ""),
        )
        _append_synthetic_message_event(
            sample_dir,
            {
                "schema_version": 1,
                "source": "batch_runner",
                "provenance": "controller_resume",
                "action": action,
                "session_id": session_id,
                "from_attempt": attempt_index,
                "to_attempt": attempt_index + 1,
                "continuation": continuations_dispatched,
                "details_source": "latest_smell_verify_tool_result",
            },
        )
        attempt_index += 1

    verify_returncode, verify_payload = _run_verify(
        execution_sample,
        sample_dir,
        args,
        verification_mode,
        baseline_seal=baseline_seal,
    )
    if getattr(args, "refactoring_backend", "direct") == "idea":
        _close_idea_project(execution_sample.project_root, sample_dir)
    final_verify_source = "runner_final"
    opencode_failure_category = (
        "PROVIDER_QUOTA_FAILED"
        if opencode_returncode == OPENCODE_FATAL_PROVIDER_RETURN_CODE
        else (
            "OPENCODE_TIMEOUT"
            if opencode_returncode == 124
            else ("OPENCODE_FAILED" if opencode_returncode else "")
        )
    )
    final_status = _compute_status(opencode_returncode, verify_returncode, verify_payload)
    idea_protocol = (
        _idea_protocol_contract(controller_attempts)
        if getattr(args, "refactoring_backend", "direct") == "idea"
        else None
    )
    if idea_protocol is not None and idea_protocol["success"] is not True:
        final_status = "IDEA_PROTOCOL_FAILED"
    resolution = str(verify_payload.get("resolution") or "")
    accepted = _is_accepted_status(final_status)
    progress = bool(verify_payload.get("progress")) or accepted
    loop_payload = verify_payload.get("loop")
    termination_reason = (
        str(loop_payload.get("termination_reason") or "")
        if isinstance(loop_payload, dict)
        else ""
    )
    last = {
        "attempt": attempt_index,
        "opencode_returncode": opencode_returncode,
        "verify_returncode": verify_returncode,
        "verify_payload": verify_payload,
        "status": final_status,
        "failure_category": _failure_category_from_verify_payload(verify_payload),
        "verify_source": final_verify_source,
        "session_id": session_id,
        "is_continuation": attempt_index > 0,
        "opencode_timed_out": opencode_returncode == 124,
        "opencode_failure_category": opencode_failure_category,
    }
    attempts = [last]
    note = (
        f"loop_policy={args.loop_mode}:{args.loop_max};"
        f"runner_continuations={continuations_dispatched};"
        f"verify_reminders={reminders_dispatched};"
        f"opencode_timed_out={str(opencode_returncode == 124).lower()};"
        f"final_verify_source={final_verify_source}"
    )

    row = {
        "sample_id": sample.sample_id,
        "smell": sample.smell,
        "project_name": sample.project_name,
        "project_root": str(sample.project_root),
        "execution_project_root": str(execution_sample.project_root),
        "location": execution_sample.location,
        "verification_mode": verification_mode,
        "allow_test_changes": bool(getattr(args, "allow_test_changes", False)),
        "refactoring_backend": getattr(args, "refactoring_backend", "direct"),
        "agent": agent,
        "status": final_status,
        "resolution": resolution,
        "accepted": accepted,
        "progress": progress,
        "termination_reason": termination_reason,
        "opencode_returncode": last["opencode_returncode"],
        "opencode_timed_out": opencode_returncode == 124,
        "opencode_failure_category": opencode_failure_category,
        "verify_returncode": last["verify_returncode"],
        "duration_seconds": f"{time.time() - started:.1f}",
        "sample_dir": str(sample_dir),
        "note": note,
    }
    result_summary = {
        **row,
        "attempts": attempts,
        "controller_attempts": controller_attempts,
        "revision_audit": revision_audit,
        "dataset_audit": dataset_audit,
        "baseline_capture": baseline_capture,
        "idea_protocol": idea_protocol,
    }
    (sample_dir / "result.json").write_text(json.dumps(result_summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run smell dataset rows through the mounted-source OpenCode refactor agent.")
    parser.add_argument("--dataset", required=True, help="Path to one supported-language smell dataset CSV file.")
    parser.add_argument("--model", default="zai/glm-4.7")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--opencode-api-key", default="")
    parser.add_argument("--opencode-api-key-env", default="")
    parser.add_argument("--opencode-auth-json", default="auto")
    parser.add_argument("--opencode-base-url", default="")
    parser.add_argument("--runs-root", default=str(ROOT / "runs"))
    parser.add_argument("--run-name", default="")
    parser.add_argument("--projects", default=os.environ.get("SMELL_PROJECTS", ""))
    parser.add_argument("--smell", default="")
    parser.add_argument("--sample-id", action="append")
    parser.add_argument("--project", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--loop-mode", choices=["off", "verify-failure"], default="verify-failure")
    parser.add_argument("--loop-max", type=int, choices=range(0, 6), default=3)
    parser.add_argument("--loop-no-progress-limit", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--loop-on", default="smell,compile,test")
    parser.add_argument("--loop-instruction", default=LoopPolicy().instruction)
    parser.add_argument(
        "--sample-deadline",
        type=int,
        default=1800,
        help="Single per-phase time budget for the command loop and the required final bridge verify; "
        "the runner adds only a 60-second OpenCode shutdown grace.",
    )
    parser.add_argument(
        "--verification-mode",
        choices=sorted(FINAL_VERIFICATION_MODES),
        default="project_full",
    )
    parser.add_argument(
        "--allow-test-changes",
        action="store_true",
        help="Explicitly allow model edits under test-source roots. The controller still freezes verification configuration and requires the full build/test contract.",
    )
    parser.add_argument(
        "--refactoring-backend",
        choices=["direct", "idea"],
        default="direct",
        help="Java edit backend. 'idea' exposes the proposal wrapper while retaining the shared java-refactor-run command and guard state.",
    )
    parser.add_argument(
        "--agent",
        choices=["smell-refactor-agent", "java-refactor-agent"],
        default="",
    )
    parser.add_argument("--no-worktree", dest="worktree", action="store_false", help="Mutate project_path directly. Default is one isolated Git checkout per sample.")
    parser.set_defaults(worktree=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--checkout-only",
        action="store_true",
        help="Diagnostic: create a real isolated checkout pinned to project_commit and verify "
        "it against the manifest, but do NOT invoke opencode/verify. Proves the real refactor "
        "entry pins project_commit without calling the model.",
    )
    parser.add_argument(
        "--project-revisions",
        default=os.environ.get("PROJECT_REVISIONS", DEFAULT_REVISIONS_PATH),
        help="JSON manifest mapping project_name -> {project_commit, tree_hash, ...}. "
        "The real refactor runner pins every checkout to project_commit; HEAD is never used.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The runner owns normalization of dataset/CLI optimization hints. Once
    # test migration is enabled, only the complete project verification
    # contract is an independent behavior gate.
    if args.allow_test_changes:
        args.verification_mode = "project_full"
    if args.refactoring_backend == "idea" and args.agent == "smell-refactor-agent":
        parser.error("--refactoring-backend=idea requires the Java refactor agent")
    # Validate the runner flags through the same parser used by the OpenCode
    # command hook, so batch and direct command invocations cannot drift.
    parse_command_policy(_command_arguments("validation task", args, args.verification_mode))
    dataset = Path(args.dataset).expanduser().resolve()
    samples = _filter_samples(_load_samples(dataset), args)
    if args.refactoring_backend == "idea" and any(sample.language != "java" for sample in samples):
        parser.error("--refactoring-backend=idea supports Java samples only")
    try:
        _validate_model_auth(args)
    except ValueError as exc:
        parser.error(str(exc))
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_name = args.run_name or f"smell-refactor-{dataset.stem}-{timestamp}"
    run_dir = Path(args.runs_root).expanduser().resolve() / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "dataset": str(dataset),
        "model": args.model,
        "selected_count": len(samples),
        "run_dir": str(run_dir),
        "execution_checkout_root": str(_execution_checkout_run_dir(run_dir)),
        "verification_mode": args.verification_mode,
        "refactoring_backend": args.refactoring_backend,
        "loop_policy": parse_command_policy(
            _command_arguments("validation task", args, args.verification_mode)
        ).loop.to_dict(),
        "time_budget": {
            "source": "sample-deadline",
            "sample_deadline_seconds": args.sample_deadline,
            "opencode_shutdown_grace_seconds": OPENCODE_SHUTDOWN_GRACE_SECONDS,
            "opencode_hard_timeout_seconds": _opencode_timeout_seconds(args.sample_deadline),
            "final_verify_timeout_seconds": args.sample_deadline,
            "final_verify_mode": "runner_final",
            "idle_watchdog_enabled": False,
        },
        "dry_run": args.dry_run,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        for sample in samples:
            print(f"{sample.sample_id}\t{sample.smell}\t{sample.project_name}\t{sample.location}")
        return 0

    if args.checkout_only:
        checkout_rows = []
        for index, sample in enumerate(samples, start=1):
            print(f"[checkout-only {index}/{len(samples)}] {sample.sample_id} {sample.project_name}", flush=True)
            row = _checkout_only_sample(sample, run_dir, args)
            checkout_rows.append(row)
            print(f"  -> status={row.get('status')} actual_commit={(row.get('actual_commit') or '')[:12]} "
                  f"actual_tree={(row.get('actual_tree_hash') or '')[:12]} "
                  f"alignment={row.get('project_revision_alignment')}", flush=True)
        (run_dir / "checkout_audit.csv").write_text(
            json.dumps(checkout_rows, indent=2) + "\n", encoding="utf-8"
        )
        return 0 if all(r.get("status") == "CHECKOUT_OK" for r in checkout_rows) else 1

    results_path = run_dir / "results.csv"
    failures = 0
    for index, sample in enumerate(samples, start=1):
        print(f"[{index}/{len(samples)}] {sample.sample_id} {sample.project_name} {sample.smell} {sample.location}", flush=True)
        try:
            row = _run_sample(sample, run_dir, args)
        except Exception as exc:  # keep batch artifacts for the failed row
            # Do not increment failures here: the status-based check below
            # counts RUNNER_FAILED once. Previously this double-counted.
            row = {
                "sample_id": sample.sample_id,
                "smell": sample.smell,
                "project_name": sample.project_name,
                "project_root": str(sample.project_root),
                "execution_project_root": "",
                "location": sample.location,
                "verification_mode": args.verification_mode,
                "agent": _select_agent(sample, args),
                "status": "RUNNER_FAILED",
                "opencode_returncode": "",
                "verify_returncode": "",
                "duration_seconds": "",
                "sample_dir": "",
                "note": str(exc),
            }
        if not _is_accepted_status(row.get("status")):
            failures += 1
        _append_result(results_path, row)
        print(f"  -> {row.get('status')} {row.get('sample_dir')}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
