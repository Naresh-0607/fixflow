from typing import Literal

from pydantic import BaseModel, Field


class TestFailure(BaseModel):
    test: str
    file: str
    error: str
    trace: str = ""


class TestResult(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    status: Literal["passed", "failed", "no_tests", "error", "timeout"]
    exit_code: int | None = None
    failures: list[TestFailure] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    output: str = ""


class QAAnalysisItem(BaseModel):
    test: str
    what_failed: str
    why: str
    root_cause: str
    file: str
    symbol: str
    suggested_fix: str
    confidence: Literal["low", "medium", "high"]


class AnalyzeResponse(BaseModel):
    job_id: str
    language: Literal["python"]
    repository: str
    docker_status: Literal["success", "failed"]
    tests: TestResult
    status: Literal[
        "issues_found",
        "tests_passed",
        "analysis_failed",
        "no_tests",
    ]
    analysis: list[QAAnalysisItem] = Field(default_factory=list)
    message: str | None = None
    analysis_error: str | None = None


class RepairChange(BaseModel):
    file: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    patch: str = Field(min_length=1)


class RepairPlan(BaseModel):
    changes: list[RepairChange] = Field(min_length=1)
    explanation: str = Field(min_length=1)


class TestComparison(BaseModel):
    iteration: int
    before: TestResult
    after: TestResult
    result: Literal["improved", "unchanged", "regressed", "passed"]


class RepairIteration(BaseModel):
    iteration: int
    changes: list[RepairChange]
    explanation: str
    comparison: TestComparison
    accepted: bool
    diff: str = ""


class GitState(BaseModel):
    initial_commit: str
    initial_status: str = ""
    initial_diff: str = ""


class RepairResponse(BaseModel):
    job_id: str
    language: Literal["python"] = "python"
    repository: str
    docker_status: Literal["success"] = "success"
    status: Literal[
        "fixed",
        "already_passing",
        "partial",
        "no_tests",
        "unsafe_change",
        "gemini_unavailable",
    ]
    iterations: int = 0
    initial_tests: TestResult
    final_tests: TestResult
    initial_analysis: list[QAAnalysisItem] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    diff: str = ""
    repairs: list[RepairIteration] = Field(default_factory=list)
    git: GitState
    message: str | None = None
    repair_error: str | None = None
