from typing import Literal

from pydantic import BaseModel, Field

FileSeverity = Literal["critical", "high", "medium", "low"]
FileIssueType = Literal[
    "syntax",
    "logic",
    "runtime_risk",
    "validation",
    "security",
    "concurrency",
    "resource_leak",
    "api_usage",
    "complexity",
    "code_quality",
    "error_handling",
]


class FileIssue(BaseModel):
    issue_id: str = "pending"
    id: str = "pending"
    source: Literal[
        "syntax", "ast", "static", "ruff", "security", "complexity", "gemini", "pytest"
    ]
    rule: str | None = None
    category: str = ""
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    severity: FileSeverity
    type: FileIssueType
    message: str = ""
    fixable: bool = False
    title: str
    description: str
    suggestion: str


class CodeMapImport(BaseModel):
    module: str
    names: list[str] = Field(default_factory=list)
    line: int = Field(ge=1)


class CodeMapSymbol(BaseModel):
    name: str
    qualified_name: str
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    complexity: int | None = Field(default=None, ge=1)


class FileCodeMap(BaseModel):
    imports: list[CodeMapImport] = Field(default_factory=list)
    classes: list[CodeMapSymbol] = Field(default_factory=list)
    functions: list[CodeMapSymbol] = Field(default_factory=list)


class FileAnalyzeResponse(BaseModel):
    file_id: str
    filename: str
    language: Literal["python"] = "python"
    source: str
    issues: list[FileIssue] = Field(default_factory=list)
    issue_count: int = 0
    status: Literal["issues_found", "clean"]
    gemini_status: Literal["complete", "unavailable", "timed_out", "skipped"]
    gemini_error: str | None = None
    sha256: str
    cache_hit: bool = False
    code_map: FileCodeMap = Field(default_factory=FileCodeMap)


class FileFixAnalysis(BaseModel):
    issues: list[FileIssue] = Field(default_factory=list)
    issue_count: int = 0
    status: Literal["issues_found", "clean"]
    sha256: str
    gemini_status: Literal["complete", "unavailable", "timed_out", "skipped"]
    gemini_error: str | None = None


class FileFixResponse(BaseModel):
    success: bool = True
    issue_id: str
    repair_provider: Literal["ruff", "mercury", "gemini"]
    file_id: str
    filename: str
    status: Literal["fixed", "partially_fixed", "unchanged"]
    source: str
    fixed_code: str
    issues: list[FileIssue] = Field(default_factory=list)
    before_count: int
    after_count: int
    issues_fixed: int
    issues_remaining: int
    lines_changed: int
    diff: str
    summary: str | None = None
    gemini_status: Literal["complete", "unavailable", "timed_out", "skipped"]
    gemini_error: str | None = None
    reason: str | None = None
    old_sha256: str
    new_sha256: str
    selected_issue_resolved: bool
    analysis: FileFixAnalysis


class FileFixRequest(BaseModel):
    issue_id: str = Field(min_length=1, max_length=200)


class FileChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class FileChatResponse(BaseModel):
    file_id: str
    answer: str
    read_only: Literal[True] = True
