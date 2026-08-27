from typing import Literal

from pydantic import BaseModel, Field

from .responses import TestResult


class RepositoryIssue(BaseModel):
    issue_id: str
    file: str
    line: int = Field(default=1, ge=1)
    end_line: int = Field(default=1, ge=1)
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    source: str = "gemini"
    rule: str = "QA-FAILURE"
    category: str = "logic"
    message: str
    fixable: bool = True
    test: str = ""
    what_failed: str = ""
    why: str = ""
    root_cause: str = ""
    symbol: str = ""
    suggested_fix: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"


class RepositoryCreatedResponse(BaseModel):
    repo_id: str
    repository: str
    status: Literal["ready"] = "ready"


class RepositoryAnalyzeResponse(BaseModel):
    repo_id: str
    job_id: str
    language: Literal["python"] = "python"
    repository: str
    docker_status: Literal["success", "failed"] = "success"
    tests: TestResult
    status: Literal[
        "issues_found",
        "tests_passed",
        "analysis_failed",
        "no_tests",
    ]
    analysis: list[RepositoryIssue] = Field(default_factory=list)
    issue_count: int = 0
    severity_counts: dict[str, int] = Field(default_factory=dict)
    message: str | None = None
    analysis_error: str | None = None


class RepositoryFixRequest(BaseModel):
    issue_id: str = Field(min_length=1)


class RepositoryFixResponse(BaseModel):
    success: bool
    issue_id: str
    file: str
    repair_provider: Literal["ruff", "mercury", "gemini"]
    diff: str
    updated_analysis: RepositoryAnalyzeResponse
    remaining_issue_count: int
    reason: str | None = None


class RepositoryRepairItem(BaseModel):
    issue_id: str
    file: str
    status: Literal["fixed", "already_resolved", "failed"]
    repair_provider: Literal["ruff", "mercury", "gemini"] | None = None
    reason: str | None = None
    diff: str = ""


class RepositoryRepairResponse(BaseModel):
    success: bool
    repo_id: str
    total: int
    fixed: int
    already_resolved: int
    failed: int
    results: list[RepositoryRepairItem] = Field(default_factory=list)
    updated_analysis: RepositoryAnalyzeResponse
    remaining_issue_count: int
    zip_ready: bool
    reason: str | None = None


class RepositoryProgressEvent(BaseModel):
    stage: str
    message: str
    progress: int = Field(ge=0, le=100)
    current: int | None = None
    total: int | None = None
    issue_id: str | None = None
    file: str | None = None
    provider: Literal["ruff", "mercury", "gemini"] | None = None
