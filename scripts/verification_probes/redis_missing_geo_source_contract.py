#!/usr/bin/env python3
from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _resp(*parts: str) -> bytes:
    encoded = [part.encode("utf-8") for part in parts]
    chunks = [f"*{len(encoded)}\r\n".encode("ascii")]
    for part in encoded:
        chunks.extend((f"${len(part)}\r\n".encode("ascii"), part, b"\r\n"))
    return b"".join(chunks)


def _read_line(stream: socket.socket) -> bytes:
    data = bytearray()
    while not data.endswith(b"\r\n"):
        chunk = stream.recv(1)
        if not chunk:
            raise RuntimeError("Redis closed the connection before replying")
        data.extend(chunk)
    return bytes(data)


def _wait_for_socket(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise RuntimeError(f"Redis exited during startup with {process.returncode}")
        time.sleep(0.05)
    raise RuntimeError("Redis Unix socket was not created")


def _exercise(server: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="redis-geo-contract-") as raw:
        runtime = Path(raw)
        socket_path = runtime / "redis.sock"
        process = subprocess.Popen(
            [
                str(server),
                "--port",
                "0",
                "--save",
                "",
                "--appendonly",
                "no",
                "--daemonize",
                "no",
                "--protected-mode",
                "no",
                "--unixsocket",
                str(socket_path),
                "--unixsocketperm",
                "700",
                "--dir",
                str(runtime),
            ],
            cwd=server.parent.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_socket(socket_path, process)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5.0)
                client.connect(str(socket_path))
                client.sendall(
                    _resp(
                        "GEORADIUSBYMEMBER",
                        "__missing_geo_source__",
                        "member",
                        "1",
                        "km",
                        "WITHDIST",
                        "COUNT",
                        "1",
                    )
                )
                missing_reply = _read_line(client)
                if missing_reply != b"*0\r\n":
                    raise RuntimeError(
                        f"missing source returned {missing_reply!r}, expected an empty array"
                    )
                client.sendall(_resp("PING"))
                ping_reply = _read_line(client)
                if ping_reply != b"+PONG\r\n":
                    raise RuntimeError(
                        f"server did not continue parsing after missing source: {ping_reply!r}"
                    )
            if process.poll() is not None:
                raise RuntimeError(
                    f"Redis exited after the missing-source command with {process.returncode}"
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: redis_missing_geo_source_contract.py PROJECT_ROOT")
    project_root = Path(argv[1]).expanduser().resolve()
    server = project_root / "src" / "redis-server"
    if not server.is_file():
        print(f"REDIS_GEO_SERVER_MISSING: {server}", file=sys.stderr)
        return 1
    try:
        _exercise(server)
    except (OSError, RuntimeError, socket.timeout) as exc:
        print(f"REDIS_GEO_MISSING_SOURCE_CONTRACT_FAILED: {exc}", file=sys.stderr)
        return 1
    print("Redis GEO missing-source behavior and continued parsing passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
