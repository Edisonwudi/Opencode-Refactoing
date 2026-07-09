#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.config import VERIFICATION_MODES  # noqa: E402


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
    test_location: str = ""
    test_command: str = ""
    verification_mode: str = ""
    canonical_project_root: Path | None = None


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
    return _run(["git", *args], project_root)


def _load_samples(dataset: Path) -> list[Sample]:
    with dataset.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"sample_id", "language", "smell_type", "project_name", "project_path", "location"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{dataset} is missing columns: {', '.join(sorted(missing))}")
        samples: list[Sample] = []
        for row in reader:
            samples.append(
                Sample(
                    sample_id=str(row["sample_id"]),
                    language=str(row["language"] or "java"),
                    smell=str(row["smell_type"]),
                    project_name=str(row["project_name"]),
                    project_root=Path(row["project_path"]).expanduser().resolve(),
                    location=str(row["location"]),
                    evidence=str(row.get("evidence", "")),
                    raw={str(k): str(v) for k, v in row.items()},
                    test_location=str(row.get("test_location", "")),
                    test_command=str(row.get("test_command", "")),
                    verification_mode=str(row.get("verification_mode", "")),
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
    cli_mode = str(args.verification_mode or "local").strip() or "local"
    sample_mode = str(sample.verification_mode or "").strip()
    requested = "local" if cli_mode == "local" else (sample_mode or cli_mode)
    if requested == "auto":
        requested = "sample_optimized" if sample.test_command.strip() else "project_full"
    if requested not in VERIFICATION_MODES:
        raise ValueError(
            f"Unsupported verification mode '{requested}'. Expected one of: {', '.join(sorted(VERIFICATION_MODES))}."
        )
    return requested


def _strict_mode(mode: str) -> bool:
    return mode != "local"


def _sanitize(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return cleaned.strip("-") or "sample"


def _remap_text(value: str, source_root: Path, target_root: Path) -> str:
    if not value:
        return value
    source = str(source_root)
    target = str(target_root)
    return value.replace(source, target)


def _prepare_worktree(sample: Sample, run_dir: Path) -> Sample:
    canonical_root = sample.project_root.resolve()
    safe_proc = _run(["git", "config", "--global", "--add", "safe.directory", "*"], canonical_root)
    if safe_proc.returncode != 0:
        raise RuntimeError(safe_proc.stderr or safe_proc.stdout or "failed to configure git safe.directory")

    worktree = run_dir / "worktrees" / f"sample-{_sanitize(sample.sample_id)}" / canonical_root.name
    if worktree.exists():
        _git(canonical_root, ["worktree", "remove", "--force", str(worktree)])
        shutil.rmtree(worktree, ignore_errors=True)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    proc = _git(canonical_root, ["worktree", "add", "--detach", str(worktree), "HEAD"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"failed to create worktree at {worktree}")
    filemode_proc = _git(worktree, ["config", "core.filemode", "false"])
    if filemode_proc.returncode != 0:
        raise RuntimeError(filemode_proc.stderr or filemode_proc.stdout or "failed to configure worktree filemode")

    return replace(
        sample,
        project_root=worktree.resolve(),
        canonical_project_root=canonical_root,
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
    source = ROOT / ".opencode"
    target = project_root / ".opencode"
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
    }
    path = sample_dir / "opencode.runtime.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    runtime_env = {api_key_env: api_key} if api_key and api_key_env else {}
    metadata["config_path"] = str(path)
    return path, runtime_env, metadata


def _prepare_opencode_home(sample_dir: Path) -> dict[str, str]:
    home = Path(tempfile.mkdtemp(prefix=f"opencode-home-{_sanitize(sample_dir.name)}-"))
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


def _task_prompt(sample: Sample, args: argparse.Namespace, verification_mode: str, agent: str) -> str:
    idea_enabled = agent == "java-refactor-agent-idea"
    lines = [
        f"Project root: {sample.project_root}",
        f"Language: {sample.language}",
        f"Smell type: {sample.smell}",
        f"Target location: {sample.location}",
    ]
    if sample.evidence:
        lines.append(f"Smell evidence: {sample.evidence}")
    lines.extend(
        [
            f"Verification mode: {verification_mode}",
            f"IDEA preference: {'enabled' if idea_enabled else 'disabled'}",
        ]
    )
    if idea_enabled and args.idea_refactor_cli:
        lines.extend([f"IDEA project root: {sample.project_root}", f"IDEA refactor CLI: {args.idea_refactor_cli}"])
    lines.append("")
    lines.append("Repair this one Java smell from the dataset row. Preserve behavior. Call smell_verify as the final acceptance gate.")
    return "\n".join(lines)


def _copy_verify_artifacts(sample_dir: Path, verify_payload: dict[str, Any]) -> None:
    artifacts = verify_payload.get("artifacts") if isinstance(verify_payload, dict) else None
    if not isinstance(artifacts, dict):
        return
    for key, filename in (("diff", "diff.patch"), ("diff_stat", "diff.stat")):
        source = artifacts.get(key)
        if source and Path(str(source)).is_file():
            shutil.copyfile(str(source), sample_dir / filename)


def _run_verify(sample: Sample, sample_dir: Path, args: argparse.Namespace, verification_mode: str) -> tuple[int, dict[str, Any]]:
    cmd = [
        sys.executable,
        str(ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"),
        "verify",
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
        str(sample_dir / "artifacts"),
    ]
    canonical = sample.canonical_project_root
    if canonical and canonical != sample.project_root:
        cmd.extend(["--project-override-root", str(canonical)])
    if args.projects:
        cmd.extend(["--projects", args.projects])
    if sample.evidence:
        cmd.extend(["--smell-evidence", sample.evidence])
    if sample.test_location:
        cmd.extend(["--sample-test-location", sample.test_location])
    if sample.test_command:
        cmd.extend(["--sample-test-command", sample.test_command])

    env = os.environ.copy()
    if _strict_mode(verification_mode):
        env["SMELL_REQUIRE_BUILD_TEST"] = "1"
    else:
        env.pop("SMELL_REQUIRE_BUILD_TEST", None)
    proc = _run(cmd, ROOT, env=env, timeout=args.verify_timeout)
    payload: dict[str, Any]
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"success": False, "status": "VERIFY_OUTPUT_PARSE_FAILED", "stdout": proc.stdout, "stderr": proc.stderr}
    (sample_dir / "verify.json").write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    _copy_verify_artifacts(sample_dir, payload)
    return proc.returncode, payload


def _run_opencode(sample: Sample, sample_dir: Path, args: argparse.Namespace, agent: str, verification_mode: str) -> int:
    _bootstrap_opencode(sample.project_root, sample_dir)
    config_path, runtime_env, auth_meta = _write_opencode_config(sample_dir, args)
    task = _task_prompt(sample, args, verification_mode, agent)
    (sample_dir / "task.txt").write_text(task + "\n", encoding="utf-8")

    env = os.environ.copy()
    env.update(_prepare_opencode_home(sample_dir))
    env.update(runtime_env)
    if config_path:
        env["OPENCODE_CONFIG"] = str(config_path)
    env["SMELL_PROJECT_ROOT"] = str(sample.project_root)
    if sample.canonical_project_root:
        env["SMELL_CANONICAL_PROJECT_ROOT"] = str(sample.canonical_project_root)
    env["SMELL_LANGUAGE"] = sample.language
    env["SMELL_SMELL"] = sample.smell
    env["SMELL_LOCATION"] = sample.location
    env["SMELL_EVIDENCE"] = sample.evidence
    env["SMELL_VERIFICATION_MODE"] = verification_mode
    env["SMELL_SAMPLE_TEST_LOCATION"] = sample.test_location
    env["SMELL_SAMPLE_TEST_COMMAND"] = sample.test_command
    if args.projects:
        env["SMELL_PROJECTS"] = args.projects
    if args.idea_refactor_cli:
        env["SMELL_IDEA_REFACTOR_CLI"] = args.idea_refactor_cli
    if _strict_mode(verification_mode):
        env["SMELL_REQUIRE_BUILD_TEST"] = "1"
    else:
        env.pop("SMELL_REQUIRE_BUILD_TEST", None)

    cmd = [args.opencode_bin, "run", task, "--agent", agent, "--model", args.model, "--dangerously-skip-permissions", "--print-logs"]
    command_payload = {
        "cmd": cmd,
        "cwd": str(sample.project_root),
        "agent": agent,
        "auth": {**auth_meta, "api_key_source": "configured" if auth_meta.get("api_key_configured") else ""},
        "verification_mode": verification_mode,
    }
    (sample_dir / "command.json").write_text(json.dumps(command_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    log_path = sample_dir / "run.log"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(sample.project_root),
            env=env,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + args.timeout
        last_marker = (-1, -1)
        last_activity = time.monotonic()
        while proc.poll() is None:
            if time.monotonic() > deadline:
                os.killpg(proc.pid, signal.SIGTERM)
                return 124
            marker = _file_marker(log_path)
            if marker != last_marker:
                last_marker = marker
                last_activity = time.monotonic()
            if args.opencode_log_idle_timeout and time.monotonic() - last_activity > args.opencode_log_idle_timeout:
                os.killpg(proc.pid, signal.SIGTERM)
                return 125
            time.sleep(1)
        return int(proc.returncode or 0)


def _file_marker(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (-1, -1)
    return (stat.st_size, stat.st_mtime_ns)


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
        "opencode_returncode",
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


def _run_sample(sample: Sample, run_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    sample_dir = run_dir / "samples" / f"sample-{_sanitize(sample.sample_id)}-{_sanitize(sample.project_name)}"
    sample_dir.mkdir(parents=True, exist_ok=True)
    agent = args.agent or ("java-refactor-agent-idea" if args.idea else "java-refactor-agent")
    verification_mode = _effective_verification_mode(sample, args)

    execution_sample = _prepare_worktree(sample, run_dir) if args.worktree else replace(sample, canonical_project_root=sample.project_root)
    (sample_dir / "sample.json").write_text(
        json.dumps({**sample.raw, "execution_project_root": str(execution_sample.project_root)}, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    opencode_returncode = _run_opencode(execution_sample, sample_dir, args, agent, verification_mode)
    verify_returncode, verify_payload = _run_verify(execution_sample, sample_dir, args, verification_mode)
    verify_status = str(verify_payload.get("status") or "")
    status = verify_status if verify_returncode == 0 else (verify_status or "VERIFY_FAILED")
    if opencode_returncode != 0 and verify_returncode != 0:
        status = "OPENCODE_FAILED"
    row = {
        "sample_id": sample.sample_id,
        "smell": sample.smell,
        "project_name": sample.project_name,
        "project_root": str(sample.project_root),
        "execution_project_root": str(execution_sample.project_root),
        "location": execution_sample.location,
        "verification_mode": verification_mode,
        "agent": agent,
        "status": status,
        "opencode_returncode": opencode_returncode,
        "verify_returncode": verify_returncode,
        "duration_seconds": f"{time.time() - started:.1f}",
        "sample_dir": str(sample_dir),
        "note": "",
    }
    (sample_dir / "result.json").write_text(json.dumps(row, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Java smell dataset rows through the minimal OpenCode Java refactor agent.")
    parser.add_argument("--dataset", required=True, help="Path to one Java delivery_schema CSV file.")
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
    parser.add_argument("--timeout", type=int, default=900, help="Seconds allowed for OpenCode per sample.")
    parser.add_argument("--verify-timeout", type=int, default=900, help="Seconds allowed for final independent verify.")
    parser.add_argument("--opencode-log-idle-timeout", type=int, default=180, help="Stop OpenCode after this many seconds with no log growth. Use 0 to disable.")
    parser.add_argument("--verification-mode", choices=sorted(VERIFICATION_MODES), default="local")
    parser.add_argument("--agent", choices=["java-refactor-agent", "java-refactor-agent-idea"], default="")
    parser.add_argument("--idea", action="store_true", help="Use java-refactor-agent-idea and expose IDEA CLI.")
    parser.add_argument("--idea-refactor-cli", default=os.environ.get("IDEA_REFACTOR_CLI", ""))
    parser.add_argument("--no-worktree", dest="worktree", action="store_false", help="Mutate project_path directly. Default is per-sample git worktree.")
    parser.set_defaults(worktree=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset = Path(args.dataset).expanduser().resolve()
    samples = _filter_samples(_load_samples(dataset), args)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_name = args.run_name or f"java-refactor-{dataset.stem}-{timestamp}"
    run_dir = Path(args.runs_root).expanduser().resolve() / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "dataset": str(dataset),
        "model": args.model,
        "selected_count": len(samples),
        "run_dir": str(run_dir),
        "verification_mode": args.verification_mode,
        "dry_run": args.dry_run,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        for sample in samples:
            print(f"{sample.sample_id}\t{sample.smell}\t{sample.project_name}\t{sample.location}")
        return 0

    results_path = run_dir / "results.csv"
    failures = 0
    for index, sample in enumerate(samples, start=1):
        print(f"[{index}/{len(samples)}] {sample.sample_id} {sample.project_name} {sample.smell} {sample.location}", flush=True)
        try:
            row = _run_sample(sample, run_dir, args)
        except Exception as exc:  # keep batch artifacts for the failed row
            failures += 1
            row = {
                "sample_id": sample.sample_id,
                "smell": sample.smell,
                "project_name": sample.project_name,
                "project_root": str(sample.project_root),
                "execution_project_root": "",
                "location": sample.location,
                "verification_mode": args.verification_mode,
                "agent": args.agent or ("java-refactor-agent-idea" if args.idea else "java-refactor-agent"),
                "status": "RUNNER_FAILED",
                "opencode_returncode": "",
                "verify_returncode": "",
                "duration_seconds": "",
                "sample_dir": "",
                "note": str(exc),
            }
        if row.get("status") != "PASS":
            failures += 1
        _append_result(results_path, row)
        print(f"  -> {row.get('status')} {row.get('sample_dir')}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
