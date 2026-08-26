from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..core.exceptions import UnsupportedProjectError


DependencyStrategy = Literal[
    "requirements",
    "pyproject",
    "setup",
    "setup_cfg",
    "pipenv",
    "none",
]


@dataclass(frozen=True)
class PythonProject:
    language: Literal["python"]
    marker: str
    dependency_strategy: DependencyStrategy


class PythonDetector:
    MARKERS: tuple[tuple[str, DependencyStrategy], ...] = (
        ("requirements.txt", "requirements"),
        ("pyproject.toml", "pyproject"),
        ("setup.py", "setup"),
        ("setup.cfg", "setup_cfg"),
        ("Pipfile", "pipenv"),
    )

    def detect(self, repository_path: Path, *, job_id: str | None = None) -> PythonProject:
        for marker, strategy in self.MARKERS:
            if (repository_path / marker).is_file():
                return PythonProject(
                    language="python",
                    marker=marker,
                    dependency_strategy=strategy,
                )

        python_file = self._find_python_file(repository_path)
        if python_file:
            return PythonProject(
                language="python",
                marker=python_file.relative_to(repository_path).as_posix(),
                dependency_strategy="none",
            )

        raise UnsupportedProjectError(
            "The repository is not recognized as a Python project. "
            "Expected requirements.txt, pyproject.toml, setup.py, setup.cfg, "
            "Pipfile, or Python source files.",
            job_id=job_id,
        )

    @staticmethod
    def _find_python_file(repository_path: Path) -> Path | None:
        ignored = {".git", ".venv", "venv", "node_modules", "__pycache__"}
        for path in repository_path.rglob("*.py"):
            relative_parts = path.relative_to(repository_path).parts
            if not ignored.intersection(relative_parts) and len(relative_parts) <= 4:
                return path
        return None
