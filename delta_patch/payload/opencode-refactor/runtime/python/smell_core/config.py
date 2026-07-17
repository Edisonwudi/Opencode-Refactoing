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
LLM_MODEL_ALIAS_ENVS = {
    "openai": "OPENAI_MODEL",
    "glm": "ZHIPU_MODEL",
    "anthropic": "ANTHROPIC_MODEL",
    "deepseek": "DEEPSEEK_MODEL",
    "gemini": "GEMINI_MODEL",
    "openrouter": "OPENROUTER_MODEL",
}
LLM_BASE_URL_ENVS = {
    "openai": ("OPENAI_API_BASE", "OPENAI_BASE_URL"),
    "glm": ("ZHIPU_API_BASE", "ZHIPU_BASE_URL"),
    "anthropic": ("ANTHROPIC_BASE_URL",),
    "deepseek": ("DEEPSEEK_BASE_URL",),
    "gemini": ("GEMINI_BASE_URL",),
    "openrouter": ("OPENROUTER_BASE_URL",),
}
LLM_API_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


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
    step_limit: int = 100
    shell_timeout: int = 600
    no_progress_timeout: int = 480
    model_retry_attempts: int = 2
    run_build: bool = True
    run_tests: bool = True
    output_root: str = "runs"

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "DefaultsConfig":
        data = data or {}
        return cls(
            step_limit=int(data.get("step_limit", 100)),
            shell_timeout=int(data.get("shell_timeout", 600)),
            no_progress_timeout=int(data.get("no_progress_timeout", 480)),
            model_retry_attempts=int(data.get("model_retry_attempts", 2)),
            run_build=bool(data.get("run_build", True)),
            run_tests=bool(data.get("run_tests", True)),
            output_root=str(data.get("output_root", "runs")),
        )


@dataclass
class LLMConfig:
    provider: str = ""
    model: str = ""
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    context_window: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    request_timeout: Optional[int] = None
    recursion_limit: Optional[int] = None
    max_history_messages: Optional[int] = None
    auto_condense_percent: Optional[int] = None
    model_kwargs: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["LLMConfig"]:
        data = data or {}
        if not data:
            return None
        return cls(
            provider=str(data.get("provider", "")).strip().lower(),
            model=str(data.get("model", "")).strip(),
            base_url=_clean_optional_string(data.get("base_url")),
            api_key=_clean_optional_string(data.get("api_key")),
            api_key_env=_clean_optional_string(data.get("api_key_env")),
            temperature=_coerce_optional_float(data.get("temperature")),
            max_tokens=_coerce_optional_int(data.get("max_tokens")),
            context_window=_coerce_optional_int(data.get("context_window")),
            top_p=_coerce_optional_float(data.get("top_p")),
            frequency_penalty=_coerce_optional_float(data.get("frequency_penalty")),
            presence_penalty=_coerce_optional_float(data.get("presence_penalty")),
            request_timeout=_coerce_optional_int(data.get("request_timeout")),
            recursion_limit=_coerce_optional_int(data.get("recursion_limit")),
            max_history_messages=_coerce_optional_int(data.get("max_history_messages")),
            auto_condense_percent=_coerce_optional_int(data.get("auto_condense_percent")),
            model_kwargs=dict(data.get("model_kwargs", {}) or {}),
        )

    def resolved_api_key(self, env: Optional[Dict[str, str]] = None) -> Optional[str]:
        if self.api_key:
            return self.api_key
        if not self.api_key_env:
            return None
        merged_env = env if env is not None else {}
        return merged_env.get(self.api_key_env) or os.environ.get(self.api_key_env)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_key_configured": bool(self.api_key or (self.api_key_env and os.environ.get(self.api_key_env))),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "context_window": self.context_window,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "request_timeout": self.request_timeout,
            "recursion_limit": self.recursion_limit,
            "max_history_messages": self.max_history_messages,
            "auto_condense_percent": self.auto_condense_percent,
            "model_kwargs": dict(self.model_kwargs),
        }


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
    llm: Optional[LLMConfig]
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
    llm: Optional[LLMConfig]
    env: Dict[str, str]
    cwd: Path
    profile: SmellProfile
    project_override: Optional[ProjectOverride] = None
    idea_refactor_cli: Optional[str] = None
    idea_refactor_ready: bool = False
    verification_mode: str = "local"
    build_source: str = "projects.yaml"
    test_source: str = "projects.yaml"
    sample_test_location: str = ""
    sample_test_command: str = ""

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
            "llm": self.llm.to_public_dict() if self.llm else None,
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
    llm = _apply_llm_env_overrides(LLMConfig.from_dict(data.get("llm")))
    languages = {
        _normalize_language(name) or name: LanguageConfig.from_dict(cfg or {})
        for name, cfg in (data.get("languages", {}) or {}).items()
    }
    if not languages:
        raise ValueError("refactor.yaml must define at least one language.")
    return RefactorConfig(defaults=defaults, llm=llm, languages=languages)


def _apply_defaults_env_overrides(defaults: DefaultsConfig) -> DefaultsConfig:
    shell_timeout = _clean_optional_string(os.environ.get("MINI_SHELL_TIMEOUT"))
    if not shell_timeout:
        return defaults
    return replace(defaults, shell_timeout=int(shell_timeout))


def _apply_llm_env_overrides(llm: Optional[LLMConfig]) -> Optional[LLMConfig]:
    if not _llm_env_override_requested():
        return llm

    base = llm or LLMConfig()
    alias_provider, alias_model = _single_llm_model_alias()
    explicit_provider = _normalize_llm_provider(os.environ.get("LLM_PROVIDER"))
    provider = explicit_provider or alias_provider or _provider_from_base_url_env() or base.provider
    provider = _normalize_llm_provider(provider)

    model = _clean_optional_string(os.environ.get("LLM_MODEL")) or alias_model or base.model
    base_url = _clean_optional_string(os.environ.get("LLM_BASE_URL")) or _base_url_from_env(provider) or base.base_url
    api_key_env = _clean_optional_string(os.environ.get("LLM_API_KEY_ENV")) or LLM_API_KEY_ENVS.get(provider) or base.api_key_env
    request_timeout = (
        _coerce_optional_int(os.environ.get("LLM_REQUEST_TIMEOUT"))
        if _clean_optional_string(os.environ.get("LLM_REQUEST_TIMEOUT"))
        else base.request_timeout
    )

    return replace(
        base,
        provider=provider,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        request_timeout=request_timeout,
    )


def _llm_env_override_requested() -> bool:
    env_names = [
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY_ENV",
        "LLM_REQUEST_TIMEOUT",
        *LLM_MODEL_ALIAS_ENVS.values(),
        *(name for names in LLM_BASE_URL_ENVS.values() for name in names),
    ]
    return any(_clean_optional_string(os.environ.get(name)) for name in env_names)


def _single_llm_model_alias() -> tuple[str, str]:
    matches = [
        (provider, os.environ[env_name].strip())
        for provider, env_name in LLM_MODEL_ALIAS_ENVS.items()
        if _clean_optional_string(os.environ.get(env_name))
    ]
    if len(matches) > 1:
        names = ", ".join(LLM_MODEL_ALIAS_ENVS.values())
        raise ValueError(f"Only one provider model alias may be set: {names}.")
    return matches[0] if matches else ("", "")


def _provider_from_base_url_env() -> str:
    for provider, env_names in LLM_BASE_URL_ENVS.items():
        if any(_clean_optional_string(os.environ.get(name)) for name in env_names):
            return provider
    return ""


def _base_url_from_env(provider: str) -> Optional[str]:
    for env_name in LLM_BASE_URL_ENVS.get(provider, ()):
        value = _clean_optional_string(os.environ.get(env_name))
        if value:
            return value
    return None


def _normalize_llm_provider(provider: Optional[str]) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized in {"zhipu", "zhipuai"}:
        return "glm"
    if normalized == "google":
        return "gemini"
    return normalized


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
    verification_mode: str = "local",
    sample_test_location: str = "",
    sample_test_command: str = "",
) -> ResolvedRunConfig:
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
    normalized_verification_mode = _resolve_verification_mode(
        verification_mode,
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
        cwd = cwd_path if cwd_path.is_absolute() else (project_root_path / cwd_path)
        cwd = cwd.resolve()
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
        llm=copy.deepcopy(refactor_config.llm),
        env=dict(override.env) if override else {},
        cwd=cwd,
        profile=merged_profile,
        project_override=override,
        verification_mode=normalized_verification_mode,
        build_source="projects.yaml",
        test_source=test_source,
        sample_test_location=str(sample_test_location or ""),
        sample_test_command=str(sample_test_command or ""),
    )


def _resolve_verification_mode(value: str, *, sample_test_command: str = "") -> str:
    mode = str(value or "local").strip()
    if mode not in VERIFICATION_MODES:
        raise ValueError(
            f"Unsupported verification_mode '{mode}'. Expected one of: {', '.join(sorted(VERIFICATION_MODES))}."
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


def _clean_optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)
