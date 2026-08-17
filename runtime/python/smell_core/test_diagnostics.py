"""Compact, product-path test failure diagnostics shared with runner audits."""
from __future__ import annotations

import json
import re
from typing import Any


VERIFY_STEP_NAMES = ("build", "test", "sample_test")
TMUX_FAILURE_HEADER = re.compile(
    r"\bFAIL ([A-Za-z0-9_.+/-]+): exit (-?[0-9]+)\b"
)
TMUX_FAILURE_CASE_MARKER = "TMUX_FAIL_CASE "


def build_test_details(payload: dict[str, Any]) -> dict[str, Any]:
    guard = payload.get("build_test_guard") if isinstance(payload, dict) else None
    details = guard.get("details") if isinstance(guard, dict) else None
    return details if isinstance(details, dict) else {}


def diagnostic_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(diagnostic_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(diagnostic_text(item) for item in value.values())
    return str(value)


def step_diagnostic_text(step: dict[str, Any]) -> str:
    """Return diagnostics while deliberately excluding command/script fields."""
    selected = [
        step.get(key)
        for key in (
            "summary_text",
            "summary",
            "failure_highlights",
            "tail",
            "message",
            "error",
            "stderr",
            "stdout",
        )
        if step.get(key) is not None
    ]
    execution = step.get("execution")
    if isinstance(execution, dict):
        selected.extend(
            execution.get(key)
            for key in (
                "summary_text",
                "summary",
                "failure_highlights",
                "tail",
                "message",
                "error",
                "stderr",
                "stdout",
            )
            if execution.get(key) is not None
        )
    return "\n".join(diagnostic_text(item) for item in selected)


def step_failed(step: Any) -> bool:
    if not isinstance(step, dict):
        return False
    status = str(step.get("status") or "").strip().casefold()
    return (
        step.get("success") is False
        or status in {"fail", "failed", "error", "timeout", "timed_out"}
        or (isinstance(step.get("returncode"), int) and step["returncode"] != 0)
    )


def failed_build_test_steps(
    payload: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    details = build_test_details(payload)
    return [
        (name, details[name])
        for name in VERIFY_STEP_NAMES
        if isinstance(details.get(name), dict) and step_failed(details[name])
    ]


def normalized_test_failure_fingerprint(exit_code: int, diagnostic: str) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(diagnostic or ""))
    text = re.sub(r"/(?:tmp|var/tmp)/[^\s'\"]+", "<tmp>", text)
    text = re.sub(r"['\"],\s*['\"]", "\n", text)
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    notable = [
        line
        for line in lines
        if line == "TIMEOUT"
        or line.startswith("[FAIL]")
        or re.search(
            r"(?:failed|error|unexpected|no such file)",
            line,
            re.IGNORECASE,
        )
    ]
    selected = notable[:3] if notable else lines[:3]
    if "TIMEOUT" in notable and "TIMEOUT" not in selected:
        selected = ["TIMEOUT", *selected[:2]]
    detail = " | ".join(selected)
    return f"exit={exit_code}" + (f" | {detail}" if detail else "")


def failed_test_diagnostics(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract compact named failures without retaining full test logs."""
    marked: set[tuple[str, int, str]] = set()
    legacy: set[tuple[str, int, str]] = set()
    fallback: set[tuple[str, int, str]] = set()
    decoder = json.JSONDecoder()
    for step_name, step in failed_build_test_steps(payload):
        if step_name not in {"test", "sample_test"}:
            continue
        text = step_diagnostic_text(step)
        marker_offset = 0
        while True:
            marker_offset = text.find(TMUX_FAILURE_CASE_MARKER, marker_offset)
            if marker_offset < 0:
                break
            payload_offset = marker_offset + len(TMUX_FAILURE_CASE_MARKER)
            try:
                item, consumed = decoder.raw_decode(text[payload_offset:])
            except json.JSONDecodeError:
                marker_offset = payload_offset
                continue
            marker_offset = payload_offset + consumed
            if not isinstance(item, dict):
                continue
            test_name = str(item.get("test") or "")
            exit_code = item.get("exit_code")
            fingerprint = str(item.get("diagnostic_fingerprint") or "")
            if (
                re.fullmatch(r"[A-Za-z0-9_.+/-]+", test_name)
                and isinstance(exit_code, int)
                and fingerprint
            ):
                marked.add((test_name, exit_code, fingerprint[:600]))
        matches = list(TMUX_FAILURE_HEADER.finditer(text))
        for index, match in enumerate(matches):
            block_end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(text)
            )
            exit_code = int(match.group(2))
            legacy.add(
                (
                    match.group(1),
                    exit_code,
                    normalized_test_failure_fingerprint(
                        exit_code,
                        text[match.end():block_end],
                    )[:600],
                )
            )
        if not matches and TMUX_FAILURE_CASE_MARKER not in text:
            returncode = step.get("returncode")
            exit_code = returncode if isinstance(returncode, int) else 1
            fallback.add(
                (
                    step_name,
                    exit_code,
                    normalized_test_failure_fingerprint(exit_code, text)[:600],
                )
            )
    selected = marked or legacy or fallback
    by_case: dict[tuple[str, int], str] = {}
    for test_name, exit_code, fingerprint in selected:
        key = (test_name, exit_code)
        if len(fingerprint) > len(by_case.get(key, "")):
            by_case[key] = fingerprint
    return [
        {
            "test": test_name,
            "exit_code": exit_code,
            "diagnostic_fingerprint": fingerprint,
        }
        for (test_name, exit_code), fingerprint in sorted(by_case.items())
    ]


def failed_test_diagnostic_signature(payload: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        f'{item["test"]}|{item["exit_code"]}|{item["diagnostic_fingerprint"]}'
        for item in failed_test_diagnostics(payload)
    )


def compact_test_diagnostic_signature(
    payload: dict[str, Any],
    *,
    limit: int = 128,
) -> str:
    """Keep each failing case visible within the formal receipt size bound."""
    diagnostics = failed_test_diagnostics(payload)
    if not diagnostics:
        return ""
    selected = diagnostics[:8]
    prefix = f"tests={len(diagnostics)}:"
    per_item = max(8, (limit - len(prefix) - len(selected) + 1) // len(selected))
    items = [
        (
            f'{item["test"]}@{item["exit_code"]}:'
            f'{item["diagnostic_fingerprint"]}'
        )[:per_item]
        for item in selected
    ]
    return (prefix + ";".join(items))[:limit]
