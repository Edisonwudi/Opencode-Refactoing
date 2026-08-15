#!/usr/bin/env python3
"""Adversarial self-check for cross-worker project-full admission."""
from __future__ import annotations

import multiprocessing
import os
import queue
import importlib.util
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "python"))

from smell_core.verification_slots import (  # noqa: E402
    SAMPLE_DEADLINE_EPOCH_MS_ENV,
    SLOT_DIRECTORY_ENV,
    SLOT_LIMIT_ENV,
    VerificationSlotError,
    acquire_verification_slot,
)


def _worker(env: dict[str, str], queue: multiprocessing.Queue) -> None:
    with acquire_verification_slot(env) as lease:
        queue.put((lease.slot_index, lease.waited_seconds))


def main() -> int:
    with acquire_verification_slot({}) as lease:
        assert lease.enabled is False

    try:
        with acquire_verification_slot({SLOT_LIMIT_ENV: "2"}):
            raise AssertionError("incomplete configuration was accepted")
    except VerificationSlotError:
        pass

    with tempfile.TemporaryDirectory(prefix="verification-slots-") as raw:
        directory = Path(raw)
        (directory / "slot-0.lock").touch()
        env = {
            SLOT_DIRECTORY_ENV: str(directory),
            SLOT_LIMIT_ENV: "1",
            SAMPLE_DEADLINE_EPOCH_MS_ENV: str(int((time.time() + 5) * 1000)),
        }
        result_queue: multiprocessing.Queue = multiprocessing.Queue()
        with acquire_verification_slot(env) as outer:
            assert outer.enabled is True and outer.slot_index == 0
            child = multiprocessing.Process(target=_worker, args=(env, result_queue))
            child.start()
            try:
                result_queue.get(timeout=0.25)
                raise AssertionError("a second worker bypassed the single slot")
            except queue.Empty:
                pass
        child.join(timeout=3)
        assert child.exitcode == 0, child.exitcode
        slot_index, waited_seconds = result_queue.get(timeout=1)
        assert slot_index == 0 and waited_seconds >= 0.2, (slot_index, waited_seconds)

        expired = dict(env)
        expired[SAMPLE_DEADLINE_EPOCH_MS_ENV] = str(int((time.time() - 1) * 1000))
        with acquire_verification_slot(env):
            try:
                with acquire_verification_slot(expired):
                    raise AssertionError("expired wait unexpectedly acquired a slot")
            except VerificationSlotError:
                pass

        with patch(
            "smell_core.verification_slots.os.open",
            side_effect=PermissionError("controller slot became unavailable"),
        ):
            try:
                with acquire_verification_slot(env):
                    raise AssertionError("unopenable slot unexpectedly acquired")
            except VerificationSlotError as exc:
                assert "Could not open controller-owned verification slot" in str(exc)

        bridge_path = ROOT / "runtime" / "python" / "bridge" / "smell_bridge.py"
        spec = importlib.util.spec_from_file_location("slot_bridge_selfcheck", bridge_path)
        assert spec is not None and spec.loader is not None
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        original_impl = bridge._run_project_full_in_fresh_worktree_impl
        original_failure = bridge._final_verify_infra_failure_result
        saved_env = {
            name: os.environ.get(name)
            for name in (
                SLOT_DIRECTORY_ENV,
                SLOT_LIMIT_ENV,
                SAMPLE_DEADLINE_EPOCH_MS_ENV,
            )
        }
        try:
            os.environ.update(env)
            bridge._run_project_full_in_fresh_worktree_impl = (
                lambda *_args, **_kwargs: {"success": True}
            )
            admitted = bridge._run_project_full_in_fresh_worktree(object(), None)
            assert admitted["success"] is True
            assert admitted["verification_admission"]["success"] is True
            assert admitted["verification_admission"]["slot_index"] == 0

            os.environ[SLOT_DIRECTORY_ENV] = str(directory / "missing")
            bridge._final_verify_infra_failure_result = (
                lambda _resolved, *, stage, message, **_kwargs: {
                    "success": False,
                    "reason": "FINAL_VERIFY_INFRA_FAILED",
                    "stage": stage,
                    "message": message,
                }
            )
            rejected = bridge._run_project_full_in_fresh_worktree(object(), None)
            assert rejected["success"] is False
            assert rejected["stage"] == "acquire_project_full_slot"
        finally:
            bridge._run_project_full_in_fresh_worktree_impl = original_impl
            bridge._final_verify_infra_failure_result = original_failure
            for name, value in saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    print("verification slot self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
