import pytest

from app.core.exceptions import UnsupportedProjectError
from app.services.python_detector import PythonDetector


def test_requirements_has_highest_dependency_priority(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    project = PythonDetector().detect(tmp_path)

    assert project.language == "python"
    assert project.dependency_strategy == "requirements"


def test_python_source_without_manifest_is_detected(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.py").write_text("print('hello')\n", encoding="utf-8")

    project = PythonDetector().detect(tmp_path)

    assert project.dependency_strategy == "none"
    assert project.marker == "src/main.py"


def test_non_python_repository_is_rejected(tmp_path):
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(UnsupportedProjectError):
        PythonDetector().detect(tmp_path, job_id="job-1")
