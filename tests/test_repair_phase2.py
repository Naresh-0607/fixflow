import subprocess
from dataclasses import replace

import httpx
import pytest

from app.core.exceptions import GeminiAnalysisError, UnsafeModificationError
from app.llm.gemini_client import GeminiClient
from app.models.responses import GitState, RepairChange, RepairPlan, TestResult as ResultModel
from app.services.repair_service import RepairService
from app.tools.repository_tools import RepositoryTools


def result(*, passed: int, failed: int, status: str | None = None) -> ResultModel:
    total = passed + failed
    return ResultModel(
        total=total,
        passed=passed,
        failed=failed,
        status=status or ("passed" if failed == 0 else "failed"),
        exit_code=0 if failed == 0 else 1,
        output=f"{passed} passed, {failed} failed",
    )


class SequenceAgent:
    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = []

    def propose(self, **kwargs):
        self.calls.append(kwargs)
        plan = self.plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        return plan


class SequenceRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.results.pop(0)


def patch(old: str, new: str) -> RepairPlan:
    return RepairPlan(
        changes=[
            RepairChange(
                file="app.py",
                reason="Correct the returned value.",
                patch=(
                    "--- a/app.py\n"
                    "+++ b/app.py\n"
                    "@@ -1,2 +1,2 @@\n"
                    " def value():\n"
                    f"-    return {old}\n"
                    f"+    return {new}\n"
                ),
            )
        ],
        explanation="Apply the smallest source repair.",
    )


@pytest.fixture()
def repair_repository(tmp_path, test_settings):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_bytes(b"def value():\n    return 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixflow@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "FixFlow Tests"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "app.py"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repository, check=True
    )
    tools = RepositoryTools(repository, test_settings)
    state = GitState(
        initial_commit=tools.initial_commit(),
        initial_status=tools.git_status(),
        initial_diff=tools.get_git_diff(),
    )
    return repository, tools, state


def service(test_settings, tools, agent, runner, *, iterations=5):
    return RepairService(
        settings=replace(test_settings, max_iterations=iterations),
        repair_agent=agent,
        repository_tools=tools,
        run_tests=runner,
    )


def run(service, state, initial=None):
    return service.repair(
        job_id="job-phase2",
        repository_name="example",
        initial_tests=initial or result(passed=0, failed=1),
        initial_analysis=[],
        git_state=state,
    )


def test_successful_repair(repair_repository, test_settings):
    repository, tools, state = repair_repository
    agent = SequenceAgent([patch("1", "2")])
    runner = SequenceRunner([result(passed=1, failed=0)])

    response = run(service(test_settings, tools, agent, runner), state)

    assert response.status == "fixed"
    assert response.iterations == 1
    assert response.final_tests.passed == 1
    assert response.modified_files == ["app.py"]
    assert response.additions == 1
    assert response.deletions == 1
    assert "return 2" in (repository / "app.py").read_text(encoding="utf-8")


def test_multiple_repair_iterations_include_reflection_context(
    repair_repository, test_settings
):
    repository, tools, state = repair_repository
    agent = SequenceAgent([patch("1", "2"), patch("2", "3")])
    runner = SequenceRunner(
        [result(passed=1, failed=1), result(passed=2, failed=0)]
    )

    response = run(
        service(test_settings, tools, agent, runner),
        state,
        result(passed=0, failed=2),
    )

    assert response.status == "fixed"
    assert [item.comparison.result for item in response.repairs] == [
        "improved",
        "passed",
    ]
    assert len(agent.calls[1]["history"]) == 1
    assert "return 3" in (repository / "app.py").read_text(encoding="utf-8")


def test_regression_is_rolled_back_before_reflection(
    repair_repository, test_settings
):
    repository, tools, state = repair_repository
    agent = SequenceAgent([patch("1", "0"), patch("1", "2")])
    runner = SequenceRunner(
        [result(passed=0, failed=2), result(passed=1, failed=0)]
    )

    response = run(
        service(test_settings, tools, agent, runner, iterations=2), state
    )

    assert response.status == "fixed"
    assert response.repairs[0].comparison.result == "regressed"
    assert response.repairs[0].accepted is False
    assert "return 2" in (repository / "app.py").read_text(encoding="utf-8")


def test_unchanged_result_is_recorded_at_iteration_limit(
    repair_repository, test_settings
):
    repository, tools, state = repair_repository
    unchanged = RepairPlan(
        changes=[
            RepairChange(
                file="app.py",
                reason="Document the function.",
                patch=(
                    "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,3 @@\n"
                    " def value():\n+    # Return the value.\n     return 1\n"
                ),
            )
        ],
        explanation="Documentation-only attempt.",
    )
    response = run(
        service(
            test_settings,
            tools,
            SequenceAgent([unchanged]),
            SequenceRunner([result(passed=0, failed=1)]),
            iterations=1,
        ),
        state,
    )

    assert response.status == "partial"
    assert response.repairs[0].comparison.result == "unchanged"
    assert response.repairs[0].accepted is True
    assert "# Return the value." in repository.joinpath("app.py").read_text()


def test_invalid_patch_stops_without_modifying_repository(
    repair_repository, test_settings
):
    repository, tools, state = repair_repository
    invalid = RepairPlan(
        changes=[
            RepairChange(
                file="app.py",
                reason="Invalid proposal.",
                patch="this is not a unified diff",
            )
        ],
        explanation="Invalid.",
    )

    response = run(
        service(
            test_settings,
            tools,
            SequenceAgent([invalid]),
            SequenceRunner([]),
        ),
        state,
    )

    assert response.status == "unsafe_change"
    assert response.iterations == 0
    assert repository.joinpath("app.py").read_bytes().endswith(b"return 1\n")


@pytest.mark.parametrize("unsafe_path", ["../../outside.py", "/etc/passwd", "C:\\boot.ini"])
def test_unsafe_paths_are_rejected(repair_repository, unsafe_path):
    _, tools, _ = repair_repository
    change = RepairChange(
        file=unsafe_path,
        reason="Escape the workspace.",
        patch="@@ -1 +1 @@\n-old\n+new\n",
    )

    with pytest.raises(UnsafeModificationError):
        tools.apply_patch([change])


def test_gemini_retries_temporary_failure_without_rerunning_tests(monkeypatch):
    calls = {"count": 0}

    class TemporaryClient:
        def __init__(self, *, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            calls["count"] += 1
            request = httpx.Request("POST", "https://example.invalid")
            if calls["count"] == 1:
                return httpx.Response(503, request=request, text="temporary")
            return httpx.Response(
                200,
                request=request,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": '{"ok": true}'}]}}
                    ]
                },
            )

    monkeypatch.setattr("app.llm.gemini_client.httpx.Client", TemporaryClient)
    client = GeminiClient(
        api_key="test-key",
        model="gemini-test",
        max_retries=2,
        retry_backoff_seconds=0,
    )

    assert client.analyze("repair", job_id="retry-job") == {"ok": True}
    assert calls["count"] == 2


def test_gemini_failure_stops_safely(repair_repository, test_settings):
    _, tools, state = repair_repository
    unavailable = GeminiAnalysisError("Gemini unavailable", job_id="job-phase2")

    response = run(
        service(
            test_settings,
            tools,
            SequenceAgent([unavailable]),
            SequenceRunner([]),
        ),
        state,
    )

    assert response.status == "gemini_unavailable"
    assert response.modified_files == []


def test_all_tests_already_passing_skips_gemini_and_retest(
    repair_repository, test_settings
):
    _, tools, state = repair_repository
    agent = SequenceAgent([])
    runner = SequenceRunner([])

    response = run(
        service(test_settings, tools, agent, runner),
        state,
        result(passed=3, failed=0),
    )

    assert response.status == "already_passing"
    assert response.iterations == 0
    assert agent.calls == []
    assert runner.calls == 0
