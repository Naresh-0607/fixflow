import ast
import os
import re
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..core.config import Settings
from ..core.exceptions import GeminiAnalysisError
from ..llm.gemini_client import GeminiClient
from ..models.responses import QAAnalysisItem, TestResult


PYTHON_PATH_PATTERN = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[A-Za-z0-9_./\\-]+\.py)(?::\d+)?"
)
IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


class AnalysisService:
    def __init__(self, settings: Settings, gemini_client: GeminiClient) -> None:
        self.settings = settings
        self.gemini_client = gemini_client

    def analyze(
        self,
        *,
        job_id: str,
        repository_path: Path,
        test_result: TestResult,
    ) -> list[QAAnalysisItem]:
        tree, source_bundle = self.repository_context(repository_path, test_result)
        prompt = self._build_prompt(tree, test_result, source_bundle)
        raw_analysis = self.gemini_client.analyze(prompt, job_id=job_id)

        if isinstance(raw_analysis, dict):
            raw_analysis = raw_analysis.get("analysis", raw_analysis.get("failures"))
        try:
            return TypeAdapter(list[QAAnalysisItem]).validate_python(raw_analysis)
        except ValidationError as exc:
            raise GeminiAnalysisError(
                "Gemini returned JSON that does not match the required QA analysis schema.",
                job_id=job_id,
                details={"validation_error": str(exc)},
            ) from exc

    def repository_context(
        self,
        repository_path: Path,
        test_result: TestResult,
    ) -> tuple[str, str]:
        tree = self._repository_tree(repository_path)
        files = self._relevant_files(repository_path, test_result)
        source_bundle = self._read_files(repository_path, files)
        return tree, source_bundle

    def _repository_tree(self, repository_path: Path) -> str:
        entries: list[str] = []
        for root, directories, filenames in os.walk(repository_path, followlinks=False):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in IGNORED_DIRECTORIES
                and not (Path(root) / directory).is_symlink()
            )
            root_path = Path(root)
            for filename in sorted(filenames):
                path = root_path / filename
                if path.is_symlink():
                    continue
                entries.append(path.relative_to(repository_path).as_posix())
                if len(entries) >= self.settings.max_tree_entries:
                    entries.append("... tree truncated ...")
                    return "\n".join(entries)
        return "\n".join(entries)

    def _relevant_files(
        self,
        repository_path: Path,
        test_result: TestResult,
    ) -> list[Path]:
        relative_paths: list[str] = [failure.file for failure in test_result.failures]
        relative_paths.extend(
            match.group("path") for match in PYTHON_PATH_PATTERN.finditer(test_result.output)
        )

        candidates: list[Path] = []
        seen: set[Path] = set()
        for raw_path in relative_paths:
            candidate = self._resolve_repository_path(repository_path, raw_path)
            if candidate and candidate.suffix == ".py" and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

        for test_file in list(candidates):
            if "test" in test_file.name.lower() or "tests" in test_file.parts:
                for imported in self._local_imports(repository_path, test_file):
                    if imported not in seen:
                        seen.add(imported)
                        candidates.append(imported)

        return candidates[: self.settings.max_relevant_files]

    @staticmethod
    def _resolve_repository_path(repository_path: Path, raw_path: str) -> Path | None:
        normalized = raw_path.replace("\\", "/")
        if normalized.startswith("/workspace/"):
            normalized = normalized.removeprefix("/workspace/")
        path = Path(normalized)
        if path.is_absolute():
            try:
                relative = path.resolve().relative_to(repository_path.resolve())
            except ValueError:
                return None
            candidate = repository_path / relative
        else:
            candidate = repository_path / normalized
        try:
            resolved = candidate.resolve()
            resolved.relative_to(repository_path.resolve())
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() and not resolved.is_symlink() else None

    def _local_imports(self, repository_path: Path, test_file: Path) -> list[Path]:
        try:
            tree = ast.parse(test_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            return []

        module_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_names.add(node.module)

        paths: list[Path] = []
        for module_name in sorted(module_names):
            module_path = Path(*module_name.split("."))
            options = (
                repository_path / module_path.with_suffix(".py"),
                repository_path / module_path / "__init__.py",
            )
            for option in options:
                resolved = self._resolve_repository_path(
                    repository_path,
                    option.relative_to(repository_path).as_posix(),
                )
                if resolved:
                    paths.append(resolved)
                    break
        return paths

    def _read_files(self, repository_path: Path, files: list[Path]) -> str:
        sections: list[str] = []
        used_characters = 0
        for path in files:
            if used_characters >= self.settings.max_source_characters:
                break
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(content) > self.settings.max_file_characters:
                content = (
                    content[: self.settings.max_file_characters]
                    + "\n# ... file truncated by FixFlow ..."
                )
            remaining = self.settings.max_source_characters - used_characters
            content = content[:remaining]
            relative = path.relative_to(repository_path).as_posix()
            section = f"\n--- FILE: {relative} ---\n{content}"
            sections.append(section)
            used_characters += len(section)
        return "\n".join(sections) or "No source files could be safely selected."

    @staticmethod
    def _build_prompt(
        repository_tree: str,
        test_result: TestResult,
        source_bundle: str,
    ) -> str:
        failure_summary = "\n".join(
            f"- {failure.test}: {failure.error}" for failure in test_result.failures
        )
        return f"""You are the analysis component of FixFlow Phase 1.
Analyze every pytest failure. Do not modify files and do not propose unrelated refactors.

Return only a JSON array. Each item must have exactly these keys:
test, what_failed, why, root_cause, file, symbol, suggested_fix, confidence.
confidence must be one of: low, medium, high.
Return one item for every failed test or collection error.

REPOSITORY STRUCTURE
{repository_tree}

PYTEST COUNTS
total={test_result.total}, passed={test_result.passed}, failed={test_result.failed}, skipped={test_result.skipped}, errors={test_result.errors}

ALL FAILURES
{failure_summary}

COMPLETE PYTEST OUTPUT AND STACK TRACES
{test_result.output}

RELEVANT PROJECT FILES
{source_bundle}
"""
