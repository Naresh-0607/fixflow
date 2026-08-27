import ast
import io
import re
import zipfile
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import files as files_api
from app.api import repositories as repositories_api
from app.core.config import PROJECT_ROOT, Settings
from app.core.exceptions import MercuryRepairError, RepositoryFixError
from app.main import app
from app.models.repositories import RepositoryAnalyzeResponse, RepositoryIssue
from app.models.responses import AnalyzeResponse
from app.models.responses import TestFailure as PytestFailure
from app.models.responses import TestResult as PytestResult
from app.services.repository_service import RepositoryService


class SequenceProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def analyze(self, prompt, *, job_id):
        self.prompts.append((job_id, prompt))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def repository_issue(
    *,
    issue_id="issue-1",
    line=2,
    source="gemini",
    rule="QA-FAILURE",
    message="Wrong result",
):
    return RepositoryIssue(
        issue_id=issue_id,
        file="app.py",
        line=line,
        end_line=line,
        severity="high",
        source=source,
        rule=rule,
        category="logic" if source == "gemini" else "code_quality",
        message=message,
        fixable=True,
        test="test_app.py::test_result",
        what_failed=message,
        why=message,
        root_cause=message,
        symbol="answer",
        suggested_fix="Correct the selected issue.",
        confidence="high",
    )


def seed_repository(test_settings, source, issues):
    job = RepositoryService(test_settings).create_managed_job(
        "https://github.com/openai/example.git"
    )
    job.repository_path.mkdir()
    job.original_path.mkdir()
    (job.repository_path / "app.py").write_text(source, encoding="utf-8")
    (job.original_path / "app.py").write_text(source, encoding="utf-8")
    record = repositories_api.repository_store.add(job)
    tests = PytestResult(status="failed", total=1, failed=1)
    record.analysis = RepositoryAnalyzeResponse(
        repo_id=job.job_id,
        job_id=job.job_id,
        repository=job.repository_name,
        tests=tests,
        status="issues_found",
        analysis=issues,
        issue_count=len(issues),
        severity_counts={"critical": 0, "high": len(issues), "medium": 0, "low": 0},
    )
    record.status = "completed"
    return record


def install_providers(monkeypatch, test_settings, mercury, gemini):
    configured = replace(test_settings, inception_api_key="inception-test-key")
    monkeypatch.setattr(repositories_api, "settings", configured)
    monkeypatch.setattr(files_api, "settings", configured)
    monkeypatch.setattr(files_api, "_mercury_client", lambda: mercury)
    monkeypatch.setattr(files_api, "_gemini_client", lambda **_: gemini)
    monkeypatch.setattr(repositories_api, "_gemini_client", lambda **_: gemini)
    return TestClient(app)


def test_mercury_repo_fix_is_targeted_and_preserves_original(
    monkeypatch, test_settings
):
    source = (
        "def answer():\n    return 0\n\n"
        + "\n".join(f"unrelated_{line} = {line}" for line in range(4, 220))
        + "\n"
    )
    issue = repository_issue()
    record = seed_repository(test_settings, source, [issue])
    mercury = SequenceProvider(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return 42",
            "summary": "Correct the result.",
        }
    )
    gemini = SequenceProvider()
    client = install_providers(monkeypatch, test_settings, mercury, gemini)

    response = client.post(
        f"/api/repositories/{record.job.job_id}/fix",
        json={"issue_id": issue.issue_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["repair_provider"] == "mercury"
    assert payload["remaining_issue_count"] == len(
        payload["updated_analysis"]["analysis"]
    )
    assert "return 42" in (record.job.repository_path / "app.py").read_text()
    assert (record.job.original_path / "app.py").read_text() == source
    assert len(mercury.prompts) == 1
    assert "L2:     return 0" in mercury.prompts[0][1]
    assert "unrelated_200" not in mercury.prompts[0][1]
    ast.parse((record.job.repository_path / "app.py").read_text())


def test_ruff_repo_fix_does_not_call_ai(monkeypatch, test_settings):
    source = "import os\nanswer = 42\n"
    issue = repository_issue(
        line=1,
        source="ruff",
        rule="F401",
        message='"os" imported but unused',
    )
    record = seed_repository(test_settings, source, [issue])
    mercury = SequenceProvider(AssertionError("Mercury must not run"))
    gemini = SequenceProvider(AssertionError("Gemini must not run"))
    client = install_providers(monkeypatch, test_settings, mercury, gemini)

    response = client.post(
        f"/api/repositories/{record.job.job_id}/fix",
        json={"issue_id": issue.issue_id},
    )

    assert response.status_code == 200
    assert response.json()["repair_provider"] == "ruff"
    assert "import os" not in (record.job.repository_path / "app.py").read_text()
    assert (record.job.original_path / "app.py").read_text() == source
    assert mercury.prompts == []
    assert gemini.prompts == []


def test_repo_fix_uses_gemini_fallback(monkeypatch, test_settings):
    issue = repository_issue()
    record = seed_repository(test_settings, "def answer():\n    return 0\n", [issue])
    mercury = SequenceProvider(MercuryRepairError("Mercury timed out."))
    gemini = SequenceProvider(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return 42",
            "summary": "Fallback repair.",
        }
    )
    client = install_providers(monkeypatch, test_settings, mercury, gemini)

    response = client.post(
        f"/api/repositories/{record.job.job_id}/fix",
        json={"issue_id": issue.issue_id},
    )

    assert response.status_code == 200
    assert response.json()["repair_provider"] == "gemini"
    assert len(mercury.prompts) == len(gemini.prompts) == 1


def test_invalid_and_noop_repo_fixes_fail_without_writes(monkeypatch, test_settings):
    source = "def answer():\n    return 0\n"
    issue = repository_issue()
    record = seed_repository(test_settings, source, [issue])
    mercury = SequenceProvider(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return (",
            "summary": "Bad",
        }
    )
    gemini = SequenceProvider(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return 0",
            "summary": "No-op",
        }
    )
    client = install_providers(monkeypatch, test_settings, mercury, gemini)

    response = client.post(
        f"/api/repositories/{record.job.job_id}/fix",
        json={"issue_id": issue.issue_id},
    )

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert response.json()["reason"]
    assert (record.job.repository_path / "app.py").read_text() == source
    assert (record.job.original_path / "app.py").read_text() == source


def test_sequential_repo_fixes_use_latest_current_and_zip(monkeypatch, test_settings):
    source = "import os\ndef answer():\n    return 0\n"
    safe = repository_issue(
        issue_id="safe",
        line=1,
        source="ruff",
        rule="F401",
        message='"os" imported but unused',
    )
    reasoning = repository_issue(
        issue_id="reasoning", line=3, message="Answer result is wrong"
    )
    record = seed_repository(test_settings, source, [safe, reasoning])
    mercury = SequenceProvider(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return 42",
            "summary": "Answer",
        },
    )
    client = install_providers(monkeypatch, test_settings, mercury, SequenceProvider())

    before = client.get(f"/api/repositories/{record.job.job_id}/download")
    repaired = client.post(f"/api/repositories/{record.job.job_id}/repair")
    archive = client.get(f"/api/repositories/{record.job.job_id}/download")

    assert before.status_code == 422
    assert repaired.status_code == archive.status_code == 200
    payload = repaired.json()
    assert payload["fixed"] == 2
    assert payload["failed"] == 0
    assert [item["repair_provider"] for item in payload["results"]] == [
        "ruff",
        "mercury",
    ]
    assert "import os" not in mercury.prompts[0][1]
    assert (record.job.original_path / "app.py").read_text() == source
    with zipfile.ZipFile(io.BytesIO(archive.content)) as result:
        fixed = result.read("app.py").decode()
    assert "import os" not in fixed and "return 42" in fixed
    stages = [event.stage for event in record.events]
    assert stages[0] == "repair_started"
    assert stages.count("issue_fix_started") == 2
    assert stages.count("issue_fixed") == 2
    assert stages[-3:] == [
        "zip_started",
        "zip_created",
        "repair_completed",
    ]


def test_bulk_repair_continues_after_failed_issue(monkeypatch, test_settings):
    source = "def first():\n    return 0\n\ndef second():\n    return 0\n"
    first = repository_issue(issue_id="first", line=2, message="First result is wrong")
    second = repository_issue(
        issue_id="second", line=5, message="Second result is wrong"
    )
    record = seed_repository(test_settings, source, [first, second])
    mercury = SequenceProvider(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return (",
            "summary": "Invalid",
        },
        {
            "start_line": 5,
            "end_line": 5,
            "replacement": "    return 2",
            "summary": "Second",
        },
    )
    gemini = SequenceProvider(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return 0",
            "summary": "No-op",
        },
    )
    client = install_providers(monkeypatch, test_settings, mercury, gemini)

    response = client.post(f"/api/repositories/{record.job.job_id}/repair")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["fixed"] == 1
    assert payload["failed"] == 1
    assert payload["zip_ready"] is True
    current = (record.job.repository_path / "app.py").read_text()
    assert "def first():\n    return 0" in current
    assert "def second():\n    return 2" in current
    assert (record.job.original_path / "app.py").read_text() == source
    stages = [event.stage for event in record.events]
    assert "issue_failed" in stages
    assert stages[-1] == "repair_completed"


def test_repository_progress_events_are_real_and_ordered(monkeypatch, test_settings):
    monkeypatch.setattr(repositories_api, "settings", test_settings)
    record = seed_repository(test_settings, "value = 1\n", [])
    record.analysis = None
    record.status = "ready"

    def fake_analysis(job, callback, *, emit_completed):
        assert emit_completed is False
        for event in (
            ("clone_started", "Cloning repository...", 10),
            ("clone_completed", "Repository cloned", 20),
            ("workspace_created", "Protected workspace prepared", 25),
            ("container_started", "Preparing Docker container...", 35),
            ("container_ready", "Docker container ready", 55),
            ("scan_started", "Running repository tests...", 62),
            ("scan_completed", "Repository tests completed", 76),
        ):
            callback(*event)
        return AnalyzeResponse(
            job_id=job.job_id,
            repository=job.repository_name,
            language="python",
            docker_status="success",
            tests=PytestResult(status="passed", total=1, passed=1),
            status="tests_passed",
        )

    async def fake_scan(record, engine):
        record.emit("file_scanned", "Scanned 1 / 1 Python files", 99)
        return []

    monkeypatch.setattr(repositories_api, "run_analysis_job", fake_analysis)
    monkeypatch.setattr(repositories_api, "_scan_repository", fake_scan)
    client = TestClient(app)

    response = client.post(f"/api/repositories/{record.job.job_id}/analyze")

    assert response.status_code == 200
    stages = [event.stage for event in record.events]
    assert stages == [
        "clone_started",
        "clone_completed",
        "workspace_created",
        "container_started",
        "container_ready",
        "scan_started",
        "scan_completed",
        "static_analysis_started",
        "file_scanned",
        "completed",
    ]


def test_failing_pytest_analysis_returns_200_persists_and_can_repair(
    monkeypatch, test_settings
):
    source = "def answer():\n    return 0\n"
    record = seed_repository(test_settings, source, [])
    record.analysis = None
    record.status = "ready"
    failure = PytestFailure(
        test="tests/test_app.py::test_answer",
        file="app.py",
        error="AssertionError: expected 42",
        trace="app.py:2: AssertionError",
    )

    def fake_analysis(job, callback, *, emit_completed):
        return AnalyzeResponse(
            job_id=job.job_id,
            repository=job.repository_name,
            language="python",
            docker_status="success",
            tests=PytestResult(
                status="failed",
                exit_code=1,
                total=1,
                failed=1,
                failures=[failure],
            ),
            status="analysis_failed",
            analysis_error="AI unavailable",
        )

    async def fake_scan(record, engine, *, emit_progress=True):
        return []

    mercury = SequenceProvider(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return 42",
            "summary": "Correct the failed test.",
        }
    )
    client = install_providers(monkeypatch, test_settings, mercury, SequenceProvider())
    monkeypatch.setattr(repositories_api, "run_analysis_job", fake_analysis)
    monkeypatch.setattr(repositories_api, "_scan_repository", fake_scan)

    analyzed = client.post(f"/api/repositories/{record.job.job_id}/analyze")

    assert analyzed.status_code == 200
    payload = analyzed.json()
    assert payload["tests"]["exit_code"] == 1
    assert payload["tests"]["status"] == "failed"
    assert payload["analysis"][0]["source"] == "pytest"
    assert payload["analysis"][0]["category"] == "test_failure"
    assert payload["analysis"][0]["line"] == 2
    assert record.status == "completed"
    assert (record.job.workspace / "repository-state.json").is_file()
    assert record.job.original_path.is_dir()
    assert record.job.repository_path.is_dir()

    repaired = client.post(f"/api/repositories/{record.job.job_id}/repair")

    assert repaired.status_code == 200
    assert repaired.json()["fixed"] == 1
    assert "return 42" in (record.job.repository_path / "app.py").read_text()
    assert (record.job.original_path / "app.py").read_text() == source


def test_no_tests_repository_analysis_returns_200(monkeypatch, test_settings):
    record = seed_repository(test_settings, "value = 1\n", [])
    record.analysis = None
    record.status = "ready"

    def fake_analysis(job, callback, *, emit_completed):
        return AnalyzeResponse(
            job_id=job.job_id,
            repository=job.repository_name,
            language="python",
            docker_status="success",
            tests=PytestResult(status="no_tests", exit_code=5),
            status="no_tests",
            message="pytest completed, but no tests were collected.",
        )

    async def fake_scan(record, engine, *, emit_progress=True):
        return []

    monkeypatch.setattr(repositories_api, "settings", test_settings)
    monkeypatch.setattr(repositories_api, "run_analysis_job", fake_analysis)
    monkeypatch.setattr(repositories_api, "_scan_repository", fake_scan)
    response = TestClient(app).post(f"/api/repositories/{record.job.job_id}/analyze")

    assert response.status_code == 200
    assert response.json()["status"] == "no_tests"
    assert response.json()["tests"]["exit_code"] == 5


def test_multiple_pytest_failures_with_missing_metadata_return_200(
    monkeypatch, test_settings
):
    record = seed_repository(test_settings, "value = 1\n", [])
    record.analysis = None
    record.status = "ready"
    failures = [
        PytestFailure(
            test="tests/test_one.py::test_one",
            file="app.py",
            error="AssertionError",
        ),
        PytestFailure(
            test="pytest::unparsed_failure",
            file="",
            error="Detailed failure information could not be parsed.",
        ),
    ]

    def fake_analysis(job, callback, *, emit_completed):
        return AnalyzeResponse(
            job_id=job.job_id,
            repository=job.repository_name,
            language="python",
            docker_status="success",
            tests=PytestResult(
                status="failed",
                exit_code=1,
                total=2,
                failed=2,
                failures=failures,
            ),
            status="analysis_failed",
        )

    async def fake_scan(record, engine, *, emit_progress=True):
        return []

    monkeypatch.setattr(repositories_api, "settings", test_settings)
    monkeypatch.setattr(repositories_api, "run_analysis_job", fake_analysis)
    monkeypatch.setattr(repositories_api, "_scan_repository", fake_scan)
    response = TestClient(app).post(f"/api/repositories/{record.job.job_id}/analyze")

    assert response.status_code == 200
    issues = response.json()["analysis"]
    assert len(issues) == 2
    assert all(issue["source"] == "pytest" for issue in issues)
    assert all(issue["line"] >= 1 for issue in issues)
    assert (
        next(issue for issue in issues if issue["file"] == "pytest")["fixable"] is False
    )


def test_repository_failure_event_keeps_real_progress(monkeypatch, test_settings):
    monkeypatch.setattr(repositories_api, "settings", test_settings)
    record = seed_repository(test_settings, "value = 1\n", [])
    record.analysis = None
    record.status = "ready"

    def failing_analysis(job, callback, *, emit_completed):
        callback("clone_started", "Cloning repository...", 10)
        raise RepositoryFixError("Clone preparation failed.", job_id=job.job_id)

    monkeypatch.setattr(repositories_api, "run_analysis_job", failing_analysis)
    response = TestClient(app).post(f"/api/repositories/{record.job.job_id}/analyze")

    assert response.status_code == 422
    assert record.events[-1].stage == "failed"
    assert record.events[-1].progress == 10
    assert record.status == "failed"


def test_repository_frontend_has_one_fix_button_progress_and_shared_sidebar():
    root = Path(__file__).parents[1]
    repository_page = (root / "frontend" / "index.html").read_text(encoding="utf-8")
    file_page = (root / "frontend" / "file-analyzer.html").read_text(encoding="utf-8")
    script = (root / "frontend" / "app.js").read_text(encoding="utf-8")
    label_pattern = re.compile(
        r'class="nav-link[^"]*"[^>]*>.*?<span>(.*?)</span>', re.DOTALL
    )

    assert label_pattern.findall(repository_page) == ["Repository QA", "File Analyzer"]
    assert label_pattern.findall(file_page) == ["Repository QA", "File Analyzer"]
    assert 'class="nav-link active" href="/"' in repository_page
    assert 'class="nav-link active" href="/file-analyzer"' in file_page
    issue_template = repository_page.split('<template id="issue-template">', 1)[1]
    assert repository_page.count('id="fix-repository"') == 1
    assert "Fix Repository" in repository_page
    assert "View Code" in issue_template
    assert "fix-repo-issue" not in issue_template
    assert "Fix Now" not in issue_template
    assert (
        "new EventSource(`/api/repositories/${repoId}/events?phase=${phase}`)" in script
    )
    assert "`/api/repositories/${state.repoId}/repair`" in script
    assert 'event.stage === "issue_fix_started"' in script
    assert 'event.stage === "repair_failed"' in script
    assert "download.hidden = true" in script


def test_managed_repository_recovers_after_in_memory_store_is_lost(
    monkeypatch, test_settings
):
    issue = repository_issue()
    record = seed_repository(test_settings, "def answer():\n    return 0\n", [issue])
    repositories_api.repository_store.save(record)
    fresh_store = repositories_api.ManagedRepositoryStore()
    monkeypatch.setattr(repositories_api, "repository_store", fresh_store)
    mercury = SequenceProvider(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return 42",
            "summary": "Recovered repair.",
        }
    )
    client = install_providers(
        monkeypatch,
        test_settings,
        mercury,
        SequenceProvider(),
    )

    response = client.post(f"/api/repositories/{record.job.job_id}/repair")

    assert response.status_code == 200
    assert response.json()["fixed"] == 1
    assert "return 42" in (record.job.repository_path / "app.py").read_text()
    assert (
        record.job.original_path / "app.py"
    ).read_text() == "def answer():\n    return 0\n"


def test_default_workspace_is_outside_reload_watched_source_tree(monkeypatch):
    monkeypatch.delenv("FIXFLOW_WORKSPACE_ROOT", raising=False)

    configured = Settings.from_environment()

    assert not configured.workspace_root.is_relative_to(PROJECT_ROOT)
