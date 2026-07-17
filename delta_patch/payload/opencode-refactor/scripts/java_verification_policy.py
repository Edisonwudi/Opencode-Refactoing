"""Normalize Java verification commands for behavior-focused delivery checks."""

from __future__ import annotations

import re


GRADLE_WRAPPER = re.compile(r"(?<!\S)(\./gradlew)(?!\s+-PnoSpotless(?:\s|$))")
MAVEN = re.compile(
    r"(?<!\S)mvn(?:\s+-D(?:checkstyle\.skip|spotless\.check\.skip|spotless\.apply\.skip)=true)*"
)
MAVEN_VERIFICATION_FLAGS = (
    "-Dcheckstyle.skip=true "
    "-Dspotless.check.skip=true "
    "-Dspotless.apply.skip=true"
)


def normalize_verification_command(command: str, project_name: str = "") -> str:
    """Keep compile/test verification independent from formatting-only gates.

    Stirling wires Java compilation to Spotless apply.  Its repository-provided
    ``noSpotless`` property disables that dependency without changing compile or
    test behavior.  Maven Checkstyle is likewise a formatting/style gate rather
    than a compilation or behavioral test, so delivery verification skips it.
    """

    normalized = str(command or "")
    if "stirling" in str(project_name or "").lower():
        normalized = GRADLE_WRAPPER.sub(r"\1 -PnoSpotless", normalized)
    normalized = MAVEN.sub(f"mvn {MAVEN_VERIFICATION_FLAGS}", normalized)
    return normalized
