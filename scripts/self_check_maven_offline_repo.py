#!/usr/bin/env python3
"""Self-check the Maven offline repository metadata normalizer."""

from __future__ import annotations

import tempfile
from pathlib import Path

from normalize_maven_offline_repo import metadata_fingerprint, normalize_marker, scan_repository


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="maven-offline-repo-check-") as raw:
        repository = Path(raw)
        central = repository / "org" / "example" / "demo" / "1.0" / "_remote.repositories"
        central.parent.mkdir(parents=True)
        central.write_text(
            "# resolver metadata\n"
            "demo-1.0.jar>central=\n"
            "demo-1.0.pom>central-https=\n"
            "locally-installed-1.0.jar>=\n",
            encoding="utf-8",
        )
        stale = central.parent / "demo-1.0.jar.lastUpdated"
        stale.write_text("stale\n", encoding="utf-8")

        before = scan_repository(repository, "local-all", check=True)
        assert before.invalid_entries == 2
        assert before.last_updated_files == 1
        fingerprint_before = metadata_fingerprint(repository)

        changed = scan_repository(repository, "local-all", check=False)
        assert changed.changed_files == 1
        assert changed.last_updated_files == 1
        assert not stale.exists()

        after = scan_repository(repository, "local-all", check=True)
        assert after.invalid_entries == 0
        assert after.last_updated_files == 0
        fingerprint_after = metadata_fingerprint(repository)
        assert fingerprint_before != fingerprint_after
        assert fingerprint_after == metadata_fingerprint(repository)
        late_stale = central.parent / "late.jar.lastUpdated"
        late_stale.write_text("introduced during batch\n", encoding="utf-8")
        assert fingerprint_after != metadata_fingerprint(repository)
        late_stale.unlink()
        marker_text = central.read_text(encoding="utf-8")
        assert "demo-1.0.jar>local-all=" in marker_text
        assert "demo-1.0.pom>local-all=" in marker_text
        assert "locally-installed-1.0.jar>=" in marker_text

    normalized, entries, invalid = normalize_marker("a.jar>central=\n", "offline")
    assert normalized == "a.jar>offline=\n"
    assert entries == 1
    assert invalid == 1
    print("maven offline repository metadata self-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
