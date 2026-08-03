from __future__ import annotations

import copy
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .analysis import detect_language_from_path, extract_snippet, signature_parameter_type_fingerprint
from .location import LocationTarget, parse_locations


SUPPORTED_LANGUAGES = {"java", "python", "c", "cpp"}
VERIFICATION_MODES = {"local", "auto", "sample_optimized", "project_full"}
JAVA_VERIFICATION_MODES = {"sample_optimized", "project_full"}


@dataclass
class CommandConfig:
    command: Optional[str] = None
    script: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CommandConfig":
        data = data or {}
        return cls(
            command=_clean_optional_string(data.get("command")),
            script=_clean_optional_string(data.get("script")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"command": self.command, "script": self.script}


@dataclass
class DefaultsConfig:
    shell_timeout: int = 600
    run_build: bool = True
    run_tests: bool = True

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "DefaultsConfig":
        data = data or {}
        return cls(
            shell_timeout=int(data.get("shell_timeout", 600)),
            run_build=bool(data.get("run_build", True)),
            run_tests=bool(data.get("run_tests", True)),
        )


@dataclass
class SmellProfile:
    instruction: str
    constraints: List[str] = field(default_factory=list)
    verification: List[str] = field(default_factory=list)
    guards: List[Dict[str, Any]] = field(default_factory=list)
    retry_hint_template: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SmellProfile":
        return cls(
            instruction=str(data.get("instruction", "")).strip(),
            constraints=[str(item) for item in data.get("constraints", [])],
            verification=[str(item) for item in data.get("verification", [])],
            guards=[dict(item) for item in data.get("guards", [])],
            retry_hint_template=_clean_optional_string(data.get("retry_hint_template")),
        )

    def merged_with(self, override: Optional[Dict[str, Any]]) -> "SmellProfile":
        if not override:
            return copy.deepcopy(self)
        base = asdict(self)
        for key, value in override.items():
            if value is None:
                continue
            base[key] = copy.deepcopy(value)
        return SmellProfile.from_dict(base)


@dataclass
class LanguageConfig:
    detect_extensions: List[str] = field(default_factory=list)
    build: CommandConfig = field(default_factory=CommandConfig)
    test: CommandConfig = field(default_factory=CommandConfig)
    smells: Dict[str, SmellProfile] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LanguageConfig":
        smells = {
            name: SmellProfile.from_dict(cfg or {})
            for name, cfg in (data.get("smells", {}) or {}).items()
        }
        return cls(
            detect_extensions=[str(item) for item in data.get("detect_extensions", [])],
            build=CommandConfig.from_dict(data.get("build")),
            test=CommandConfig.from_dict(data.get("test")),
            smells=smells,
        )


@dataclass
class ProjectRootsConfig:
    dataset: str = "."
    idea: str = "."
    build: str = "."

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ProjectRootsConfig":
        data = data or {}
        return cls(
            dataset=str(data.get("dataset") or ".").strip() or ".",
            idea=str(data.get("idea") or ".").strip() or ".",
            build=str(data.get("build") or ".").strip() or ".",
        )


@dataclass
class ProjectOverride:
    root: Path
    language: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    roots: ProjectRootsConfig = field(default_factory=ProjectRootsConfig)
    build: CommandConfig = field(default_factory=CommandConfig)
    test: CommandConfig = field(default_factory=CommandConfig)
    smells: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectOverride":
        language = _normalize_language(data.get("language"))
        env = {str(k): str(v) for k, v in (data.get("env", {}) or {}).items()}
        smells = {name: dict(cfg or {}) for name, cfg in (data.get("smells", {}) or {}).items()}
        return cls(
            root=Path(str(data["root"])).expanduser().resolve(),
            language=language,
            env=env,
            cwd=_clean_optional_string(data.get("cwd")),
            roots=ProjectRootsConfig.from_dict(data.get("roots")),
            build=CommandConfig.from_dict(data.get("build")),
            test=CommandConfig.from_dict(data.get("test")),
            smells=smells,
        )


@dataclass
class RefactorConfig:
    defaults: DefaultsConfig
    languages: Dict[str, LanguageConfig]


@dataclass
class ResolvedRunConfig:
    project_root: Path
    dataset_root: Path
    idea_project_root: Path
    build_root: Path
    smell: str
    language: str
    locations: List[LocationTarget]
    defaults: DefaultsConfig
    build: CommandConfig
    test: CommandConfig
    env: Dict[str, str]
    cwd: Path
    profile: SmellProfile
    project_override: Optional[ProjectOverride] = None
    idea_refactor_cli: Optional[str] = None
    idea_refactor_ready: bool = False
    verification_mode: str = "project_full"
    build_source: str = "projects.yaml"
    test_source: str = "projects.yaml"
    sample_test_location: str = ""
    sample_test_command: str = ""
    target_context: Dict[str, Any] = field(default_factory=dict)
    finding_contract: Dict[str, Any] = field(default_factory=dict)
    # Java Guard v5 freezes a target predicate, not a project finding catalog.
    # ``finding_contract`` remains only for non-Java checkpoint compatibility.
    guard_contract: Dict[str, Any] = field(default_factory=dict)
    guard_scope: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_root": str(self.project_root),
            "dataset_root": str(self.dataset_root),
            "idea_project_root": str(self.idea_project_root),
            "build_root": str(self.build_root),
            "smell": self.smell,
            "language": self.language,
            "locations": [
                {
                    "raw": item.raw,
                    "project_path": str(item.project_path),
                    "file_path": str(item.file_path),
                    "line": item.line,
                    "method": item.method,
                    "class_name": item.class_name,
                    "start_line": item.start_line,
                    "signature_text": item.signature_text,
                    "parameter_count": item.parameter_count,
                    "param_type_fingerprint": item.param_type_fingerprint,
                }
                for item in self.locations
            ],
            "defaults": asdict(self.defaults),
            "build": self.build.to_dict(),
            "test": self.test.to_dict(),
            "env": dict(self.env),
            "cwd": str(self.cwd),
            "profile": asdict(self.profile),
            "project_override_root": str(self.project_override.root) if self.project_override else None,
            "idea_refactor_cli": self.idea_refactor_cli,
            "idea_refactor_ready": self.idea_refactor_ready,
            "verification_mode": self.verification_mode,
            "build_source": self.build_source,
            "test_source": self.test_source,
            "sample_test_location": self.sample_test_location,
            "sample_test_command": self.sample_test_command,
            "target_context": copy.deepcopy(self.target_context),
            "finding_contract": copy.deepcopy(self.finding_contract),
            "guard_contract": copy.deepcopy(self.guard_contract),
            "guard_scope": (
                {
                    "changed_files": list(self.guard_scope.changed_files),
                    "changed_production_files": list(
                        self.guard_scope.changed_production_files
                    ),
                    "target_files": list(self.guard_scope.target_files),
                    "analysis_files": list(self.guard_scope.analysis_files),
                }
                if self.guard_scope is not None
                else None
            ),
        }


def interpolate_command_text(text: str, project_root: Path) -> str:
    root_text = str(project_root)
    return text.replace("${project_root}", root_text).replace("${PROJECT_ROOT}", root_text)


def bundled_refactor_config_path() -> Path:
    return Path(resources.files("smell_core.defaults").joinpath("refactor.yaml"))


def bundled_projects_config_path() -> Path:
    return Path(resources.files("smell_core.defaults").joinpath("projects.yaml"))


def load_refactor_config(path: Optional[str]) -> RefactorConfig:
    source = Path(path).expanduser().resolve() if path else bundled_refactor_config_path()
    data = _load_yaml(source)
    defaults = _apply_defaults_env_overrides(DefaultsConfig.from_dict(data.get("defaults")))
    languages = {
        _normalize_language(name) or name: LanguageConfig.from_dict(cfg or {})
        for name, cfg in (data.get("languages", {}) or {}).items()
    }
    if not languages:
        raise ValueError("refactor.yaml must define at least one language.")
    return RefactorConfig(defaults=defaults, languages=languages)


def _apply_defaults_env_overrides(defaults: DefaultsConfig) -> DefaultsConfig:
    shell_timeout = _clean_optional_string(os.environ.get("MINI_SHELL_TIMEOUT"))
    if not shell_timeout:
        return defaults
    return replace(defaults, shell_timeout=int(shell_timeout))


def load_project_overrides(path: Optional[str]) -> List[ProjectOverride]:
    source = Path(path).expanduser().resolve() if path else bundled_projects_config_path()
    data = _load_yaml(source)
    return [ProjectOverride.from_dict(entry) for entry in data.get("projects", [])]


def resolve_run_config(
    *,
    refactor_config: RefactorConfig,
    project_overrides: List[ProjectOverride],
    project_root: str,
    project_override_root: Optional[str] = None,
    smell: str,
    location: str,
    cli_language: Optional[str] = None,
    verification_mode: str = "",
    sample_test_location: str = "",
    sample_test_command: str = "",
    target_context: Optional[Dict[str, Any]] = None,
) -> ResolvedRunConfig:
    from .target_context import validate_target_context

    project_root_path = Path(project_root).expanduser().resolve()
    override_lookup_root = (
        Path(project_override_root).expanduser().resolve()
        if project_override_root
        else project_override_lookup_root(project_overrides, project_root_path)
    )
    override = select_project_override(project_overrides, override_lookup_root)
    roots = override.roots if override else ProjectRootsConfig()
    dataset_root = _resolve_project_subroot(project_root_path, roots.dataset)
    idea_project_root = _resolve_project_subroot(project_root_path, roots.idea)
    build_root = _resolve_project_subroot(project_root_path, roots.build)
    locations = parse_locations(location, dataset_root)
    language = resolve_language(
        cli_language=cli_language,
        project_override=override,
        locations=locations,
        refactor_config=refactor_config,
    )
    locations = _enrich_locations_with_source_anchors(locations, language)
    language_config = refactor_config.languages.get(language)
    if language_config is None:
        raise ValueError(f"Unsupported language '{language}'.")
    if smell not in language_config.smells:
        raise ValueError(f"Unsupported smell '{smell}' for language '{language}'.")
    profile = language_config.smells[smell]
    smell_override = override.smells.get(smell) if override else None
    merged_profile = profile.merged_with(smell_override)
    build = _merge_command_config(language_config.build, override.build if override else None)
    project_test = _merge_command_config(language_config.test, override.test if override else None)
    if override and override.root.resolve() != project_root_path:
        build = _rebase_command_config(build, override.root.resolve(), project_root_path)
        project_test = _rebase_command_config(project_test, override.root.resolve(), project_root_path)
    normalized_verification_mode = _resolve_verification_mode(
        verification_mode,
        language=language,
        sample_test_command=sample_test_command,
    )
    if normalized_verification_mode == "sample_optimized" and _clean_optional_string(sample_test_command):
        test = CommandConfig(command=str(sample_test_command))
    else:
        test = project_test
    test_source = (
        "dataset"
        if normalized_verification_mode == "sample_optimized"
        else ("projects.yaml" if normalized_verification_mode != "local" else "")
    )
    cwd = build_root
    if override and override.cwd:
        cwd_path = Path(override.cwd)
        if cwd_path.is_absolute():
            try:
                cwd = project_root_path / cwd_path.resolve().relative_to(override.root.resolve())
            except ValueError:
                cwd = cwd_path
        else:
            cwd = project_root_path / cwd_path
        cwd = cwd.resolve()
    resolved_env = dict(override.env) if override else {}
    if override and override.root.resolve() != project_root_path:
        canonical = str(override.root.resolve())
        execution = str(project_root_path)
        resolved_env = {key: str(value).replace(canonical, execution) for key, value in resolved_env.items()}
    return ResolvedRunConfig(
        project_root=project_root_path,
        dataset_root=dataset_root,
        idea_project_root=idea_project_root,
        build_root=build_root,
        smell=smell,
        language=language,
        locations=locations,
        defaults=copy.deepcopy(refactor_config.defaults),
        build=build,
        test=test,
        env=resolved_env,
        cwd=cwd,
        profile=merged_profile,
        project_override=override,
        verification_mode=normalized_verification_mode,
        build_source="projects.yaml",
        test_source=test_source,
        sample_test_location=str(sample_test_location or ""),
        sample_test_command=str(sample_test_command or ""),
        target_context=copy.deepcopy(validate_target_context(target_context)),
    )


def _resolve_verification_mode(
    value: str,
    *,
    language: str,
    sample_test_command: str = "",
) -> str:
    default_mode = "project_full" if language == "java" else "local"
    mode = str(value or default_mode).strip()
    if mode not in VERIFICATION_MODES:
        raise ValueError(
            f"Unsupported verification_mode '{mode}'. Expected one of: {', '.join(sorted(VERIFICATION_MODES))}."
        )
    if language == "java" and mode not in JAVA_VERIFICATION_MODES:
        raise ValueError(
            "Java verification_mode must be 'sample_optimized' or 'project_full'; "
            "every Java PASS requires configured build/test verification."
        )
    if mode == "auto":
        return "sample_optimized" if _clean_optional_string(sample_test_command) else "project_full"
    return mode


def _resolve_project_subroot(project_root: Path, value: str) -> Path:
    root = Path(str(value or ".")).expanduser()
    if root.is_absolute():
        return root.resolve()
    return (project_root / root).resolve()


def _enrich_locations_with_source_anchors(
    locations: List[LocationTarget],
    language: str,
) -> List[LocationTarget]:
    enriched: List[LocationTarget] = []
    for target in locations:
        if target.class_name and not target.method:
            enriched.append(target)
            continue
        try:
            snippet = extract_snippet(target, language)
        except Exception:
            snippet = None
        if snippet is None:
            enriched.append(target)
            continue
        enriched.append(
            replace(
                target,
                start_line=snippet.start_line,
                signature_text=snippet.signature_text,
                parameter_count=snippet.parameter_count,
                param_type_fingerprint=signature_parameter_type_fingerprint(snippet.signature_text, language),
            )
        )
    return enriched


def resolve_language(
    *,
    cli_language: Optional[str],
    project_override: Optional[ProjectOverride],
    locations: List[LocationTarget],
    refactor_config: RefactorConfig,
) -> str:
    normalized_cli = _normalize_language(cli_language)
    if normalized_cli:
        if normalized_cli not in refactor_config.languages:
            raise ValueError(f"Unsupported language '{cli_language}'.")
        return normalized_cli
    if project_override and project_override.language:
        return project_override.language
    if locations:
        detected = detect_language_from_locations(locations, refactor_config)
        if detected:
            return detected
    raise ValueError("Unable to determine language from CLI, locations, or project override.")


def detect_language_from_locations(
    locations: List[LocationTarget], refactor_config: RefactorConfig
) -> Optional[str]:
    detected: Optional[str] = None
    extension_map = {
        ext.lower(): language
        for language, cfg in refactor_config.languages.items()
        for ext in cfg.detect_extensions
    }
    for target in locations:
        language = extension_map.get(target.file_path.suffix.lower()) or detect_language_from_path(target.file_path)
        if not language:
            continue
        if detected and detected != language:
            raise ValueError(f"Multiple languages detected in locations; first '{detected}', then '{language}'.")
        detected = language
    return detected


def select_project_override(
    project_overrides: List[ProjectOverride], project_root: Path
) -> Optional[ProjectOverride]:
    target = project_root.resolve()
    best: Optional[ProjectOverride] = None
    best_len = -1
    for entry in project_overrides:
        root = entry.root.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if len(root.parts) > best_len:
            best = entry
            best_len = len(root.parts)
    return best


def project_override_lookup_root(project_overrides: List[ProjectOverride], project_root: Path) -> Path:
    """Return the root used only for matching project overrides.

    Batch execution may happen in a git worktree outside the canonical project
    directory. In that case, keep execution rooted at *project_root*, but use
    the original repository root for projects.yaml matching when it can be
    proven by git metadata and an existing override.
    """
    target = project_root.resolve()
    if select_project_override(project_overrides, target):
        return target
    derived = _derive_git_worktree_common_root(target)
    if derived and select_project_override(project_overrides, derived):
        return derived
    return target


def _derive_git_worktree_common_root(project_root: Path) -> Optional[Path]:
    if not project_root.exists():
        return None
    if not (project_root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--git-common-dir"],
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    common_text = proc.stdout.strip()
    if not common_text:
        return None
    common_dir = Path(common_text).expanduser()
    if not common_dir.is_absolute():
        common_dir = (project_root / common_dir).resolve()
    else:
        common_dir = common_dir.resolve()
    if common_dir.name != ".git":
        return None
    candidate = common_dir.parent.resolve()
    if candidate == project_root.resolve():
        return None
    return candidate


def dump_resolved_config(config: ResolvedRunConfig) -> str:
    return json.dumps(config.to_dict(), indent=2, ensure_ascii=True)


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at top level in {path}.")
    return data


def _normalize_language(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip().lower()
    if normalized == "c++":
        normalized = "cpp"
    if normalized in SUPPORTED_LANGUAGES:
        return normalized
    return None


def _merge_command_config(base: CommandConfig, override: Optional[CommandConfig]) -> CommandConfig:
    if not override:
        return copy.deepcopy(base)
    if override.command is not None or override.script is not None:
        return copy.deepcopy(override)
    return CommandConfig(
        command=base.command,
        script=base.script,
    )


def _rebase_command_config(config: CommandConfig, canonical_root: Path, execution_root: Path) -> CommandConfig:
    canonical = str(canonical_root)
    execution = str(execution_root)
    return CommandConfig(
        command=config.command.replace(canonical, execution) if config.command is not None else None,
        script=config.script.replace(canonical, execution) if config.script is not None else None,
    )


def _clean_optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None
