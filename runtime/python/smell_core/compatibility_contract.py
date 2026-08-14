"""Target-local public API and virtual ABI continuity checks.

The contract consumes only declarations already frozen by a Target Guard and
the production-only patch already captured by the checkpoint.  It never walks
or searches the project tree.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


TARGET_LOCAL_COMPATIBILITY_CONTRACT = "target-local-api-abi-continuity-v2"
_HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".hxx")
_DECLARATION_TOKEN = re.compile(
    r"\*\*|->|::|\.\.\.|[A-Za-z_]\w*|0[xX][0-9A-Fa-f]+|"
    r"\d+(?:\.\d+)?|[^\s]"
)


def evaluate_target_local_compatibility(
    *,
    language: str,
    baseline_targets: Iterable[Mapping[str, Any]],
    current_targets: Iterable[Mapping[str, Any]],
    production_patch: str | None,
) -> dict[str, Any]:
    """Return high-confidence compatibility violations in the supplied scope."""
    normalized_language = str(language).strip().lower()
    violations: list[dict[str, Any]] = []
    if normalized_language == "python":
        violations.extend(
            _python_public_signature_violations(
                baseline_targets,
                current_targets,
            )
        )
    elif normalized_language in {"c", "cpp"}:
        violations.extend(_virtual_abi_violations(production_patch))
    return {
        "contract": TARGET_LOCAL_COMPATIBILITY_CONTRACT,
        "applicable": normalized_language in {"python", "c", "cpp"},
        "ok": not violations,
        "violations": violations,
        "scope": "frozen_targets_and_changed_production_hunks",
    }


def _python_public_signature_violations(
    baseline_targets: Iterable[Mapping[str, Any]],
    current_targets: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    baseline = {
        int(item.get("target_index")): item
        for item in baseline_targets
        if isinstance(item.get("target_index"), int)
    }
    current = {
        int(item.get("target_index")): item
        for item in current_targets
        if isinstance(item.get("target_index"), int)
    }
    violations: list[dict[str, Any]] = []
    for target_index, frozen in sorted(baseline.items()):
        identity = frozen.get("declaration_identity")
        if not isinstance(identity, Mapping) or not _is_public_python_identity(identity):
            continue
        candidate = current.get(target_index)
        if not isinstance(candidate, Mapping) or candidate.get("resolved") is not True:
            # Target identity/deletion is handled by the existing declaration
            # continuity gate. Do not create a second diagnosis here.
            continue
        before = _parameter_contract(str(frozen.get("signature_text") or ""))
        after = _parameter_contract(str(candidate.get("signature_text") or ""))
        if before is not None and not _is_high_risk_python_api(identity, before):
            continue
        if before is None or after is None:
            violations.append({
                "code": "PUBLIC_PYTHON_SIGNATURE_UNAVAILABLE",
                "target_index": target_index,
                "file": str(frozen.get("file") or ""),
                "owner": str(identity.get("owner_qualified_name") or ""),
                "method": str(identity.get("declared_name") or ""),
            })
        elif before != after:
            violations.append({
                "code": "PUBLIC_PYTHON_SIGNATURE_CHANGED",
                "target_index": target_index,
                "file": str(frozen.get("file") or ""),
                "owner": str(identity.get("owner_qualified_name") or ""),
                "method": str(identity.get("declared_name") or ""),
                "baseline_parameter_contract": list(before),
                "current_parameter_contract": list(after),
            })
    return violations


def _is_public_python_identity(identity: Mapping[str, Any]) -> bool:
    name = str(identity.get("declared_name") or "")
    owner = str(identity.get("owner_qualified_name") or "")
    if not name:
        return False
    owner_name = re.split(r"[.:]", owner)[-1] if owner else ""
    if name in {"__init__", "__new__"}:
        return bool(owner_name and not owner_name.startswith("_"))
    return not name.startswith("_") and (
        not owner_name or not owner_name.startswith("_")
    )


def _is_high_risk_python_api(
    identity: Mapping[str, Any],
    parameters: tuple[str, ...],
) -> bool:
    """Select declaration shapes with a locally provable compatibility risk.

    Public-class construction is an API boundary by definition.  For ordinary
    Python callables, the target-local declaration alone cannot prove module
    export status; only freeze signatures with optional/defaulted arguments,
    because removing that established call shape is a concrete backward-call
    incompatibility rather than a guess based on a repository scan.
    """
    name = str(identity.get("declared_name") or "")
    if name in {"__init__", "__new__"}:
        return True
    return "=" in parameters


def _parameter_contract(signature: str) -> tuple[str, ...] | None:
    start = signature.find("(")
    if start < 0:
        return None
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(signature)):
        character = signature[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return tuple(_DECLARATION_TOKEN.findall(signature[start + 1:index]))
    return None


def _virtual_abi_violations(production_patch: str | None) -> list[dict[str, Any]]:
    if production_patch is None:
        return [{
            "code": "VIRTUAL_ABI_PATCH_UNAVAILABLE",
        }]
    removed_by_file = _changed_virtual_declarations(production_patch, "-")
    added_by_file = _changed_virtual_declarations(production_patch, "+")
    violations: list[dict[str, Any]] = []
    for file_name, removed_lines in sorted(removed_by_file.items()):
        if not file_name.lower().endswith(_HEADER_SUFFIXES):
            continue
        removed = _pure_virtual_declarations(removed_lines)
        if not removed:
            continue
        added = {
            _normalize_declaration(value)
            for value in _pure_virtual_declarations(
                added_by_file.get(file_name, [])
            )
        }
        for declaration in removed:
            normalized = _normalize_declaration(declaration)
            if normalized in added:
                continue
            violations.append({
                "code": "CPP_PURE_VIRTUAL_ABI_CHANGED",
                "file": file_name,
                "method": _cpp_declaration_name(declaration),
                "baseline_declaration": normalized,
            })
    return violations


def _changed_virtual_declarations(
    patch: str,
    marker: str,
) -> dict[str, list[str]]:
    """Collect changed virtual declarations, including unchanged prefixes.

    Unified diffs commonly leave the first line of a multi-line declaration as
    context and change only its final parameter line.  Retaining that bounded
    hunk context is necessary to recognize the declaration without opening or
    searching the header.
    """
    result: dict[str, list[str]] = {}
    current_file = ""
    context_prefix: list[str] = []
    pending: list[str] = []
    lexical_owner = ""
    pending_lexical_owner = ""

    def flush() -> None:
        nonlocal pending
        if pending and current_file:
            result.setdefault(current_file, []).append(" ".join(pending))
        pending = []

    for line in patch.splitlines():
        if line.startswith("diff --git ") or line.startswith("@@ "):
            flush()
            context_prefix = []
            lexical_owner = ""
            pending_lexical_owner = ""
            continue
        if line.startswith("+++ "):
            flush()
            rendered = line[4:].strip()
            current_file = (
                ""
                if rendered == "/dev/null"
                else (rendered[2:] if rendered.startswith("b/") else rendered)
            )
            context_prefix = []
            lexical_owner = ""
            pending_lexical_owner = ""
            continue
        if not current_file or line.startswith("--- "):
            continue
        if line.startswith(" "):
            flush()
            stripped = line[1:].strip()
            owner = _cpp_lexical_owner_open(stripped)
            if owner:
                lexical_owner = owner
                pending_lexical_owner = ""
            else:
                declared_owner = _cpp_lexical_owner_declaration(stripped)
                if declared_owner:
                    pending_lexical_owner = declared_owner
                elif pending_lexical_owner and "{" in stripped:
                    lexical_owner = pending_lexical_owner
                    pending_lexical_owner = ""
            if "virtual" in stripped:
                context_prefix = [
                    *([f"class {lexical_owner} {{"] if lexical_owner else []),
                    stripped,
                ]
            elif context_prefix and not _declaration_terminated(context_prefix):
                context_prefix.append(stripped)
            else:
                context_prefix = []
            if stripped.startswith("}"):
                lexical_owner = ""
                pending_lexical_owner = ""
            continue
        if line.startswith(marker) and not line.startswith(marker * 3):
            stripped = line[1:].strip()
            owner = _cpp_lexical_owner_open(stripped)
            if owner:
                lexical_owner = owner
                pending_lexical_owner = ""
            else:
                declared_owner = _cpp_lexical_owner_declaration(stripped)
                if declared_owner:
                    pending_lexical_owner = declared_owner
                elif pending_lexical_owner and "{" in stripped:
                    lexical_owner = pending_lexical_owner
                    pending_lexical_owner = ""
            if not pending:
                pending = (
                    [*context_prefix, stripped]
                    if context_prefix and "virtual" not in stripped
                    else [
                        *(
                            [f"class {lexical_owner} {{"]
                            if lexical_owner
                            and "virtual" in stripped
                            and not owner
                            else []
                        ),
                        stripped,
                    ]
                )
            else:
                pending.append(stripped)
            if _declaration_terminated(pending):
                flush()
            if stripped.startswith("}"):
                lexical_owner = ""
                pending_lexical_owner = ""
            continue
        # The opposite side of the replacement does not invalidate the common
        # context prefix, but it does end this side's contiguous declaration.
        flush()
    flush()
    return result


def _cpp_lexical_owner_open(line: str) -> str:
    match = re.search(
        r"\b(?:class|struct)\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\b[^;{}]*\{",
        str(line or ""),
    )
    return match.group(1) if match else ""


def _cpp_lexical_owner_declaration(line: str) -> str:
    """Remember a class name whose opening brace is on a later diff line."""
    text = str(line or "").strip()
    if "{" in text or ";" in text:
        return ""
    match = re.match(
        r"^(?:class|struct)\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\b",
        text,
    )
    return match.group(1) if match else ""


def _pure_virtual_declarations(lines: Iterable[str]) -> list[str]:
    declarations: list[str] = []
    pending: list[str] = []
    for line in lines:
        stripped = str(line).strip()
        if not stripped:
            pending = []
            continue
        if not pending and "virtual" not in stripped:
            continue
        pending.append(stripped)
        joined = " ".join(pending)
        if ";" not in stripped and "{}" not in stripped:
            continue
        if re.search(r"\bvirtual\b", joined) and re.search(r"=\s*0\s*;", joined):
            declarations.append(joined)
        pending = []
    return declarations


def _declaration_terminated(lines: Iterable[str]) -> bool:
    rendered = " ".join(lines)
    return ";" in rendered or "{}" in rendered


def _normalize_declaration(value: str) -> str:
    return " ".join(_DECLARATION_TOKEN.findall(value))


def _cpp_declaration_name(value: str) -> str:
    prefix = value.split("(", 1)[0]
    names = re.findall(r"(?:~?[A-Za-z_]\w*)", prefix)
    return names[-1] if names else ""
