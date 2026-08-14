#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TORNADO_PROBE = (
    ROOT / "scripts" / "verification_probes" / "tornado_http1_continue_ownership.py"
)
REDIS_PROBE = (
    ROOT / "scripts" / "verification_probes" / "redis_missing_geo_source_contract.py"
)


TORNADO_MODULE = '''from types import SimpleNamespace

class HTTP1Connection:
    def __init__(self, stream, is_client):
        self.stream = stream

    async def read_response(self, delegate):
        await self.stream.read_until_regex(b"headers")
        delegate.headers_received(SimpleNamespace(code=204), {{}})
        delegate.finish()
        {extra_callback}
        return True
'''


FAKE_REDIS_SERVER = r'''#!/usr/bin/env python3
import os
import socket
import sys

args = sys.argv[1:]
socket_path = args[args.index("--unixsocket") + 1]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(socket_path)
server.listen(1)
connection, _ = server.accept()
connection.recv(4096)
if {safe}:
    connection.sendall(b"*0\r\n")
    connection.recv(4096)
    connection.sendall(b"+PONG\r\n")
    connection.recv(1)
else:
    connection.close()
    os._exit(1)
'''


def _probe(script: Path, project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), str(project)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )


def _tornado_project(root: Path, *, extra_callback: str = "") -> Path:
    project = root
    package = project / "tornado"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "http1connection.py").write_text(
        TORNADO_MODULE.format(extra_callback=extra_callback),
        encoding="utf-8",
    )
    return project


def _redis_project(root: Path, *, safe: bool) -> Path:
    project = root
    source = project / "src"
    source.mkdir(parents=True)
    server = source / "redis-server"
    server.write_text(
        FAKE_REDIS_SERVER.format(safe="True" if safe else "False"),
        encoding="utf-8",
    )
    server.chmod(0o755)
    return project


def _assert_project_full_wiring() -> None:
    overlay = yaml.safe_load(
        (
            ROOT
            / "runtime"
            / "python"
            / "smell_core"
            / "defaults"
            / "projects.runtime-overrides.yaml"
        ).read_text(encoding="utf-8")
    )
    projects = {
        str(item.get("root") or ""): item
        for item in list(overlay.get("projects") or [])
    }
    tornado_test = str(projects["/opt/projects/python/tornado"]["test"]["script"])
    assert "tornado/test/web_test.py" in tornado_test, tornado_test
    assert "tornado_http1_continue_ownership.py" in tornado_test, tornado_test
    redis_test = str(projects["/opt/projects/c/redis"]["test"]["script"])
    assert "./src/redis-server test all" in redis_test, redis_test
    assert "redis_missing_geo_source_contract.py" in redis_test, redis_test


def main() -> int:
    _assert_project_full_wiring()
    with tempfile.TemporaryDirectory(prefix="nonjava-semantic-probes-") as raw:
        root = Path(raw)
        cases = (
            (
                TORNADO_PROBE,
                _tornado_project(root / "tornado-safe"),
                0,
            ),
            (
                TORNADO_PROBE,
                _tornado_project(
                    root / "tornado-r5",
                    extra_callback="delegate.on_connection_close()",
                ),
                1,
            ),
            (
                TORNADO_PROBE,
                _tornado_project(
                    root / "tornado-double-finish",
                    extra_callback="delegate.finish()",
                ),
                1,
            ),
            (REDIS_PROBE, _redis_project(root / "redis-safe", safe=True), 0),
            (REDIS_PROBE, _redis_project(root / "redis-r5", safe=False), 1),
        )
        for script, project, expected in cases:
            result = _probe(script, project)
            assert result.returncode == expected, (
                project.name,
                result.stdout,
                result.stderr,
            )
    print(
        "Non-Java behavior probes passed: Tornado finish/close ownership and Redis "
        "missing-source process behavior"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
