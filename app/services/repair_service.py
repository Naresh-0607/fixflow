from collections.abc import Callable

from ..agent.repair_agent import RepairAgent
from ..core.config import Settings
from ..core.exceptions import (
    GeminiAnalysisError,
    PatchApplicationError,
    UnsafeModificationError,
)
from ..core.logging import log_stage
from ..models.responses import (
    GitState,
    QAAnalysisItem,
    RepairIteration,
    RepairResponse,
    TestComparison,
    TestResult,
)
from ..tools.repository_tools import RepositoryTools


class RepairService:
    def __init__(
        self,
        *,
        settings: Settings,
        repair_agent: RepairAgent,
        repository_tools: RepositoryTools,
        run_tests: Callable[[], TestResult],
    ) -> None:
        self.settings = settings
        self.repair_agent = repair_agent
        self.repository_tools = repository_tools
        self.run_tests = run_tests

    def repair(
        self,
        *,
        job_id: str,
        repository_name: str,
        initial_tests: TestResult,
        initial_analysis: list[QAAnalysisItem],
        git_state: GitState,
    ) -> RepairResponse:
        if initial_tests.status == "passed":
            return self._response(
                job_id=job_id,
                repository_name=repository_name,
                status="already_passing",
                initial_tests=initial_tests,
                final_tests=initial_tests,
                initial_analysis=initial_analysis,
                history=[],
                git_state=git_state,
                message="All tests already pass; no files were modified.",
            )
        if initial_tests.status == "no_tests":
            return self._response(
                job_id=job_id,
                repository_name=repository_name,
                status="no_tests",
                initial_tests=initial_tests,
                final_tests=initial_tests,
                initial_analysis=initial_analysis,
                history=[],
                git_state=git_state,
                message="pytest completed, but no tests were collected.",
            )

        current_tests = initial_tests
        history: list[RepairIteration] = []
        for iteration in range(1, self.settings.max_iterations + 1):
            if history:
                log_stage(job_id, "iteration=%s reflection started", iteration)
            log_stage(job_id, "iteration=%s Gemini repair requested", iteration)
            try:
                plan = self.repair_agent.propose(
                    job_id=job_id,
                    repository_path=self.repository_tools.repository_path,
                    iteration=iteration,
                    current_tests=current_tests,
                    initial_analysis=initial_analysis,
                    history=history.copy(),
                    current_diff=self.repository_tools.get_git_diff(),
                )
                log_stage(job_id, "iteration=%s patch generated", iteration)
            except GeminiAnalysisError as exc:
                return self._response(
                    job_id=job_id,
                    repository_name=repository_name,
                    status="gemini_unavailable",
                    initial_tests=initial_tests,
                    final_tests=current_tests,
                    initial_analysis=initial_analysis,
                    history=history,
                    git_state=git_state,
                    message="Repair stopped because Gemini was unavailable.",
                    repair_error=exc.message,
                )

            try:
                for change in plan.changes:
                    log_stage(
                        job_id,
                        "iteration=%s applying patch file=%s",
                        iteration,
                        change.file,
                    )
                transaction = self.repository_tools.apply_patch(plan.changes)
                log_stage(job_id, "iteration=%s patch applied", iteration)
            except (UnsafeModificationError, PatchApplicationError) as exc:
                log_stage(
                    job_id,
                    "iteration=%s unsafe patch rejected reason=%s",
                    iteration,
                    exc.message,
                )
                return self._response(
                    job_id=job_id,
                    repository_name=repository_name,
                    status="unsafe_change",
                    initial_tests=initial_tests,
                    final_tests=current_tests,
                    initial_analysis=initial_analysis,
                    history=history,
                    git_state=git_state,
                    message="Repair stopped before an unsafe or invalid patch was applied.",
                    repair_error=exc.message,
                )

            attempted_diff = self.repository_tools.get_git_diff()
            log_stage(job_id, "iteration=%s pytest started", iteration)
            try:
                after_tests = self.run_tests()
            except Exception:
                self.repository_tools.restore_transaction(transaction)
                log_stage(
                    job_id,
                    "iteration=%s test execution failed; repository restored",
                    iteration,
                )
                raise
            log_stage(
                job_id,
                "iteration=%s pytest failed=%s errors=%s",
                iteration,
                after_tests.failed,
                after_tests.errors,
            )
            result = self.compare(current_tests, after_tests)
            accepted = result != "regressed"
            comparison = TestComparison(
                iteration=iteration,
                before=current_tests,
                after=after_tests,
                result=result,
            )
            record = RepairIteration(
                iteration=iteration,
                changes=plan.changes,
                explanation=plan.explanation,
                comparison=comparison,
                accepted=accepted,
                diff=attempted_diff,
            )
            history.append(record)
            log_stage(job_id, "iteration=%s result=%s", iteration, result)

            if result == "regressed":
                log_stage(job_id, "iteration=%s regression detected", iteration)
                log_stage(job_id, "iteration=%s reverting changes", iteration)
                self.repository_tools.restore_transaction(transaction)
                log_stage(job_id, "iteration=%s repository restored", iteration)
                log_stage(job_id, "iteration=%s requesting reflection", iteration)
                continue

            current_tests = after_tests
            if result == "passed":
                log_stage(job_id, "repair completed failed=0")
                return self._response(
                    job_id=job_id,
                    repository_name=repository_name,
                    status="fixed",
                    initial_tests=initial_tests,
                    final_tests=current_tests,
                    initial_analysis=initial_analysis,
                    history=history,
                    git_state=git_state,
                    message="All tests pass after autonomous repair.",
                )

        log_stage(
            job_id,
            "repair stopped maximum_iterations=%s failed=%s errors=%s",
            self.settings.max_iterations,
            current_tests.failed,
            current_tests.errors,
        )
        return self._response(
            job_id=job_id,
            repository_name=repository_name,
            status="partial",
            initial_tests=initial_tests,
            final_tests=current_tests,
            initial_analysis=initial_analysis,
            history=history,
            git_state=git_state,
            message="The maximum repair iteration limit was reached.",
        )

    @staticmethod
    def compare(
        before: TestResult,
        after: TestResult,
    ) -> str:
        before_defects = before.failed + before.errors
        after_defects = after.failed + after.errors
        if after.status in {"no_tests", "error", "timeout"}:
            return "regressed"
        if after.total < before.total:
            return "regressed"
        if after.status == "passed" and after_defects == 0:
            return "passed"
        if after_defects > before_defects:
            return "regressed"
        if after_defects < before_defects:
            return "improved"
        if after.passed < before.passed:
            return "regressed"
        if after.passed > before.passed:
            return "improved"
        return "unchanged"

    def _response(
        self,
        *,
        job_id: str,
        repository_name: str,
        status: str,
        initial_tests: TestResult,
        final_tests: TestResult,
        initial_analysis: list[QAAnalysisItem],
        history: list[RepairIteration],
        git_state: GitState,
        message: str,
        repair_error: str | None = None,
    ) -> RepairResponse:
        diff_stats = self.repository_tools.diff_stats()
        return RepairResponse(
            job_id=job_id,
            repository=repository_name,
            status=status,
            iterations=len(history),
            initial_tests=initial_tests,
            final_tests=final_tests,
            initial_analysis=initial_analysis,
            modified_files=self.repository_tools.changed_files(),
            additions=int(diff_stats["additions"]),
            deletions=int(diff_stats["deletions"]),
            diff=self.repository_tools.get_git_diff(),
            repairs=history,
            git=git_state,
            message=message,
            repair_error=repair_error,
        )
