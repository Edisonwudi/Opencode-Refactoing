"""Cross-process admission control for expensive project verification.

The controller owns the lock directory and fixed slot files.  Workers only
take advisory locks; they do not invent capacity or fall back to an unlocked
run when the configured contract is incomplete.
"""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Optional


SLOT_DIRECTORY_ENV = "SMELL_PROJECT_FULL_SLOT_DIR"
SLOT_LIMIT_ENV = "SMELL_PROJECT_FULL_SLOT_LIMIT"
SAMPLE_DEADLINE_EPOCH_MS_ENV = "SMELL_SAMPLE_DEADLINE_EPOCH_MS"


class VerificationSlotError(RuntimeError):
    """The configured project-verification admission contract failed."""


@dataclass(frozen=True)
class VerificationSlotLease:
    enabled: bool
    slot_index: Optional[int]
    waited_seconds: float


def _configured_slot_files(
    env: Mapping[str, str],
) -> tuple[list[Path], float] | None:
    raw_directory = str(env.get(SLOT_DIRECTORY_ENV, "")).strip()
    raw_limit = str(env.get(SLOT_LIMIT_ENV, "")).strip()
    if not raw_directory and not raw_limit:
        return None
    if not raw_directory or not raw_limit:
        raise VerificationSlotError(
            f"{SLOT_DIRECTORY_ENV} and {SLOT_LIMIT_ENV} must be configured together."
        )
    directory = Path(raw_directory)
    if not directory.is_absolute() or not directory.is_dir():
        raise VerificationSlotError(
            f"{SLOT_DIRECTORY_ENV} must name an existing absolute directory."
        )
    if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 64:
        raise VerificationSlotError(
            f"{SLOT_LIMIT_ENV} must be an integer between 1 and 64."
        )
    raw_deadline = str(env.get(SAMPLE_DEADLINE_EPOCH_MS_ENV, "")).strip()
    if not raw_deadline.isdigit():
        raise VerificationSlotError(
            f"{SAMPLE_DEADLINE_EPOCH_MS_ENV} is required when verification slots are enabled."
        )
    files = [directory / f"slot-{index}.lock" for index in range(int(raw_limit))]
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise VerificationSlotError(
            "The controller-owned verification slot files are missing: "
            + ", ".join(missing)
        )
    return files, int(raw_deadline) / 1000.0


@contextmanager
def acquire_verification_slot(
    env: Mapping[str, str] | None = None,
) -> Iterator[VerificationSlotLease]:
    """Take one controller-owned project-full slot until the context exits."""
    configured = _configured_slot_files(env or os.environ)
    if configured is None:
        yield VerificationSlotLease(False, None, 0.0)
        return

    slot_files, deadline_epoch = configured
    started = time.monotonic()
    while True:
        if time.time() >= deadline_epoch:
            raise VerificationSlotError(
                "The sample deadline expired while waiting for a project-full slot."
            )
        for slot_index, path in enumerate(slot_files):
            try:
                descriptor = os.open(path, os.O_RDWR)
            except OSError as exc:
                raise VerificationSlotError(
                    f"Could not open controller-owned verification slot {path}."
                ) from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            except OSError as exc:
                os.close(descriptor)
                raise VerificationSlotError(
                    f"Could not lock controller-owned verification slot {path}."
                ) from exc
            try:
                yield VerificationSlotLease(
                    True,
                    slot_index,
                    max(0.0, time.monotonic() - started),
                )
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            return
        time.sleep(0.1)
