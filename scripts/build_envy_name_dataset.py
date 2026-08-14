#!/usr/bin/env python3
"""Build feature_envy / mysterious_name dataset CSVs for c/cpp/python.

Scans the 40 pinned non-Java project checkouts with the smell_core
tree-sitter primitives, curates 30 samples per (language, smell), validates
every sample with the authoritative detectors
(``analyze_feature_envy_target`` / ``detect_mysterious_names``) and writes
``<lang>/{feature_envy,mysterious_name}_30.csv`` into ``dataset/nonjava/``
in this repository.

Dataset CSVs are stored in container (image) path format
(``/opt/projects/<lang>/<name>``). Candidates and the scan cache keep local
checkout paths supplied through repeated ``--project-root LANG=PATH`` options;
``write_csv`` is the only container-path conversion boundary.
Reads of existing CSV rows only use path-free keys
(project_name, file, begin_line), so both path formats round-trip cleanly.

Candidate generation mirrors the detector logic exactly (same thresholds,
same dominant-receiver selection) and final samples are re-checked through
the public detector APIs, so no threshold is ever loosened to fill quota.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON = ROOT / "runtime" / "python"
if str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

from smell_core.analysis import (  # noqa: E402
    COMPLEXITY_NODE_TYPES,
    LANGUAGE_EXTENSIONS,
    _build_source_snippet,
    _extract_declared_name,
    _iter_nodes,
    _iter_source_files,
    _node_text,
    _parameter_fingerprints_from_node,
    _root_receiver_identifier,
    count_meaningful_lines,
    iter_local_variable_names,
    iter_member_accesses,
    parse_function_nodes,
)
from smell_core.feature_envy import (  # noqa: E402
    FEATURE_ENVY_FOREIGN_RATIO,
    FEATURE_ENVY_MIN_FOREIGN_ACCESS,
    FEATURE_ENVY_MIN_LOC,
    _LOCAL_RECEIVERS,
    _parameter_type_map,
    _simple_type_name,
    analyze_feature_envy_target,
)
from smell_core.mysterious_name import (  # noqa: E402
    detect_mysterious_names,
    suspicious_name_reason,
)

DEFAULT_DATASET_ROOT = ROOT / "dataset" / "nonjava"
CACHE_DIR = ROOT / "runs" / "build_envy_name_dataset"

# Dataset CSVs store image-format roots; local checkouts back them.
CONTAINER_PROJECT_PREFIX = "/opt/projects"


def _container_project_root(language: str, project_name: str) -> str:
    return f"{CONTAINER_PROJECT_PREFIX}/{language}/{project_name}"


def _local_project_root(
    project_roots: dict[str, Path], language: str, project_name: str
) -> Path:
    """Local checkout for a project, regardless of CSV path format."""
    return project_roots[language] / project_name


PROJECTS = {
    "c": ["cJSON", "curl", "git", "libevent", "libssh2", "libuv", "lua", "nginx", "redis", "rrdtool", "tmux"],
    "cpp": [
        "CMake", "Catch2", "OpenTTD", "aria2", "duckdb", "fmt", "json-3.11.3",
        "poco-poco-1.14.2-release", "protobuf-29.3", "rocksdb", "spdlog-1.14.1", "yaml-cpp",
    ],
    "python": [
        "airflow", "dagster", "dask", "dbt-core", "django", "fsspec", "luigi", "mlflow",
        "poetry", "prefect", "pytest", "ray", "requests", "salt", "scrapy", "tornado", "yt-dlp",
    ],
}
SMELLS = ("feature_envy", "mysterious_name")
SAMPLE_COUNT = 30
MAX_PER_PROJECT = 7


def _parse_project_roots(
    parser: argparse.ArgumentParser,
    values: list[str],
    languages: list[str],
) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        language, separator, raw_path = value.partition("=")
        language = language.strip()
        raw_path = raw_path.strip()
        if not separator or language not in PROJECTS or not raw_path:
            parser.error(
                "--project-root must use LANG=PATH with LANG in "
                + ", ".join(sorted(PROJECTS))
            )
        if language in roots:
            parser.error(f"duplicate --project-root for {language}")
        root = Path(raw_path).expanduser().resolve()
        if not root.is_dir():
            parser.error(f"--project-root for {language} is not a directory: {root}")
        roots[language] = root
    missing = [language for language in languages if language not in roots]
    if missing:
        parser.error(
            "missing --project-root for selected language(s): " + ", ".join(missing)
        )
    return roots


def _rebase_cached_project_roots(
    cache: dict[str, dict[str, dict[str, list[dict]]]],
    project_roots: dict[str, Path],
) -> None:
    """Rebind cached candidates to the roots declared for this invocation."""
    for language, language_root in project_roots.items():
        projects = cache.get(language)
        if not isinstance(projects, dict):
            continue
        for project, findings_by_smell in projects.items():
            if not isinstance(findings_by_smell, dict):
                continue
            current_root = str(language_root / project)
            for candidates in findings_by_smell.values():
                if not isinstance(candidates, list):
                    continue
                for candidate in candidates:
                    if isinstance(candidate, dict):
                        candidate["project_root"] = current_root


# Skip test trees: bare test/tests/testing dirs, poco-style testsuite dirs, and
# *Test.* / *Tests.* file names (case-sensitive so latest.c/contest.cpp survive).
TEST_DIR_NAMES = {"test", "tests", "testing", "testsuite"}
TEST_FILE_STEM_RE = re.compile(r"Tests?$")
# Vendored or machine-generated code is not a meaningful refactoring target.
VENDOR_DIR_NAMES = {"third_party", "3rdparty", "vendor", "vendored", "generated", "snowball", "extras"}
# Backstop against misparsed mega-spans (see function-node has_error skip).
MAX_FUNCTION_SPAN_LINES = 1500

# Human review 20260721: samples judged not refactorable.  Heuristic filters
# cover the systemic classes (vendored/generated code, test files, misparsed
# spans); the remaining judgment calls (thin wrappers, pure dispatchers,
# ucontext_t-bound code, already-optimal singledispatch) are pinned here as
# (language, project, file, begin_line) so they can never re-enter the dataset.
REVIEW_EXCLUSIONS = {
    ("c", "libssh2", "src/session.c", 919),               # session_free: pure destructor walk
    ("c", "redis", "src/debug.c", 1489),                  # logRegisters: register dump, ucontext_t
    ("c", "curl", "lib/setopt.c", 1861),                  # setopt_cptr: pure option assignment dispatch
    ("c", "lua", "lstate.c", 341),                        # lua_newstate: field-fill factory
    ("c", "curl", "src/tool_getparam.c", 2455),           # opt_string: pure option dispatch
    ("c", "libuv", "src/win/pipe.c", 1578),               # uv__pipe_write_data: misplaced ownership
    ("c", "lua", "ldo.c", 1153),                          # luaD_protectedparser: thin wrapper
    ("c", "lua", "lcode.c", 1357),                       # luaK_indexed: envied_type=expdesc ambiguously names t and k
    ("cpp", "duckdb", "third_party/mbedtls/library/sha1.cpp", 65),  # anonymous local struct receiver
    ("cpp", "protobuf-29.3", "third_party/abseil-cpp/absl/debugging/internal/demangle.cc", 27),  # namespace misparse
    ("cpp", "rocksdb", "tools/db_bench_tool.cc", 4488),   # InitializeOptionsFromFlags: FLAGS fill
    ("cpp", "duckdb", "third_party/snowball/src_c/stem_UTF_8_turkish.cpp", 1354),  # generated state machine
    ("cpp", "poco-poco-1.14.2-release", "Foundation/testsuite/src/URITest.cpp", 171),  # test file
    ("cpp", "rocksdb", "db_stress_tool/db_stress_test_base.cc", 4437),  # duplicate FLAGS fill
    ("cpp", "duckdb", "third_party/snowball/src_c/stem_UTF_8_tamil.cpp", 1498),  # generated state machine
    ("cpp", "poco-poco-1.14.2-release", "XML/testsuite/src/XMLStreamParserTest.cpp", 35),  # test file
    ("cpp", "protobuf-29.3", "third_party/abseil-cpp/absl/debugging/internal/examine_stack.cc", 46),  # ucontext_t
    ("cpp", "rocksdb", "tools/ldb_cmd.cc", 299),          # SelectCommand: factory dispatch
    ("cpp", "yaml-cpp", "src/scantoken.cpp", 295),        # ScanPlainScalar: pure fill
    ("python", "dask", "dask/dataframe/backends.py", 378),  # singledispatch already optimal
    ("python", "mlflow", "mlflow/server/auth/__init__.py", 4063),  # route-registration factory
    ("python", "pytest", "src/_pytest/main.py", 57),      # pytest_addoption: option declarations
    # Human review round 2 (20260721): sink/receiver-output and registry fills.
    ("c", "libuv", "src/win/tty.c", 205),                 # uv_tty_init: pure init fill
    ("cpp", "Catch2", "src/catch2/reporters/catch_reporter_xml.cpp", 236),  # XML field shuffle
    ("cpp", "protobuf-29.3", "src/google/protobuf/compiler/java/lite/map_field.cc", 558),  # codegen template printing
    ("cpp", "yaml-cpp", "src/stream.cpp", 161),           # QueueUnicodeCodepoint: std::deque output sink
    ("cpp", "CMake", "Source/cmCommands.cxx", 228),       # GetProjectCommands: registry fill
    ("cpp", "aria2", "src/version_usage.cc", 84),         # showUsage: Console printf sink
    # Human review round 3 (20260721).
    ("cpp", "CMake", "Source/kwsys/SystemTools.cxx", 4309),  # OSVERSIONINFOEXA is an OS struct; declarative mapping table
    ("cpp", "Catch2", "extras/catch_amalgamated.cpp", 9736),  # amalgamated duplicate of catch_reporter_helpers.cpp:298
    # Human review round 4 (20260721).
    ("cpp", "Catch2", "src/catch2/reporters/catch_reporter_xml.cpp", 207),  # testRunEnded: output-sink DTO
    ("cpp", "OpenTTD", "src/newgrf_text.cpp", 237),       # TranslateTTDPatchCodes: builder is a pure output sink
    ("cpp", "duckdb", "extension/parquet/parquet_reader.cpp", 230),  # DeriveLogicalType: thrift-generated SchemaElement
    # Human review round 5 (20260721).
    ("cpp", "CMake", "Source/cmExtraCodeBlocksGenerator.cxx", 485),  # AppendTarget: cmXMLWriter is a pure write-out sink
    ("cpp", "Catch2", "src/catch2/reporters/catch_reporter_console.cpp", 488),  # benchmarkEnded: POD field reads, no computation
}

FIELDNAMES = [
    "sample_id", "language", "smell_type", "project_name", "project_path", "file",
    "method", "begin_line", "end_line", "metric_value", "location", "is_test", "evidence",
    "target_context_json",
]


def _skip_path(rel_path: Path) -> bool:
    """Test trees/files and vendored or generated code are out of scope."""
    parts = [part.lower() for part in rel_path.parts[:-1]]
    if any(part in TEST_DIR_NAMES for part in parts):
        return True
    if any(part in VENDOR_DIR_NAMES for part in parts):
        return True
    stem = rel_path.stem.lower()
    if stem.startswith("test_") or stem.endswith("_test"):
        return True
    return bool(TEST_FILE_STEM_RE.search(rel_path.stem))


def _method_label(name: str, signature_text: str) -> str:
    text = re.sub(r"\s+", " ", signature_text).strip()
    # Macro-prefixed signatures (e.g. CJSON_PUBLIC(void) cJSON_Delete(...)) break
    # a naive first-"(" split, so anchor on the function name itself.
    matches = list(re.finditer(re.escape(name) + r"\s*\(", text)) if name else []
    if matches:
        start = matches[-1].end()
        close = text.rfind(")")
        if close >= start:
            return f"{name}({text[start:close].strip()})"
    if "(" in text and ")" in text:
        inner = text.split("(", 1)[1].rsplit(")", 1)[0].strip()
        return f"{name}({inner})"
    return name


# --- feature_envy refactorability filter -----------------------------------
# Samples whose body is a pure field-copy factory (e.g. duckdb CreateAPIv1:
# 548 lines of `result.duckdb_xxx = duckdb_xxx;`) carry no movable behavior and
# only invite metric-gaming rewrites.  A candidate is excluded only when ALL of
# the following hold for its dominant receiver:
#   - write accesses (member access used as an assignment lvalue) > 90%
#   - the body contains no control-flow statement (if/for/while/switch/match)
#   - computational read accesses of that receiver < 3
REFACTOR_MAX_WRITE_RATIO = 0.9
REFACTOR_MIN_READS = 3
# Output-sink / registration-fill receivers (review round 2): accesses to the
# dominant receiver are almost exclusively "push data out" calls
# (out->printf, q.push_back, printer->Print, AddBuiltinCommand, add_url_rule)
# instead of reads that drive decisions.  Excluded only when ALL hold:
#   - sink-style call share of the receiver's accesses > 85%
#   - non-sink, non-write accesses of that receiver < 3
#   - the body has no control flow
SINK_MAX_CALL_RATIO = 0.85
SINK_MIN_READS = 3
_SINK_MEMBER_NAMES = frozenset({
    "printf", "fprintf", "sprintf", "snprintf", "vprintf", "print", "println",
    "push_back", "push", "append", "insert", "put", "puts", "putchar",
    "addoption", "addinivalue_line", "add_url_rule", "addbuiltincommand",
})
# Template wrapper simple names carry no information about the wrapped type,
# so envied_type falls back to envied_receiver for them (review round 4:
# envied_type=optional for a cm::optional<TestPreset>& receiver).
_NONINFORMATIVE_WRAPPER_TYPES = frozenset({
    "optional", "unique_ptr", "shared_ptr", "weak_ptr", "vector", "deque",
    "list", "map", "set", "unordered_map", "unordered_set", "pair", "span",
    "string_view", "reference_wrapper", "variant", "expected", "function",
})
_ACCESS_NODE_TYPES = {
    "python": {"attribute"},
    "c": {"field_expression"},
    "cpp": {"field_expression"},
}
# COMPLEXITY_NODE_TYPES has no python match_statement; the rule calls for it.
_CONTROL_FLOW_EXTRA = {"python": {"match_statement"}}
_ACCESS_CHAIN_TYPES = {"attribute", "field_expression", "parenthesized_expression"}

# Calibration anchors from the human diff review: the first must be excluded,
# the other three must survive the filter.
CALIBRATION_TARGETS = [
    ("cpp", "duckdb", "src/include/duckdb/main/capi/extension_api.hpp", "CreateAPIv1", 687, "exclude"),
    ("c", "cJSON", "cJSON.c", "ensure", 485, "keep"),
    ("c", "git", "builtin/rebase.c", "cmd_rebase", None, "keep"),
    ("cpp", "CMake", "Source/cmVersion_Dependencies.cxx", "CollectDependencyInfo", 29, "keep"),
]


def _is_write_access(node) -> bool:
    """True when this member access is (part of) the lvalue of an assignment."""
    current = node
    parent = current.parent
    while parent is not None and parent.type in _ACCESS_CHAIN_TYPES:
        current, parent = parent, parent.parent
    if parent is None:
        return False
    if parent.type in {"assignment", "augmented_assignment", "assignment_expression"}:
        left = parent.child_by_field_name("left")
        return left is not None and left == current
    # `x.field++` / `x.field--` (c/cpp update_expression).
    return parent.type == "update_expression"


def _is_sink_member_name(name: str) -> bool:
    raw = name.strip()
    lowered = raw.lower()
    if lowered in _SINK_MEMBER_NAMES:
        return True
    if lowered.startswith(("write", "register", "emit")):
        return True
    # addXxx / add_xxx registration-style calls, but not bare `add` or `address`.
    return lowered.startswith("add") and len(raw) > 3 and (raw[3].isupper() or raw[3] == "_")


def _is_call_target(node) -> bool:
    parent = node.parent
    if parent is None or parent.type not in {"call", "call_expression"}:
        return False
    function_node = parent.child_by_field_name("function")
    return function_node is not None and function_node == node


def _sink_fill_verdict(language, body_node, source_bytes, receiver) -> dict:
    """Output-sink / registration-fill metrics for one receiver."""
    node_types = _ACCESS_NODE_TYPES.get(language, set())
    total = 0
    sink_calls = 0
    reads = 0
    for node in _iter_nodes(body_node):
        if node.type not in node_types:
            continue
        receiver_node = node.child_by_field_name("object") or node.child_by_field_name("argument")
        if _root_receiver_identifier(receiver_node, source_bytes) != receiver:
            continue
        total += 1
        if _is_write_access(node):
            continue
        member_node = node.child_by_field_name("attribute") or node.child_by_field_name("field")
        member = _node_text(source_bytes, member_node).strip()
        if _is_call_target(node) and _is_sink_member_name(member):
            sink_calls += 1
        else:
            reads += 1
    ratio = sink_calls / total if total else 0.0
    excluded = (
        ratio > SINK_MAX_CALL_RATIO
        and reads < SINK_MIN_READS
        and not _has_control_flow(body_node, language)
    )
    return {"total": total, "sink_calls": sink_calls, "reads": reads, "sink_ratio": ratio, "excluded": excluded}


def _receiver_read_write_counts(body_node, source_bytes, language) -> tuple[Counter, Counter]:
    """(reads, writes) per root receiver identifier inside one function body."""
    node_types = _ACCESS_NODE_TYPES.get(language, set())
    reads: Counter = Counter()
    writes: Counter = Counter()
    for node in _iter_nodes(body_node):
        if node.type not in node_types:
            continue
        receiver_node = node.child_by_field_name("object") or node.child_by_field_name("argument")
        receiver = _root_receiver_identifier(receiver_node, source_bytes)
        if not receiver:
            continue
        if _is_write_access(node):
            writes[receiver] += 1
        else:
            reads[receiver] += 1
    return reads, writes


def _has_control_flow(body_node, language) -> bool:
    control_types = COMPLEXITY_NODE_TYPES.get(language, set()) | _CONTROL_FLOW_EXTRA.get(language, set())
    return any(node.type in control_types for node in _iter_nodes(body_node))


def _refactorability_verdict(language, body_node, source_bytes, receiver) -> dict:
    reads, writes = _receiver_read_write_counts(body_node, source_bytes, language)
    receiver_reads = reads.get(receiver, 0)
    receiver_writes = writes.get(receiver, 0)
    total = receiver_reads + receiver_writes
    write_ratio = receiver_writes / total if total else 0.0
    has_control_flow = _has_control_flow(body_node, language)
    excluded = (
        write_ratio > REFACTOR_MAX_WRITE_RATIO
        and not has_control_flow
        and receiver_reads < REFACTOR_MIN_READS
    )
    return {
        "receiver": receiver,
        "reads": receiver_reads,
        "writes": receiver_writes,
        "write_ratio": write_ratio,
        "has_control_flow": has_control_flow,
        "excluded": excluded,
    }


def calibrate_refactorability_filter(project_roots: dict[str, Path]) -> bool:
    """Print the verdict for each review anchor; False when any mismatches."""
    all_ok = True
    for language, project, rel, function_name, line, expected in CALIBRATION_TARGETS:
        if language not in project_roots:
            continue
        root = project_roots[language] / project
        try:
            function_nodes = parse_function_nodes(root / rel, language)
        except Exception as exc:
            print(f"[calibrate] {language}/{project} {function_name}: parse failed: {exc}")
            all_ok = False
            continue
        verdict = None
        for node, source_bytes in function_nodes:
            if _extract_declared_name(node, language, source_bytes) != function_name:
                continue
            if line is not None and node.start_point.row + 1 != line:
                continue
            body_node = node.child_by_field_name("body")
            if body_node is None:
                continue
            reads, writes = _receiver_read_write_counts(body_node, source_bytes, language)
            local_receivers = _LOCAL_RECEIVERS.get(language, set())
            totals = {
                receiver: reads.get(receiver, 0) + writes.get(receiver, 0)
                for receiver in set(reads) | set(writes)
                if receiver not in local_receivers
            }
            dominant, dominant_count = "", 0
            for receiver, count in sorted(totals.items()):
                if count > dominant_count:
                    dominant, dominant_count = receiver, count
            verdict = _refactorability_verdict(language, body_node, source_bytes, dominant)
            verdict["function_line"] = node.start_point.row + 1
            break
        if verdict is None:
            print(f"[calibrate] {language}/{project} {function_name}: function not found")
            all_ok = False
            continue
        actual = "exclude" if verdict["excluded"] else "keep"
        ok = actual == expected
        all_ok = all_ok and ok
        print(
            f"[calibrate] {'PASS' if ok else 'FAIL'} {language}/{project} "
            f"{function_name} (line {verdict['function_line']}): verdict={actual} "
            f"(expected {expected}); dominant={verdict['receiver']} "
            f"writes={verdict['writes']} reads={verdict['reads']} "
            f"write_ratio={verdict['write_ratio']:.2f} "
            f"control_flow={verdict['has_control_flow']}"
        )
    return all_ok


def _envy_candidate(language, project, root, rel, name, snippet, function_node, body_node, source_bytes, stats):
    if snippet.end_line - snippet.start_line > MAX_FUNCTION_SPAN_LINES:
        # Backstop against misparsed mega-spans (e.g. a whole namespace wrapped
        # into one bogus function_definition: protobuf demangle.cc spanned
        # 27..1986 with has_error=True).
        stats["span_cap_skipped"] += 1
        return None
    signature_flat = re.sub(r"\s+", " ", snippet.signature_text)
    if function_node.has_error and f"{name}(" not in signature_flat:
        # ERROR nodes are common in macro-heavy C bodies of perfectly real
        # functions (cmd_rebase among them), so has_error alone is not enough;
        # but when the supposed function name is not even followed by `(` in
        # the signature text, the node is not a real function at all.
        stats["parse_error_skipped"] += 1
        return None
    if (language, project, str(rel), snippet.start_line) in REVIEW_EXCLUSIONS:
        stats["review_excluded"] += 1
        return None
    loc = count_meaningful_lines(snippet.body_text, language)
    if loc < FEATURE_ENVY_MIN_LOC:
        return None
    accesses = iter_member_accesses(body_node, source_bytes, language)
    local_receivers = _LOCAL_RECEIVERS.get(language, set())
    foreign: dict[str, int] = {}
    for access in accesses:
        if not access.receiver or access.receiver in local_receivers:
            continue
        foreign[access.receiver] = foreign.get(access.receiver, 0) + 1
    if not foreign:
        return None
    # Same dominant-receiver tie-break as feature_envy.analyze_feature_envy_target.
    dominant_receiver, dominant_count = "", 0
    for receiver, count in sorted(foreign.items()):
        if count > dominant_count:
            dominant_receiver, dominant_count = receiver, count
    total = len(accesses)
    ratio = dominant_count / total if total else 0.0
    if dominant_count < FEATURE_ENVY_MIN_FOREIGN_ACCESS or ratio < FEATURE_ENVY_FOREIGN_RATIO:
        return None
    fingerprints = _parameter_fingerprints_from_node(function_node, language, source_bytes)
    param_names = {fp.rsplit(":", 1)[-1].strip() for fp in fingerprints}
    local_names = {n for n, _ in iter_local_variable_names(body_node, source_bytes, language)}
    if dominant_receiver not in param_names and dominant_receiver not in local_names:
        # Module-level receivers (imported modules, globals) are not feature envy.
        return None
    verdict = _refactorability_verdict(language, body_node, source_bytes, dominant_receiver)
    if verdict["excluded"]:
        # Pure field-copy factory: nothing movable, only invites metric gaming.
        stats["refactorability_excluded"] += 1
        return None
    sink_verdict = _sink_fill_verdict(language, body_node, source_bytes, dominant_receiver)
    if sink_verdict["excluded"]:
        # Output-sink / registration-fill receiver: accesses only push data out.
        stats["sink_fill_excluded"] += 1
        return None
    evidence_tail = f"foreign_access={dominant_count}; foreign_ratio={ratio:.2f}"
    if language == "python":
        evidence = f"envied_receiver={dominant_receiver}; {evidence_tail}"
    else:
        type_map = _parameter_type_map(snippet.signature_text, language)
        simple_type = _simple_type_name(type_map.get(dominant_receiver, ""))
        # Macro-prefixed signatures can make the text-based type parse collapse
        # onto the function name itself; that is not a real envied type.  A
        # template wrapper simple name (optional/vector/...) is equally
        # meaningless: envied_type=optional says nothing about TestPreset.
        type_usable = (
            bool(simple_type)
            and simple_type != name
            and simple_type not in _NONINFORMATIVE_WRAPPER_TYPES
            and re.fullmatch(r"[A-Za-z_]\w*", simple_type)
        )
        if dominant_receiver in param_names and type_usable:
            evidence = f"envied_type={simple_type}; {evidence_tail}"
        else:
            evidence = f"envied_receiver={dominant_receiver}; {evidence_tail}"
    return {
        "language": language,
        "smell_type": "feature_envy",
        "project_name": project,
        "project_root": str(root),
        "file": str(rel),
        "method": _method_label(name, snippet.signature_text),
        "begin_line": snippet.start_line,
        "end_line": snippet.end_line,
        "metric_value": dominant_count,
        "evidence": evidence,
        "receiver_name": dominant_receiver,
    }


def _name_candidates(language, project, root, rel, name, snippet, function_node, body_node, source_bytes):
    findings = []

    def add(kind, ident, line):
        reason = suspicious_name_reason(ident)
        if reason:
            findings.append({
                "language": language,
                "smell_type": "mysterious_name",
                "project_name": project,
                "project_root": str(root),
                "file": str(rel),
                "method": _method_label(name, snippet.signature_text),
                "begin_line": snippet.start_line,
                "end_line": snippet.end_line,
                "metric_value": len(ident),
                "evidence": f"kind={kind}; name={ident}; reason={reason}; len={len(ident)}",
                "finding_kind": kind,
                "finding_name": ident,
                "finding_line": line,
            })

    if name:
        add("method", name, snippet.start_line)
    for fingerprint in _parameter_fingerprints_from_node(function_node, language, source_bytes):
        param_name = fingerprint.rsplit(":", 1)[-1].strip()
        if param_name:
            add("param", param_name, snippet.start_line)
    for local_name, local_line in iter_local_variable_names(body_node, source_bytes, language):
        if local_name:
            add("local", local_name, local_line)
    return findings


def scan_project(language: str, project: str, root: Path) -> dict[str, list[dict]]:
    envy: list[dict] = []
    names: list[dict] = []
    stats: Counter = Counter()
    seen_names: set[tuple[str, str, str, int]] = set()
    extensions = LANGUAGE_EXTENSIONS[language]
    for path in _iter_source_files(root, extensions):
        rel = path.relative_to(root)
        if _skip_path(rel):
            continue
        try:
            function_nodes = parse_function_nodes(path, language)
        except Exception:
            continue
        for node, source_bytes in function_nodes:
            body_node = node.child_by_field_name("body")
            if body_node is None:
                continue
            snippet = _build_source_snippet(node, source_bytes, language)
            if snippet is None:
                continue
            name = _extract_declared_name(node, language, source_bytes) or ""
            candidate = _envy_candidate(
                language, project, root, rel, name, snippet, node, body_node, source_bytes, stats
            )
            if candidate:
                envy.append(candidate)
            for finding in _name_candidates(
                language, project, root, rel, name, snippet, node, body_node, source_bytes
            ):
                key = (finding["file"], finding["finding_kind"], finding["finding_name"], finding["finding_line"])
                if key not in seen_names:
                    seen_names.add(key)
                    names.append(finding)
    filtered = {key: stats[key] for key in ("refactorability_excluded", "sink_fill_excluded", "parse_error_skipped", "span_cap_skipped", "review_excluded") if stats[key]}
    if filtered:
        print(f"[scan] {language}/{project}: filters skipped {filtered}", flush=True)
    return {"feature_envy": envy, "mysterious_name": names}


def _candidate_sort_key(smell: str, candidate: dict):
    if smell == "feature_envy":
        return (-candidate["metric_value"], candidate["file"], candidate["begin_line"])
    # Prefer informative names; single-underscore placeholders go last.
    return (
        candidate["finding_name"] == "_",
        candidate["metric_value"],
        candidate["file"],
        candidate["begin_line"],
    )


def _round_robin_order(candidates: list[dict], smell: str) -> list[dict]:
    by_project: dict[str, list[dict]] = defaultdict(list)
    for candidate in candidates:
        by_project[candidate["project_name"]].append(candidate)
    pools = {
        project: deque(sorted(items, key=lambda item: _candidate_sort_key(smell, item)))
        for project, items in by_project.items()
    }
    ordered: list[dict] = []
    while pools:
        for project in sorted(list(pools)):
            pool = pools[project]
            if pool:
                ordered.append(pool.popleft())
            if not pool:
                del pools[project]
    return ordered


def curate(candidates: list[dict], smell: str) -> tuple[list[dict], list[dict]]:
    """Pick up to SAMPLE_COUNT rows (<=MAX_PER_PROJECT each, one per function)."""
    ordered = _round_robin_order(candidates, smell)
    selected: list[dict] = []
    backups: list[dict] = []
    counts: Counter = Counter()
    used_functions: set[tuple[str, str, int]] = set()
    for candidate in ordered:
        key = (candidate["project_name"], candidate["file"], candidate["begin_line"])
        if len(selected) < SAMPLE_COUNT and counts[candidate["project_name"]] < MAX_PER_PROJECT and key not in used_functions:
            selected.append(candidate)
            counts[candidate["project_name"]] += 1
            used_functions.add(key)
        else:
            backups.append(candidate)
    return selected, backups


def validate_envy(candidate: dict) -> bool:
    root = Path(candidate["project_root"])
    target_file = root / candidate["file"]
    if not target_file.is_file():
        return False
    receiver = str(candidate.get("receiver_name") or "").strip()
    if not receiver:
        # Old scan caches predate target_context materialization. Recompute the
        # root from this one already-selected declaration, never from evidence.
        current = analyze_feature_envy_target(
            root,
            language=candidate["language"],
            target_file=target_file,
            method=candidate["method"],
            line=candidate["begin_line"],
        )
        receiver = str(current.get("dominant_receiver_type") or "").strip()
        if not receiver:
            return False
        candidate["receiver_name"] = receiver
    profile = analyze_feature_envy_target(
        root,
        language=candidate["language"],
        target_file=target_file,
        method=candidate["method"],
        line=candidate["begin_line"],
        expected_receiver=receiver,
        exact_receiver_selector=True,
    )
    return bool(
        profile.get("ok")
        and profile.get("strict_detector_hit")
        and int(profile.get("expected_receiver_access") or 0) > 0
        and profile.get("begin_line") == candidate["begin_line"]
    )


def validate_name(candidate: dict, findings_cache: dict[str, list]) -> bool:
    root = Path(candidate["project_root"])
    target_file = root / candidate["file"]
    if not target_file.is_file():
        return False
    cache_key = str(target_file)
    if cache_key not in findings_cache:
        findings_cache[cache_key] = detect_mysterious_names(target_file, language=candidate["language"])
    match = next(
        (
            finding
            for finding in findings_cache[cache_key]
            if finding.kind == candidate["finding_kind"]
            and finding.name == candidate["finding_name"]
            and finding.line == candidate["finding_line"]
        ),
        None,
    )
    if match is None:
        return False
    try:
        nodes = parse_function_nodes(target_file, candidate["language"])
    except Exception:
        return False
    containing = [
        node for node, _ in nodes
        if node.start_point.row + 1 <= match.line <= node.end_point.row + 1
    ]
    if not containing:
        return False
    smallest = min(containing, key=lambda node: node.end_point.row - node.start_point.row)
    return smallest.start_point.row + 1 == candidate["begin_line"]


def select_keep_existing(
    language: str, smell: str, candidates: list[dict], existing_csv: Path
) -> tuple[list[dict], list[dict], list[str]]:
    """Keep reviewed-suitable rows from the existing CSV, then fill to SAMPLE_COUNT.

    A row survives only when it is still produced by the current candidate
    pipeline (so every active filter applies to it) and still passes detector
    validation.  Returns (rows, added, drop_reasons).
    """
    pool = {
        (candidate["project_name"], candidate["file"], candidate["begin_line"]): candidate
        for candidate in candidates
    }
    kept: list[dict] = []
    counts: Counter = Counter()
    used: set[tuple[str, str, int]] = set()
    drops: list[str] = []
    if existing_csv.is_file():
        with existing_csv.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = (row["project_name"], row["file"], int(row["begin_line"]))
                label = f"{language}#{row['sample_id']} {row['project_name']} {row['file']}:{row['begin_line']}"
                candidate = pool.get(key)
                if candidate is None:
                    drops.append(f"{label}: no longer a candidate (filtered)")
                    continue
                if counts[candidate["project_name"]] >= MAX_PER_PROJECT:
                    drops.append(f"{label}: per-project cap")
                    continue
                if not validate_envy(candidate):
                    drops.append(f"{label}: detector revalidation failed")
                    continue
                kept.append(candidate)
                counts[candidate["project_name"]] += 1
                used.add(key)
    added: list[dict] = []
    ordered = _round_robin_order([c for c in candidates if (c["project_name"], c["file"], c["begin_line"]) not in used], smell)
    for candidate in ordered:
        if len(kept) + len(added) >= SAMPLE_COUNT:
            break
        key = (candidate["project_name"], candidate["file"], candidate["begin_line"])
        if key in used or counts[candidate["project_name"]] >= MAX_PER_PROJECT:
            continue
        if not validate_envy(candidate):
            continue
        added.append(candidate)
        counts[candidate["project_name"]] += 1
        used.add(key)
    return kept + added, added, drops


def select_valid(candidates: list[dict], backups: list[dict], smell: str) -> tuple[list[dict], int]:
    validator = validate_envy if smell == "feature_envy" else None
    findings_cache: dict[str, list] = {}
    selected: list[dict] = []
    replaced = 0
    counts: Counter = Counter(candidate["project_name"] for candidate in candidates)
    used = {(c["project_name"], c["file"], c["begin_line"]) for c in candidates}

    def is_valid(candidate: dict) -> bool:
        if validator is not None:
            return validator(candidate)
        return validate_name(candidate, findings_cache)

    for candidate in candidates:
        if is_valid(candidate):
            selected.append(candidate)
        else:
            replaced += 1
            counts[candidate["project_name"]] -= 1
            used.discard((candidate["project_name"], candidate["file"], candidate["begin_line"]))
    while len(selected) < SAMPLE_COUNT and backups:
        candidate = backups.pop(0)
        key = (candidate["project_name"], candidate["file"], candidate["begin_line"])
        if key in used or counts[candidate["project_name"]] >= MAX_PER_PROJECT:
            continue
        if not is_valid(candidate):
            continue
        selected.append(candidate)
        counts[candidate["project_name"]] += 1
        used.add(key)
    return selected, replaced


def write_csv(path: Path, language: str, smell: str, rows: list[dict]) -> None:
    """Write rows in container (image) path format; candidates stay local."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FIELDNAMES,
            lineterminator="\n",
        )
        writer.writeheader()
        for index, candidate in enumerate(rows, start=1):
            container_root = _container_project_root(language, candidate["project_name"])
            container_file = f"{container_root}/{candidate['file']}"
            if smell == "feature_envy":
                target_context = {
                    "receiver_type": str(candidate["receiver_name"]),
                }
            else:
                # Preserve the established Mysterious Name selector schema;
                # this builder does not add or reinterpret its contract.
                target_context = {
                    "symbol_kind": str(candidate["finding_kind"]),
                    "symbol_name": str(candidate["finding_name"]),
                }
            writer.writerow({
                "sample_id": index,
                "language": language,
                "smell_type": smell,
                "project_name": candidate["project_name"],
                "project_path": container_root,
                "file": candidate["file"],
                "method": candidate["method"],
                "begin_line": candidate["begin_line"],
                "end_line": candidate["end_line"],
                "metric_value": candidate["metric_value"],
                "location": f"{container_file}:method={candidate['method']}|line={candidate['begin_line']}",
                "is_test": 0,
                "evidence": candidate["evidence"],
                "target_context_json": json.dumps(
                    target_context,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
            })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--languages", nargs="*", default=list(PROJECTS))
    parser.add_argument("--smells", nargs="*", default=list(SMELLS))
    parser.add_argument(
        "--project-root",
        action="append",
        default=[],
        metavar="LANG=PATH",
        help=(
            "Local directory containing the pinned project checkouts for one "
            "language; repeat for every selected language."
        ),
    )
    parser.add_argument("--cache", type=Path, default=CACHE_DIR / "candidates.json")
    parser.add_argument("--rescan", action="store_true", help="ignore the candidate cache")
    parser.add_argument("--no-write", action="store_true", help="curate/validate only, do not write CSVs")
    args = parser.parse_args()

    unknown_languages = sorted(set(args.languages) - set(PROJECTS))
    if unknown_languages:
        parser.error("unsupported language(s): " + ", ".join(unknown_languages))
    unknown_smells = sorted(set(args.smells) - set(SMELLS))
    if unknown_smells:
        parser.error("unsupported smell(s): " + ", ".join(unknown_smells))
    project_roots = _parse_project_roots(parser, args.project_root, args.languages)

    if "feature_envy" in args.smells and not calibrate_refactorability_filter(project_roots):
        print("refactorability filter calibration FAILED; aborting", flush=True)
        return 1

    cache: dict[str, dict[str, dict[str, list[dict]]]] = {}
    if args.cache.is_file() and not args.rescan:
        cache = json.loads(args.cache.read_text(encoding="utf-8"))
        _rebase_cached_project_roots(cache, project_roots)

    summary: dict[str, dict] = {}
    for language in args.languages:
        cache.setdefault(language, {})
        for project in PROJECTS[language]:
            root = _local_project_root(project_roots, language, project)
            if project in cache[language]:
                continue
            if not root.is_dir():
                raise FileNotFoundError(f"missing project checkout: {root}")
            print(f"[scan] {language}/{project} ...", flush=True)
            cache[language][project] = scan_project(language, project, root)
            args.cache.parent.mkdir(parents=True, exist_ok=True)
            args.cache.write_text(json.dumps(cache), encoding="utf-8")
            print(
                f"[scan] {language}/{project}: "
                f"feature_envy={len(cache[language][project]['feature_envy'])} "
                f"mysterious_name={len(cache[language][project]['mysterious_name'])}",
                flush=True,
            )

    for language in args.languages:
        for smell in args.smells:
            candidates = [
                candidate
                for project in PROJECTS[language]
                for candidate in cache[language][project][smell]
            ]
            if smell == "feature_envy":
                existing_csv = args.out_root / language / f"{smell}_30.csv"
                rows, added, drops = select_keep_existing(language, smell, candidates, existing_csv)
                distribution = Counter(row["project_name"] for row in rows)
                summary[f"{language}/{smell}"] = {
                    "candidates": len(candidates),
                    "kept_existing": len(rows) - len(added),
                    "added": len(added),
                    "valid": len(rows),
                    "dropped_existing": drops,
                    "distribution": dict(sorted(distribution.items())),
                }
                print(
                    f"[curate] {language}/{smell}: candidates={len(candidates)} "
                    f"kept={len(rows) - len(added)} added={len(added)} "
                    f"dropped={len(drops)} valid={len(rows)} projects={len(distribution)}",
                    flush=True,
                )
                for drop in drops:
                    print(f"[drop] {drop}", flush=True)
                for candidate in added:
                    print(
                        f"[add] {language} {candidate['project_name']} "
                        f"{candidate['file']}:{candidate['begin_line']}-{candidate['end_line']} "
                        f"{candidate['method']} | {candidate['evidence']}",
                        flush=True,
                    )
            else:
                picked, backups = curate(candidates, smell)
                rows, replaced = select_valid(picked, backups, smell)
                distribution = Counter(row["project_name"] for row in rows)
                summary[f"{language}/{smell}"] = {
                    "candidates": len(candidates),
                    "curated": len(picked),
                    "valid": len(rows),
                    "replaced_invalid": replaced,
                    "distribution": dict(sorted(distribution.items())),
                }
                print(
                    f"[curate] {language}/{smell}: candidates={len(candidates)} "
                    f"curated={len(picked)} valid={len(rows)} replaced={replaced} "
                    f"projects={len(distribution)}",
                    flush=True,
                )
            if len(rows) == SAMPLE_COUNT and not args.no_write:
                out = args.out_root / language / f"{smell}_30.csv"
                write_csv(out, language, smell, rows)
                print(f"[write] {out}", flush=True)
            elif len(rows) != SAMPLE_COUNT:
                print(
                    f"[warn] {language}/{smell}: only {len(rows)} valid samples "
                    f"(target {SAMPLE_COUNT}); CSV not written",
                    flush=True,
                )

    args.cache.parent.mkdir(parents=True, exist_ok=True)
    (args.cache.parent / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    shortfalls = [key for key, value in summary.items() if value["valid"] < SAMPLE_COUNT]
    if shortfalls:
        print(f"SHORTFALL: {shortfalls}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
