#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path


class _HeaderStream:
    max_buffer_size = 1024 * 1024

    def __init__(self) -> None:
        self._headers = [
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n",
        ]
        self._closed = False
        self.close_callback = None

    async def read_until_regex(self, _regex: bytes, **_kwargs: object) -> bytes:
        if not self._headers:
            raise AssertionError("HTTP1Connection read past the final response")
        return self._headers.pop(0)

    async def read_until_close(self) -> bytes:
        await asyncio.Future()
        raise AssertionError("unreachable")

    def set_close_callback(self, callback: object) -> None:
        self.close_callback = callback

    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True


class _CountingDelegate:
    def __init__(self) -> None:
        self.header_codes: list[int] = []
        self.finish_count = 0
        self.connection_close_count = 0

    def headers_received(self, start_line: object, _headers: object) -> None:
        self.header_codes.append(int(getattr(start_line, "code")))

    def data_received(self, _data: bytes) -> None:
        return None

    def finish(self) -> None:
        self.finish_count += 1

    def on_connection_close(self) -> None:
        self.connection_close_count += 1


async def _exercise(connection_class: type) -> tuple[bool, _CountingDelegate]:
    stream = _HeaderStream()
    delegate = _CountingDelegate()
    connection = connection_class(stream, True)
    finish_future = getattr(connection, "_finish_future", None)
    if finish_future is not None and not finish_future.done():
        finish_future.set_result(None)
    result = bool(
        await asyncio.wait_for(connection.read_response(delegate), timeout=5.0)
    )
    return result, delegate


def _load_connection(project_root: Path) -> type:
    sys.path.insert(0, str(project_root))
    module = importlib.import_module("tornado.http1connection")
    module_path = Path(str(module.__file__)).resolve()
    try:
        module_path.relative_to(project_root)
    except ValueError as exc:
        raise RuntimeError(
            f"loaded Tornado outside candidate project: {module_path}"
        ) from exc
    return module.HTTP1Connection


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: tornado_http1_continue_ownership.py PROJECT_ROOT")
    project_root = Path(argv[1]).expanduser().resolve()
    connection_class = _load_connection(project_root)
    try:
        result, delegate = asyncio.run(_exercise(connection_class))
    except (AssertionError, asyncio.TimeoutError, OSError) as exc:
        print(
            f"TORNADO_DELEGATE_COMPLETION_OWNERSHIP_FAILED: {exc}",
            file=sys.stderr,
        )
        return 1
    expected_codes = [204]
    if (
        not result
        or delegate.header_codes != expected_codes
        or delegate.finish_count != 1
        or delegate.connection_close_count != 0
    ):
        print(
            "TORNADO_DELEGATE_COMPLETION_OWNERSHIP_FAILED: "
            f"result={result} headers={delegate.header_codes} "
            f"finish_count={delegate.finish_count} "
            f"on_connection_close_count={delegate.connection_close_count}",
            file=sys.stderr,
        )
        return 1
    print("Tornado final-response delegate completion ownership passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
