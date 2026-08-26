import json
from pathlib import Path

from pydantic import ValidationError

from ..core.config import Settings
from ..core.exceptions import GeminiAnalysisError
from ..llm.gemini_client import GeminiClient
from ..models.responses import (
    QAAnalysisItem,
    RepairIteration,
    RepairPlan,
    TestResult,
)
from ..services.analysis_service import AnalysisService


class RepairAgent:
    """Builds bounded code context and validates Gemini's repair proposal."""

    def __init__(self, settings: Settings, gemini_client: GeminiClient) -> None:
        self.settings = settings
        self.gemini_client = gemini_client
        self.context_service = AnalysisService(settings, gemini_client)

    def propose(
        self,
        *,
        job_id: str,
        repository_path: Path,
        iteration: int,
        current_tests: TestResult,
        initial_analysis: list[QAAnalysisItem],
        history: list[RepairIteration],
        current_diff: str,
    ) -> RepairPlan:
        tree, source_bundle = self.context_service.repository_context(
            repository_path,
            current_tests,
        )
        prompt = self._build_prompt(
            iteration=iteration,
            tree=tree,
            source_bundle=source_bundle,
            current_tests=current_tests,
            initial_analysis=initial_analysis,
            history=history,
            current_diff=current_diff,
        )
        raw_plan = self.gemini_client.analyze(prompt, job_id=job_id)
        if isinstance(raw_plan, dict) and "repair" in raw_plan:
            raw_plan = raw_plan["repair"]
        try:
            return RepairPlan.model_validate(raw_plan)
        except ValidationError as exc:
            raise GeminiAnalysisError(
                "Gemini returned JSON that does not match the repair schema.",
                job_id=job_id,
                details={"validation_error": str(exc)},
            ) from exc

    @staticmethod
    def _build_prompt(
        *,
        iteration: int,
        tree: str,
        source_bundle: str,
        current_tests: TestResult,
        initial_analysis: list[QAAnalysisItem],
        history: list[RepairIteration],
        current_diff: str,
    ) -> str:
        history_payload = []
        for item in history:
            history_payload.append(
                {
                    "iteration": item.iteration,
                    "changes": [
                        {"file": change.file, "reason": change.reason}
                        for change in item.changes
                    ],
                    "before": RepairAgent._test_summary(
                        item.comparison.before
                    ),
                    "after": RepairAgent._test_summary(item.comparison.after),
                    "result": item.comparison.result,
                    "accepted": item.accepted,
                    "after_failures": [
                        failure.model_dump()
                        for failure in item.comparison.after.failures
                    ],
                }
            )
        previous_output = (
            history[-1].comparison.after.output if history else "No previous attempt."
        )
        return f"""You are the repair planner for FixFlow Phase 2, iteration {iteration}.
Return only one JSON object with exactly these top-level keys:
changes, explanation.
changes must be a non-empty array. Every change must contain exactly:
file, reason, patch.

Each patch must be a valid unified diff for exactly the declared existing file,
using --- a/path and +++ b/path headers plus @@ hunks. Make the smallest repair
that addresses the observed failures. Do not create, delete, rename, or modify
binary files. Do not modify tests merely to hide failures. Do not repeat a
previously unsuccessful repair. You have no shell access; FixFlow will validate
and apply the diff, then run the complete pytest -v suite in Docker.

INITIAL ROOT-CAUSE ANALYSIS
{json.dumps([item.model_dump() for item in initial_analysis], indent=2)}

REPOSITORY STRUCTURE
{tree}

CURRENT PYTEST RESULT
{json.dumps(RepairAgent._test_summary(current_tests), indent=2)}

CURRENT COMPLETE PYTEST OUTPUT AND TRACES
{current_tests.output}

PREVIOUS REPAIR ATTEMPTS
{json.dumps(history_payload, indent=2)}

MOST RECENT ATTEMPT PYTEST OUTPUT
{previous_output}

CURRENT ACCEPTED GIT DIFF
{current_diff or "No accepted modifications yet."}

RELEVANT TEST AND SOURCE FILES (CURRENT RESTORED STATE)
{source_bundle}
"""

    @staticmethod
    def _test_summary(test_result: TestResult) -> dict[str, object]:
        return {
            "total": test_result.total,
            "passed": test_result.passed,
            "failed": test_result.failed,
            "skipped": test_result.skipped,
            "errors": test_result.errors,
            "status": test_result.status,
            "exit_code": test_result.exit_code,
            "failures": [
                failure.model_dump() for failure in test_result.failures
            ],
        }
