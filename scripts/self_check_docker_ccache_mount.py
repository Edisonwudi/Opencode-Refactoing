#!/usr/bin/env python3
"""Verify Docker samples share only a scoped ccache named volume."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_nonjava_verification_replay_matrix import (  # noqa: E402
    CCACHE_MOUNT_TARGET,
    _ccache_volume_name,
    _compiler_cache_manifest,
    _docker_runtime_args,
)


def _option_value(args: list[str], option: str) -> str:
    index = args.index(option)
    return args[index + 1]


def main() -> int:
    image = "registry.example/refactor/cpp-env:0.1.1"
    root = "/opt/projects/cpp/protobuf-29.3"
    volume = _ccache_volume_name(image, "cpp", root)
    assert volume == (
        "smell-ccache-registry-example-refactor-cpp-env-0-1-1-"
        "cpp-protobuf-29-3"
    ), volume
    assert _ccache_volume_name(image, "cpp", root) == volume
    assert _ccache_volume_name(image + "-next", "cpp", root) != volume
    assert _ccache_volume_name(image, "c", root) != volume
    assert _ccache_volume_name(image, "cpp", "/opt/projects/cpp/yaml-cpp") != volume

    args = _docker_runtime_args(
        image=image,
        language="cpp",
        canonical_project_root=root,
        cpuset="4-5",
        memory="12g",
    )
    assert _option_value(args, "--cpuset-cpus") == "4-5", args
    assert _option_value(args, "--memory") == "12g", args
    assert _option_value(args, "--network") == "none", args

    mounts = [args[index + 1] for index, item in enumerate(args) if item == "--mount"]
    assert mounts == [
        f"type=volume,source={volume},target={CCACHE_MOUNT_TARGET}"
    ], mounts
    assert f"CCACHE_DIR={CCACHE_MOUNT_TARGET}" in args, args
    assert "CCACHE_UMASK=000" in args, args
    joined = "\n".join(args)
    for forbidden in (
        "build-refactoragent",
        ".smell-test-reports",
        "/runs",
        "verify.json",
        "result.json",
        "PASS",
    ):
        assert forbidden not in joined, (forbidden, args)

    python_args = _docker_runtime_args(
        image="registry.example/refactor/python-env:0.1.1",
        language="python",
        canonical_project_root="/opt/projects/python/tornado",
        cpuset="6-7",
        memory="8g",
    )
    assert "--mount" not in python_args, python_args
    assert not any("CCACHE_DIR=" in item for item in python_args), python_args
    assert _option_value(python_args, "--cpuset-cpus") == "6-7", python_args

    audit = _compiler_cache_manifest(
        image,
        [
            {"language": "cpp", "canonical_project_root": root},
            {"language": "cpp", "canonical_project_root": root},
            {
                "language": "cpp",
                "canonical_project_root": "/opt/projects/cpp/yaml-cpp",
            },
            {
                "language": "python",
                "canonical_project_root": "/opt/projects/python/tornado",
            },
        ],
    )
    assert audit == {
        "scope": "ccache_objects_only",
        "mount_target": CCACHE_MOUNT_TARGET,
        "test_results_shared": False,
        "acceptance_shared": False,
        "volumes": sorted(
            {
                volume,
                _ccache_volume_name(
                    image, "cpp", "/opt/projects/cpp/yaml-cpp"
                ),
            }
        ),
    }, audit

    overlay = (
        ROOT
        / "runtime"
        / "python"
        / "smell_core"
        / "defaults"
        / "projects.runtime-overrides.yaml"
    ).read_text(encoding="utf-8")
    inherited_dir = (
        'export CCACHE_DIR="${CCACHE_DIR:-/tmp/refactoragent-ccache}"'
    )
    assert "export CCACHE_DIR=/tmp/refactoragent-ccache" not in overlay
    assert overlay.count(inherited_dir) == 8, overlay.count(inherited_dir)

    entrypoint = (
        ROOT / "docker" / "java-refactor-delivery" / "entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert (
        "CCACHE_DIR must use /var/cache/refactoragent/ccache"
        in entrypoint
    )
    assert 'chown "$RUN_AS_USER:$RUN_AS_USER" "$cache_dir"' in entrypoint
    assert 'chown -R "$RUN_AS_USER:$RUN_AS_USER" "$cache_dir"' not in entrypoint
    assert 'runuser -u "$RUN_AS_USER" -- test -w "$cache_dir"' in entrypoint
    assert "export CCACHE_UMASK=000" in entrypoint
    assert entrypoint.count("prepare_compiler_cache_for_run_user") >= 3

    print(
        "docker ccache mount self-check: PASS scope=image/language/project "
        "shared=compiler-objects-only cpuset=preserved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
