#!/usr/bin/env python3
"""Run RRDtool's upstream Automake suite and emit fresh JUnit evidence."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path


RESULT_LINE = re.compile(r"^(PASS|SKIP|XFAIL|FAIL|ERROR):\s+(.+?)\s*$")
EXCLUDED = {
    "create-with-source-4": "requires the external dc calculator absent from the fixed delivery image",
}


def _libdbi_build_contract(build: Path) -> str:
    header = build / "src" / "rrd_config.h"
    makefile = build / "src" / "Makefile"
    compiled = build / "src" / ".libs" / "librrd_la-rrd_fetch_libdbi.o"
    if not header.is_file() or "#define HAVE_LIBDBI 1" not in header.read_text(
        encoding="utf-8", errors="replace"
    ):
        return "RRDtool configure did not enable libDBI"
    if not makefile.is_file() or "rrd_fetch_libdbi.c" not in makefile.read_text(
        encoding="utf-8", errors="replace"
    ):
        return "RRDtool generated Makefile omitted rrd_fetch_libdbi.c"
    if not compiled.is_file() or compiled.stat().st_size == 0:
        return "RRDtool libDBI target object was not freshly compiled"
    return ""


def _extract_libdbi_long_function(source: str) -> str:
    match = re.search(r"static\s+long\s+rrd_fetch_dbi_long\s*\(", source)
    if match is None:
        raise ValueError("rrd_fetch_dbi_long definition is missing")
    opening = source.find("{", match.start())
    if opening < 0:
        raise ValueError("rrd_fetch_dbi_long body is missing")
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
    raise ValueError("rrd_fetch_dbi_long body is unterminated")


def _libdbi_probe_translation_unit(function: str) -> str:
    return r'''#include <dbi/dbi.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define DNAN (-9999L)
#define SIZEOF_TIME_T 8

#define dbi_result_get_field_attribs_idx probe_get_field_attribs_idx
#define dbi_result_get_field_type_idx probe_get_field_type_idx
#define dbi_result_field_is_null_idx probe_field_is_null_idx
#define dbi_result_get_string_idx probe_get_string_idx
#define dbi_result_get_char_idx probe_get_char_idx
#define dbi_result_get_short_idx probe_get_short_idx
#define dbi_result_get_int_idx probe_get_int_idx
#define dbi_result_get_longlong_idx probe_get_longlong_idx
#define dbi_result_get_float_idx probe_get_float_idx
#define dbi_result_get_double_idx probe_get_double_idx
#define dbi_result_get_field_length_idx probe_get_field_length_idx
#define dbi_result_get_binary_copy_idx probe_get_binary_copy_idx
#define dbi_result_get_datetime_idx probe_get_datetime_idx

static unsigned int probe_attr;
static unsigned int probe_type;
static unsigned int probe_get_field_attribs_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return probe_attr;
}
static unsigned int probe_get_field_type_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return probe_type;
}
static int probe_field_is_null_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return 0;
}
static const char *probe_get_string_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return "17";
}
static char probe_get_char_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return 11;
}
static short probe_get_short_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return 22;
}
static int probe_get_int_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return 44;
}
static long long probe_get_longlong_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return 88;
}
static float probe_get_float_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return 4.0f;
}
static double probe_get_double_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return 8.0;
}
static size_t probe_get_field_length_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return 2;
}
static unsigned char *probe_get_binary_copy_idx(dbi_result result, int idx) {
  unsigned char *copy;
  (void)result; (void)idx;
  copy = malloc(2);
  if (copy != NULL) { copy[0] = '1'; copy[1] = '\0'; }
  return copy;
}
static time_t probe_get_datetime_idx(dbi_result result, int idx) {
  (void)result; (void)idx; return (time_t)55;
}

''' + function + r'''

int main(void) {
  const dbi_result result = (dbi_result)1;
  probe_type = DBI_TYPE_INTEGER;
  probe_attr = DBI_INTEGER_UNSIGNED | DBI_INTEGER_SIZE1;
  if (rrd_fetch_dbi_long(result, 0) != 11) { return 11; }
  probe_attr = DBI_INTEGER_UNSIGNED | DBI_INTEGER_SIZE4;
  if (rrd_fetch_dbi_long(result, 0) != 44) { return 44; }
  probe_attr = DBI_INTEGER_UNSIGNED | DBI_INTEGER_SIZE8;
  if (rrd_fetch_dbi_long(result, 0) != 88) { return 88; }
  probe_type = DBI_TYPE_DECIMAL;
  probe_attr = DBI_DECIMAL_UNSIGNED | DBI_DECIMAL_SIZE4;
  if (rrd_fetch_dbi_long(result, 0) != 4) { return 104; }
  probe_attr = DBI_DECIMAL_UNSIGNED | DBI_DECIMAL_SIZE8;
  if (rrd_fetch_dbi_long(result, 0) != 8) { return 108; }
  return 0;
}
'''


def _run_libdbi_attribute_probe(
    build: Path, *, include_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    source_path = build / "src" / "rrd_fetch_libdbi.c"
    if not source_path.is_file():
        return subprocess.CompletedProcess(
            args=["libdbi-attribute-probe"],
            returncode=2,
            stdout="",
            stderr="rrd_fetch_libdbi.c is missing",
        )
    try:
        function = _extract_libdbi_long_function(
            source_path.read_text(encoding="utf-8", errors="strict")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return subprocess.CompletedProcess(
            args=["libdbi-attribute-probe"],
            returncode=2,
            stdout="",
            stderr=str(exc),
        )
    with tempfile.TemporaryDirectory(prefix="rrdtool-libdbi-probe-") as raw:
        temporary = Path(raw)
        probe_source = temporary / "probe.c"
        probe_binary = temporary / "probe"
        probe_source.write_text(
            _libdbi_probe_translation_unit(function), encoding="utf-8"
        )
        command = [*shlex.split(os.environ.get("CC", "cc")), "-std=c11"]
        if include_dir is not None:
            command.extend(["-I", str(include_dir)])
        command.extend([str(probe_source), "-lm", "-o", str(probe_binary)])
        compiled = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if compiled.returncode != 0:
            return compiled
        return subprocess.run(
            [str(probe_binary)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )


def _normalize_test_name(name: str) -> str:
    return name.removesuffix("$(EXEEXT)").removesuffix(".exe")


def _declared_tests(build: Path) -> tuple[str, ...]:
    tests_dir = build / "tests"
    if not (tests_dir / "Makefile").is_file():
        raise SystemExit("RRDtool configured tests Makefile is missing")
    completed = subprocess.run(
        ["make", "--no-print-directory", "-C", str(tests_dir), "-pn"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "RRDtool Automake TESTS query failed: " + completed.stderr[-4000:]
        )
    matches = re.findall(r"^TESTS\s*=\s*(.+)$", completed.stdout, flags=re.MULTILINE)
    if len(matches) != 1:
        raise SystemExit(f"expected one RRDtool Automake TESTS declaration, got {len(matches)}")
    tests = tuple(_normalize_test_name(item) for item in shlex.split(matches[0]))
    if not tests or len(tests) != len(set(tests)):
        raise SystemExit("RRDtool Automake TESTS declaration is empty or ambiguous")
    unresolved = [item for item in tests if "$" in item or "(" in item or ")" in item]
    if unresolved:
        raise SystemExit(f"RRDtool Automake TESTS contains unresolved entries: {unresolved}")
    missing_exclusions = sorted(set(EXCLUDED) - set(tests))
    if missing_exclusions:
        raise SystemExit(
            f"RRDtool explicit exclusions are absent from this revision: {missing_exclusions}"
        )
    return tests


def _libdbi_probe_errors(build: Path) -> tuple[str, str]:
    build_error = _libdbi_build_contract(build)
    probe = _run_libdbi_attribute_probe(build)
    probe_error = ""
    if probe.returncode != 0:
        probe_error = (
            (probe.stdout or "") + (probe.stderr or "")
        ).strip() or f"libDBI attribute probe exited {probe.returncode}"
    return build_error, probe_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--build-dir", default=".")
    parser.add_argument("--focused-libdbi-probe", action="store_true")
    args = parser.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    build = (root / args.build_dir).resolve()
    if not (build / "Makefile").is_file():
        raise SystemExit("RRDtool configured build directory is missing")
    if args.focused_libdbi_probe:
        build_error, probe_error = _libdbi_probe_errors(build)
        if build_error:
            print(f"FAIL: libdbi-object-built: {build_error}")
        if probe_error:
            print(f"FAIL: libdbi-unsigned-size-attributes: {probe_error}")
        if build_error or probe_error:
            return 1
        print("rrdtool focused libDBI probe: PASS")
        return 0

    tests = _declared_tests(build)
    report = root / ".smell-test-reports" / "TEST-rrdtool-make-check.xml"
    report.unlink(missing_ok=True)

    started = time.monotonic()
    completed = subprocess.run(
        ["make", "check", f"TESTS={' '.join(name for name in tests if name not in EXCLUDED)}"],
        cwd=build,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    duration = time.monotonic() - started
    print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")

    libdbi_build_error, libdbi_probe_error = _libdbi_probe_errors(build)
    if libdbi_build_error:
        print(f"FAIL: libdbi-object-built: {libdbi_build_error}")
    if libdbi_probe_error:
        print(f"FAIL: libdbi-unsigned-size-attributes: {libdbi_probe_error}")

    seen: set[tuple[str, str]] = set()
    outcomes: list[tuple[str, str]] = []
    for raw in completed.stdout.splitlines():
        match = RESULT_LINE.fullmatch(raw.strip())
        if not match:
            continue
        item = (match.group(1), _normalize_test_name(match.group(2)))
        if item not in seen:
            seen.add(item)
            outcomes.append(item)
    executed = [item for item in outcomes if item[0] in {"PASS", "FAIL", "ERROR"}]
    failures = [item for item in outcomes if item[0] in {"FAIL", "ERROR"}]
    skipped = [item for item in outcomes if item[0] in {"SKIP", "XFAIL"}]
    for name, reason in EXCLUDED.items():
        outcomes.append(("SKIP", name))
        skipped.append(("SKIP", name))
    outcomes.extend(
        [
            ("FAIL" if libdbi_build_error else "PASS", "libdbi-object-built"),
            (
                "FAIL" if libdbi_probe_error else "PASS",
                "libdbi-unsigned-size-attributes",
            ),
        ]
    )
    if libdbi_build_error:
        failures.append(("FAIL", "libdbi-object-built"))
    if libdbi_probe_error:
        failures.append(("FAIL", "libdbi-unsigned-size-attributes"))
    failure_details = {
        "libdbi-object-built": libdbi_build_error,
        "libdbi-unsigned-size-attributes": libdbi_probe_error,
    }

    suite = ET.Element(
        "testsuite",
        {
            "name": "rrdtool-make-check",
            "tests": str(len(outcomes)),
            "failures": str(len(failures)),
            "errors": "0",
            "skipped": str(len(skipped)),
            "time": f"{duration:.3f}",
        },
    )
    for status, name in outcomes:
        case = ET.SubElement(
            suite,
            "testcase",
            {"classname": "rrdtool.tests", "name": name},
        )
        if status in {"SKIP", "XFAIL"}:
            ET.SubElement(case, "skipped", {"message": EXCLUDED.get(name, status)})
        elif status in {"FAIL", "ERROR"}:
            failure = ET.SubElement(case, "failure", {"message": status})
            failure.text = failure_details.get(name) or completed.stdout[-16000:]
    report.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(report, encoding="utf-8", xml_declaration=True)

    valid = (
        completed.returncode == 0
        and bool(executed)
        and not failures
        and any(status == "PASS" for status, _ in outcomes)
        and {name for status, name in outcomes if status == "PASS" and name in tests}
        == {name for name in tests if name not in EXCLUDED}
        and not libdbi_build_error
        and not libdbi_probe_error
    )
    if not valid:
        print(
            "RRDtool suite did not produce a clean non-zero Automake result: "
            f"rc={completed.returncode} outcomes={len(outcomes)} failures={len(failures)}"
        )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
