import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be zero or greater")
    return value


def _optional_string(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    workspace_root: Path
    gemini_api_key: str | None
    gemini_model_primary: str
    gemini_model_fallback: str | None
    gemini_timeout_seconds: int
    clone_timeout_seconds: int
    docker_info_timeout_seconds: int
    docker_pull_timeout_seconds: int
    docker_build_timeout_seconds: int
    docker_run_timeout_seconds: int
    docker_memory: str
    docker_cpus: str
    docker_pids_limit: int
    max_tree_entries: int
    max_relevant_files: int
    max_file_characters: int
    max_source_characters: int
    max_iterations: int = 5
    max_files_per_iteration: int = 5
    max_patch_characters: int = 50_000
    max_search_results: int = 100
    gemini_max_retries: int = 2
    gemini_retry_backoff_seconds: float = 1.0

    @property
    def gemini_model(self) -> str:
        """Backward-compatible name for the configured primary model."""
        return self.gemini_model_primary

    @classmethod
    def from_environment(cls) -> "Settings":
        workspace_value = os.getenv("FIXFLOW_WORKSPACE_ROOT")
        workspace_root = (
            Path(workspace_value).expanduser().resolve()
            if workspace_value
            else PROJECT_ROOT / "workspaces"
        )
        return cls(
            workspace_root=workspace_root,
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model_primary=(
                _optional_string("GEMINI_MODEL_PRIMARY")
                or _optional_string("GEMINI_MODEL")
                or "gemini-3.7-flash"
            ),
            gemini_model_fallback=_optional_string("GEMINI_MODEL_FALLBACK"),
            gemini_timeout_seconds=_positive_int("GEMINI_TIMEOUT_SECONDS", 120),
            clone_timeout_seconds=_positive_int("CLONE_TIMEOUT_SECONDS", 90),
            docker_info_timeout_seconds=_positive_int(
                "DOCKER_INFO_TIMEOUT_SECONDS", 10
            ),
            docker_pull_timeout_seconds=_positive_int(
                "DOCKER_PULL_TIMEOUT_SECONDS", 180
            ),
            docker_build_timeout_seconds=_positive_int(
                "DOCKER_BUILD_TIMEOUT_SECONDS", 600
            ),
            docker_run_timeout_seconds=_positive_int(
                "DOCKER_RUN_TIMEOUT_SECONDS", 300
            ),
            docker_memory=os.getenv("DOCKER_MEMORY", "1g"),
            docker_cpus=os.getenv("DOCKER_CPUS", "1.0"),
            docker_pids_limit=_positive_int("DOCKER_PIDS_LIMIT", 256),
            max_tree_entries=_positive_int("MAX_TREE_ENTRIES", 300),
            max_relevant_files=_positive_int("MAX_RELEVANT_FILES", 16),
            max_file_characters=_positive_int("MAX_FILE_CHARACTERS", 30_000),
            max_source_characters=_positive_int(
                "MAX_SOURCE_CHARACTERS", 180_000
            ),
            max_iterations=_positive_int("MAX_ITERATIONS", 5),
            max_files_per_iteration=_positive_int(
                "MAX_FILES_PER_ITERATION", 5
            ),
            max_patch_characters=_positive_int(
                "MAX_PATCH_CHARACTERS", 50_000
            ),
            max_search_results=_positive_int("MAX_SEARCH_RESULTS", 100),
            gemini_max_retries=_positive_int("GEMINI_MAX_RETRIES", 2),
            gemini_retry_backoff_seconds=_non_negative_float(
                "GEMINI_RETRY_BACKOFF_SECONDS", 1.0
            ),
        )


settings = Settings.from_environment()
