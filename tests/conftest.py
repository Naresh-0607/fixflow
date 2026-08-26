from pathlib import Path

import pytest

from app.core.config import Settings


@pytest.fixture()
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        workspace_root=tmp_path / "workspaces",
        gemini_api_key="test-key",
        gemini_model_primary="gemini-test-primary",
        gemini_model_fallback="gemini-test-fallback",
        gemini_timeout_seconds=10,
        clone_timeout_seconds=10,
        docker_info_timeout_seconds=5,
        docker_pull_timeout_seconds=10,
        docker_build_timeout_seconds=20,
        docker_run_timeout_seconds=20,
        docker_memory="512m",
        docker_cpus="0.5",
        docker_pids_limit=128,
        max_tree_entries=100,
        max_relevant_files=10,
        max_file_characters=10_000,
        max_source_characters=40_000,
    )
