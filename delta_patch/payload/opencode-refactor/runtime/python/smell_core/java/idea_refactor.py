from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional

from ..config import ResolvedRunConfig


DEFAULT_IDEA_REFACTOR_CLI = "idea-refactor"


@dataclass(frozen=True)
class IdeaRefactorPreflightOptions:
    required: bool = False
    open: bool = False
    timeout: int = 60
    poll_interval: float = 1.0
    cli_path: Optional[str] = None


class IdeaRefactorPreflightError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "IDEA_PRECHECK_FAILED",
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def resolve_idea_refactor_cli(config: ResolvedRunConfig, explicit_cli: Optional[str] = None) -> str:
    return str(
        explicit_cli
        or config.env.get("SMELL_IDEA_REFACTOR_CLI")
        or config.env.get("IDEA_REFACTOR_CLI")
        or DEFAULT_IDEA_REFACTOR_CLI
    ).strip()


def run_idea_refactor_preflight(
    config: ResolvedRunConfig,
    options: IdeaRefactorPreflightOptions,
) -> Dict[str, object]:
    cli_path = resolve_idea_refactor_cli(config, options.cli_path)
    args = [
        cli_path,
        "ensure-service",
        "--project-root",
        str(config.idea_project_root),
        "--timeout",
        str(max(1, int(options.timeout))),
        "--poll-interval",
        str(max(0.1, float(options.poll_interval))),
    ]
    if options.open:
        args.append("--open")

    try:
        proc = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, **config.env},
            timeout=max(30, int(options.timeout) + 30),
        )
    except FileNotFoundError as exc:
        raise IdeaRefactorPreflightError(
            f"IDEA refactor CLI not found: {cli_path}",
            code="IDEA_REFACTOR_CLI_NOT_FOUND",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise IdeaRefactorPreflightError(
            f"IDEA refactor preflight timed out after {options.timeout} seconds.",
            code="IDEA_PRECHECK_TIMEOUT",
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        ) from exc

    payload = _parse_json_object(proc.stdout)
    if proc.returncode == 0 and payload.get("status") == "ok":
        return payload

    message = _diagnostic_summary(payload) or (proc.stderr.strip() or proc.stdout.strip())
    if not message:
        message = f"IDEA refactor preflight failed with exit code {proc.returncode}."
    code = _diagnostic_code(payload) or "IDEA_PRECHECK_FAILED"
    raise IdeaRefactorPreflightError(
        message,
        code=code,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _parse_json_object(text: str) -> Dict[str, object]:
    try:
        parsed = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _diagnostic_summary(payload: Dict[str, object]) -> str:
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list) or not diagnostics:
        return ""
    first = diagnostics[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("summary") or "").strip()


def _diagnostic_code(payload: Dict[str, object]) -> str:
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list) or not diagnostics:
        return ""
    first = diagnostics[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("code") or "").strip()
