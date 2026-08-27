import ast
import asyncio
import hashlib
import json
import re
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..core.config import settings
from ..core.exceptions import (
    FileRepairError,
    FixFlowError,
    PipelineExecutionError,
    RepositoryFixError,
    RepositoryWorkspaceNotFound,
)
from ..models.file_workspace import FileIssue
from ..models.repositories import (
    RepositoryAnalyzeResponse,
    RepositoryCreatedResponse,
    RepositoryFixRequest,
    RepositoryFixResponse,
    RepositoryIssue,
    RepositoryProgressEvent,
    RepositoryRepairItem,
    RepositoryRepairResponse,
)
from ..models.requests import AnalyzeRequest
from ..models.responses import AnalyzeResponse, QAAnalysisItem, TestFailure
from ..services.file_workspace_service import FileAnalysisEngine, unified_diff
from ..services.repository_service import RepositoryJob, RepositoryService
from .analysis import run_analysis_job
from .files import _gemini_client, _repair_with_ai

router = APIRouter(prefix="/api/repositories", tags=["repository-workspaces"])
IGNORED_SCAN_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
VALID_FILE_ISSUE_TYPES = {
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
}


@dataclass
class ManagedRepository:
    job: RepositoryJob
    events: list[RepositoryProgressEvent] = field(default_factory=list)
    status: str = "ready"
    analysis: RepositoryAnalyzeResponse | None = None
    zip_ready: bool = False
    archive_path: Path | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    def emit(
        self,
        stage: str,
        message: str,
        progress: int,
        *,
        current: int | None = None,
        total: int | None = None,
        issue_id: str | None = None,
        file: str | None = None,
        provider: Literal["ruff", "mercury", "gemini"] | None = None,
    ) -> None:
        with self.lock:
            self.events.append(
                RepositoryProgressEvent(
                    stage=stage,
                    message=message,
                    progress=max(0, min(100, progress)),
                    current=current,
                    total=total,
                    issue_id=issue_id,
                    file=file,
                    provider=provider,
                )
            )


class ManagedRepositoryStore:
    METADATA_FILENAME = "repository-state.json"

    def __init__(self) -> None:
        self._records: dict[str, ManagedRepository] = {}
        self._lock = threading.Lock()

    def add(self, job: RepositoryJob) -> ManagedRepository:
        record = ManagedRepository(job=job)
        with self._lock:
            self._records[job.job_id] = record
        self.save(record)
        return record

    def save(self, record: ManagedRepository) -> None:
        with record.lock:
            archive_name = (
                record.archive_path.name if record.archive_path is not None else None
            )
            payload = {
                "version": 1,
                "repo_id": record.job.job_id,
                "repository": record.job.repository_name,
                "clone_url": record.job.clone_url,
                "status": (
                    "completed" if record.status == "repairing" else record.status
                ),
                "analysis": (
                    record.analysis.model_dump(mode="json")
                    if record.analysis is not None
                    else None
                ),
                "zip_ready": record.zip_ready,
                "archive_name": archive_name,
            }
        metadata = record.job.workspace / self.METADATA_FILENAME
        temporary = metadata.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
        temporary.replace(metadata)

    def _load(self, repo_id: str) -> ManagedRepository | None:
        if re.fullmatch(r"[a-f0-9]{32}", repo_id) is None:
            return None
        workspace = settings.workspace_root / repo_id
        metadata = workspace / self.METADATA_FILENAME
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            if payload.get("repo_id") != repo_id:
                return None
            current = workspace / "repo" / "current"
            original = workspace / "repo" / "original"
            if not current.is_dir() or not original.is_dir():
                return None
            job = RepositoryJob(
                job_id=repo_id,
                repository_name=str(payload["repository"]),
                workspace=workspace,
                repository_path=current,
                original_path=original,
                clone_url=str(payload["clone_url"]),
            )
            raw_analysis = payload.get("analysis")
            analysis = (
                RepositoryAnalyzeResponse.model_validate(raw_analysis)
                if isinstance(raw_analysis, dict)
                else None
            )
            archive_name = payload.get("archive_name")
            archive = (
                workspace / "artifacts" / Path(str(archive_name)).name
                if archive_name
                else None
            )
            zip_ready = bool(payload.get("zip_ready")) and bool(
                archive and archive.is_file()
            )
            return ManagedRepository(
                job=job,
                status=str(payload.get("status") or "ready"),
                analysis=analysis,
                zip_ready=zip_ready,
                archive_path=archive if zip_ready else None,
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def get(self, repo_id: str) -> ManagedRepository:
        with self._lock:
            record = self._records.get(repo_id)
        if record is None:
            record = self._load(repo_id)
            if record is not None:
                with self._lock:
                    self._records[repo_id] = record
        if record is None or not record.job.workspace.is_dir():
            raise RepositoryWorkspaceNotFound(
                "The requested repository workspace does not exist.",
                job_id=repo_id,
            )
        return record


repository_store = ManagedRepositoryStore()


def _stable_issue_id(file: str, source: str, rule: str, line: int, message: str) -> str:
    identity = f"{file}:{source}:{rule}:{line}:{message.casefold()}"
    return f"repo-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"


def _safe_repository_file(root: Path, raw_path: str) -> Path | None:
    normalized = raw_path.replace("\\", "/").removeprefix("/workspace/")
    relative = Path(normalized)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if not candidate.is_file() or candidate.is_symlink() or candidate.suffix != ".py":
        return None
    return candidate


def _symbol_line(path: Path | None, symbol: str) -> tuple[int, int]:
    if path is None:
        return 1, 1
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return 1, 1
    target = symbol.rsplit(".", 1)[-1].split("[", 1)[0]
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == target
        ):
            return node.lineno, getattr(node, "end_lineno", node.lineno)
    return 1, 1


def _qa_issue(item: QAAnalysisItem, root: Path) -> RepositoryIssue:
    path = _safe_repository_file(root, item.file)
    relative = path.relative_to(root).as_posix() if path else item.file
    line, end_line = _symbol_line(path, item.symbol)
    message = item.root_cause or item.what_failed
    severity = {"high": "high", "medium": "medium", "low": "low"}[item.confidence]
    legacy = item.model_dump(exclude={"file", "confidence"})
    return RepositoryIssue(
        issue_id=_stable_issue_id(relative, "gemini", "QA-FAILURE", line, message),
        file=relative,
        line=line,
        end_line=end_line,
        severity=severity,
        source="gemini",
        rule="QA-FAILURE",
        category="logic",
        message=message,
        fixable=path is not None,
        confidence=item.confidence,
        **legacy,
    )


def _pytest_issue(
    failure: TestFailure,
    root: Path,
    qa: QAAnalysisItem | None = None,
) -> RepositoryIssue:
    raw_file = qa.file if qa is not None and qa.file else failure.file
    path = _safe_repository_file(root, raw_file)
    if path is None:
        for match in re.finditer(
            r"(?m)^(?P<file>(?:/workspace/)?[^:\n]+\.py):(?P<line>\d+)(?::|$)",
            failure.trace,
        ):
            candidate = _safe_repository_file(root, match.group("file"))
            if candidate is not None:
                path = candidate
                break
    relative = path.relative_to(root).as_posix() if path else (raw_file or "pytest")
    if qa is not None:
        line, end_line = _symbol_line(path, qa.symbol)
        message = qa.root_cause or qa.what_failed or failure.error
        confidence = qa.confidence
        severity = {"high": "high", "medium": "medium", "low": "low"}[confidence]
        what_failed = qa.what_failed
        why = qa.why
        root_cause = qa.root_cause
        symbol = qa.symbol
        suggested_fix = qa.suggested_fix
    else:
        line = 1
        if path is not None:
            escaped_relative = re.escape(relative)
            escaped_absolute = re.escape(f"/workspace/{relative}")
            location = re.search(
                rf"(?m)^(?:{escaped_relative}|{escaped_absolute}):(\d+)(?::|$)",
                failure.trace.replace("\\", "/"),
            )
            if location:
                line = int(location.group(1))
        end_line = line
        detail = failure.error or "pytest reported a test failure"
        message = (
            detail
            if failure.test == "pytest::unparsed_failure"
            else f"{failure.test} failed: {detail}"
        )
        confidence = "high"
        severity = "high"
        what_failed = message
        why = "pytest reported a failing test or collection error."
        root_cause = failure.trace[-2_000:] or message
        symbol = failure.test.rsplit("::", 1)[-1] if "::" in failure.test else ""
        suggested_fix = "Repair the implementation causing this pytest failure, then rerun the tests."
    rule = "PYTEST-FAILURE"
    return RepositoryIssue(
        issue_id=_stable_issue_id(relative, "pytest", rule, line, message),
        file=relative,
        line=line,
        end_line=end_line,
        severity=severity,
        source="pytest",
        rule=rule,
        category="test_failure",
        message=message,
        fixable=path is not None,
        test=failure.test,
        what_failed=what_failed,
        why=why,
        root_cause=root_cause,
        symbol=symbol,
        suggested_fix=suggested_fix,
        confidence=confidence,
    )


def _local_issue(file: str, issue: FileIssue) -> RepositoryIssue:
    return RepositoryIssue(
        issue_id=_stable_issue_id(
            file,
            issue.source,
            issue.rule or "LOCAL",
            issue.line,
            issue.message,
        ),
        file=file,
        line=issue.line,
        end_line=issue.end_line,
        severity=issue.severity,
        source=issue.source,
        rule=issue.rule or "LOCAL",
        category=issue.category or issue.type,
        message=issue.message or issue.title,
        fixable=issue.fixable,
        what_failed=issue.title,
        why=issue.description,
        root_cause=issue.description,
        symbol="",
        suggested_fix=issue.suggestion,
        confidence=(
            "high" if issue.severity in {"critical", "high"} else issue.severity
        ),
    )


def _python_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if (
            path.is_symlink()
            or any(part in IGNORED_SCAN_DIRECTORIES for part in relative.parts)
            or path.stat().st_size > settings.max_file_upload_bytes
        ):
            continue
        paths.append(path)
        if len(paths) >= settings.max_tree_entries:
            break
    return sorted(paths)


async def _scan_repository(
    record: ManagedRepository,
    engine: FileAnalysisEngine,
    *,
    emit_progress: bool = True,
) -> list[RepositoryIssue]:
    paths = _python_files(record.job.repository_path)
    findings: list[RepositoryIssue] = []
    total = max(1, len(paths))
    for index, path in enumerate(paths, 1):
        source = path.read_text(encoding="utf-8", errors="replace")
        issues, _ = await engine.analyze_source_local_detailed(
            filename=path.name,
            source=source,
        )
        relative = path.relative_to(record.job.repository_path).as_posix()
        findings.extend(_local_issue(relative, issue) for issue in issues)
        if emit_progress:
            record.emit(
                "file_scanned",
                f"Scanned {index} / {len(paths)} Python files",
                96 + min(3, int(index / total * 3)),
            )
    return findings


def _severity_counts(issues: list[RepositoryIssue]) -> dict[str, int]:
    return {
        severity: sum(issue.severity == severity for issue in issues)
        for severity in ("critical", "high", "medium", "low")
    }


def _repository_response(
    record: ManagedRepository,
    base: AnalyzeResponse,
    issues: list[RepositoryIssue],
) -> RepositoryAnalyzeResponse:
    unique = {issue.issue_id: issue for issue in issues}
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}[item.severity],
            item.file,
            item.line,
        ),
    )
    status = (
        base.status
        if base.docker_status == "failed"
        or (base.status == "analysis_failed" and base.tests.exit_code in {2, 3, 4})
        else ("issues_found" if ordered else base.status)
    )
    return RepositoryAnalyzeResponse(
        repo_id=record.job.job_id,
        job_id=record.job.job_id,
        repository=base.repository,
        tests=base.tests,
        status=status,
        analysis=ordered,
        issue_count=len(ordered),
        severity_counts=_severity_counts(ordered),
        message=base.message,
        analysis_error=base.analysis_error,
    )


@router.post("", response_model=RepositoryCreatedResponse)
def create_repository(request: AnalyzeRequest) -> RepositoryCreatedResponse:
    job = RepositoryService(settings).create_managed_job(request.repository_url)
    record = repository_store.add(job)
    record.emit("repo_validated", "Repository URL validated", 5)
    return RepositoryCreatedResponse(
        repo_id=job.job_id,
        repository=job.repository_name,
    )


@router.post("/{repo_id}/analyze", response_model=RepositoryAnalyzeResponse)
def analyze_repository_workspace(repo_id: str) -> RepositoryAnalyzeResponse:
    record = repository_store.get(repo_id)
    with record.lock:
        if record.status == "running":
            raise RepositoryFixError(
                "Repository analysis is already running.", job_id=repo_id
            )
        if record.analysis is not None:
            return record.analysis
        record.status = "running"
    try:
        base = run_analysis_job(record.job, record.emit, emit_completed=False)
        record.emit("static_analysis_started", "Running local static analysis...", 96)
        engine = FileAnalysisEngine(settings, _gemini_client(fast_analysis=True))
        local = asyncio.run(_scan_repository(record, engine))
        remaining_qa = list(base.analysis)
        pytest_issues: list[RepositoryIssue] = []
        for failure in base.tests.failures:
            match = next(
                (item for item in remaining_qa if item.test == failure.test),
                None,
            )
            if match is not None:
                remaining_qa.remove(match)
            pytest_issues.append(
                _pytest_issue(failure, record.job.repository_path, match)
            )
        qa = [_qa_issue(item, record.job.repository_path) for item in remaining_qa]
        result = _repository_response(record, base, [*pytest_issues, *qa, *local])
        with record.lock:
            record.analysis = result
            record.status = "completed"
        record.emit("completed", "Analysis complete", 100)
        repository_store.save(record)
        return result
    except FixFlowError as exc:
        with record.lock:
            record.status = "failed"
            failure_progress = record.events[-1].progress if record.events else 0
        record.emit("failed", exc.message, failure_progress)
        repository_store.save(record)
        raise
    except Exception as exc:
        with record.lock:
            record.status = "failed"
            failure_progress = record.events[-1].progress if record.events else 0
        record.emit("failed", "Repository analysis failed", failure_progress)
        repository_store.save(record)
        raise PipelineExecutionError(
            "Repository analysis failed unexpectedly.",
            job_id=repo_id,
            details={
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
        ) from exc


@router.get("/{repo_id}/events")
async def repository_events(
    repo_id: str,
    request: Request,
    phase: Literal["analysis", "repair"] = "analysis",
) -> StreamingResponse:
    record = repository_store.get(repo_id)

    async def stream():
        terminal_stages = (
            {"repair_completed", "repair_failed"}
            if phase == "repair"
            else {"completed", "failed"}
        )
        with record.lock:
            if phase == "repair":
                repair_starts = [
                    index
                    for index, event in enumerate(record.events)
                    if event.stage == "repair_started"
                ]
                if repair_starts:
                    offset = repair_starts[-1]
                else:
                    analysis_terminals = [
                        index
                        for index, event in enumerate(record.events)
                        if event.stage in {"completed", "failed"}
                    ]
                    offset = analysis_terminals[-1] + 1 if analysis_terminals else 0
            else:
                offset = 0
        terminal_seen = False
        while True:
            with record.lock:
                events = record.events[offset:]
            for event in events:
                yield f"event: progress\ndata: {event.model_dump_json()}\n\n"
                offset += 1
                terminal_seen = terminal_seen or event.stage in terminal_stages
            with record.lock:
                caught_up = offset >= len(record.events)
            if terminal_seen and caught_up:
                break
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{repo_id}/file")
def repository_file(repo_id: str, path: str = Query(min_length=1)) -> dict[str, str]:
    record = repository_store.get(repo_id)
    target = _safe_repository_file(record.job.repository_path, path)
    if target is None:
        raise RepositoryFixError(
            "The requested repository file is unavailable.", job_id=repo_id
        )
    return {
        "file": target.relative_to(record.job.repository_path).as_posix(),
        "source": target.read_text(encoding="utf-8", errors="replace"),
    }


def _as_file_issue(issue: RepositoryIssue) -> FileIssue:
    issue_type = issue.category if issue.category in VALID_FILE_ISSUE_TYPES else "logic"
    return FileIssue(
        issue_id=issue.issue_id,
        id=issue.issue_id,
        source=issue.source,
        rule=issue.rule,
        category=issue.category,
        line=issue.line,
        end_line=issue.end_line,
        severity=issue.severity,
        type=issue_type,
        message=issue.message,
        fixable=issue.fixable,
        title=issue.what_failed or issue.message,
        description=issue.root_cause or issue.why or issue.message,
        suggestion=issue.suggested_fix or "Repair only the selected issue.",
    )


def _fix_failure(repo_id: str, issue_id: str, reason: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "success": False,
            "repo_id": repo_id,
            "issue_id": issue_id,
            "reason": reason,
        },
    )


@router.post("/{repo_id}/fix", response_model=RepositoryFixResponse)
def fix_repository_issue(
    repo_id: str,
    request: RepositoryFixRequest,
) -> RepositoryFixResponse | JSONResponse:
    record = repository_store.get(repo_id)
    with record.lock:
        if record.analysis is None:
            return _fix_failure(
                repo_id, request.issue_id, "Analyze the repository first.", 409
            )
        selected = next(
            (
                item
                for item in record.analysis.analysis
                if item.issue_id == request.issue_id
            ),
            None,
        )
        if selected is None:
            return _fix_failure(
                repo_id,
                request.issue_id,
                "The selected issue is not present in the latest repository analysis.",
                409,
            )
        target = _safe_repository_file(record.job.repository_path, selected.file)
        if target is None:
            return _fix_failure(
                repo_id, request.issue_id, "The issue file is unavailable.", 422
            )
        before = target.read_text(encoding="utf-8")
        file_issue = _as_file_issue(selected)
        engine = FileAnalysisEngine(settings, _gemini_client())
        try:
            if selected.source == "ruff" and selected.fixable:
                provider = "ruff"
                repaired = asyncio.run(
                    engine.apply_ruff_safe_fix(before, target.name, file_issue)
                )
            else:
                repaired, _, provider = asyncio.run(
                    _repair_with_ai(
                        engine=engine,
                        file_id=repo_id,
                        filename=selected.file,
                        source=before,
                        issue=file_issue,
                    )
                )
        except FileRepairError as exc:
            return _fix_failure(repo_id, request.issue_id, exc.message, exc.status_code)
        try:
            compile(repaired, selected.file, "exec", ast.PyCF_ONLY_AST)
        except (SyntaxError, ValueError, TypeError) as exc:
            return _fix_failure(
                repo_id,
                request.issue_id,
                f"The repaired source failed syntax validation at line {getattr(exc, 'lineno', 1) or 1}.",
                422,
            )
        diff, changed = unified_diff(selected.file, before, repaired)
        if repaired == before or not diff or changed == 0:
            return _fix_failure(
                repo_id, request.issue_id, "The repair produced no source change.", 409
            )
        after_local, _ = asyncio.run(
            engine.analyze_source_local_detailed(
                filename=target.name,
                source=repaired,
            )
        )
        if engine.issue_is_present(file_issue, after_local):
            return _fix_failure(
                repo_id,
                request.issue_id,
                "The selected issue is still present after local re-analysis.",
                422,
            )
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(repaired, encoding="utf-8", newline="")
        temporary.replace(target)
        retained = [
            item
            for item in record.analysis.analysis
            if item.issue_id != selected.issue_id
            and not (item.file == selected.file and item.source != "gemini")
        ]
        retained = [
            item.model_copy(
                update={
                    "line": relocated[0],
                    "end_line": relocated[1],
                }
            )
            if item.file == selected.file
            and item.source == "gemini"
            and (relocated := _symbol_line(target, item.symbol))
            else item
            for item in retained
        ]
        retained.extend(_local_issue(selected.file, item) for item in after_local)
        updated = record.analysis.model_copy(
            update={
                "analysis": retained,
                "issue_count": len(retained),
                "severity_counts": _severity_counts(retained),
            }
        )
        record.analysis = updated
        record.zip_ready = False
        record.archive_path = None
        repository_store.save(record)
    return RepositoryFixResponse(
        success=True,
        issue_id=request.issue_id,
        file=selected.file,
        repair_provider=provider,
        diff=diff,
        updated_analysis=updated,
        remaining_issue_count=len(updated.analysis),
    )


def _build_repository_zip(record: ManagedRepository) -> Path:
    root = record.job.repository_path
    artifacts = record.job.workspace / "artifacts"
    artifacts.mkdir(exist_ok=True)
    archive = artifacts / f"{record.job.repository_name}-fixed.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if (
                not path.is_file()
                or path.is_symlink()
                or any(part == ".git" for part in relative.parts)
            ):
                continue
            output.write(path, relative.as_posix())
    return archive


@router.post("/{repo_id}/repair", response_model=RepositoryRepairResponse)
def repair_repository(repo_id: str) -> RepositoryRepairResponse:
    record = repository_store.get(repo_id)
    with record.lock:
        if record.analysis is None:
            raise RepositoryFixError("Analyze the repository first.", job_id=repo_id)
        if record.status == "repairing":
            raise RepositoryFixError(
                "Repository repair is already running.", job_id=repo_id
            )
        initial_issues = list(record.analysis.analysis)
        record.status = "repairing"
        record.zip_ready = False
        record.archive_path = None
    repository_store.save(record)
    total = len(initial_issues)
    fixed = 0
    already_resolved = 0
    failed = 0
    results: list[RepositoryRepairItem] = []
    record.emit(
        "repair_started",
        f"Repairing {total} repository issues",
        0,
        current=0,
        total=total,
    )
    try:
        for current, initial in enumerate(initial_issues, 1):
            progress = int((current - 1) / max(1, total) * 75)
            with record.lock:
                selected = next(
                    (
                        item
                        for item in (
                            record.analysis.analysis if record.analysis else []
                        )
                        if item.issue_id == initial.issue_id
                    ),
                    None,
                )
            if selected is None:
                already_resolved += 1
                results.append(
                    RepositoryRepairItem(
                        issue_id=initial.issue_id,
                        file=initial.file,
                        status="already_resolved",
                        reason="The issue was resolved by an earlier safe repair.",
                    )
                )
                record.emit(
                    "issue_fixed",
                    f"Issue {current} was already resolved",
                    int(current / max(1, total) * 75),
                    current=current,
                    total=total,
                    issue_id=initial.issue_id,
                    file=initial.file,
                )
                continue
            record.emit(
                "issue_fix_started",
                f"Fixing issue {current} of {total}",
                progress,
                current=current,
                total=total,
                issue_id=selected.issue_id,
                file=selected.file,
            )
            try:
                outcome = fix_repository_issue(
                    repo_id,
                    RepositoryFixRequest(issue_id=selected.issue_id),
                )
                if isinstance(outcome, JSONResponse):
                    payload = json.loads(outcome.body)
                    raise FileRepairError(
                        str(payload.get("reason") or "Repair failed.")
                    )
                fixed += 1
                results.append(
                    RepositoryRepairItem(
                        issue_id=selected.issue_id,
                        file=selected.file,
                        status="fixed",
                        repair_provider=outcome.repair_provider,
                        diff=outcome.diff,
                    )
                )
                record.emit(
                    "issue_fixed",
                    f"Issue {current} fixed",
                    int(current / max(1, total) * 75),
                    current=current,
                    total=total,
                    issue_id=selected.issue_id,
                    file=selected.file,
                    provider=outcome.repair_provider,
                )
            except Exception as exc:  # noqa: BLE001 - one issue must not stop the run
                failed += 1
                reason = exc.message if isinstance(exc, FixFlowError) else str(exc)
                results.append(
                    RepositoryRepairItem(
                        issue_id=selected.issue_id,
                        file=selected.file,
                        status="failed",
                        reason=reason or "The issue could not be safely repaired.",
                    )
                )
                record.emit(
                    "issue_failed",
                    f"Issue {current} could not be safely repaired",
                    int(current / max(1, total) * 75),
                    current=current,
                    total=total,
                    issue_id=selected.issue_id,
                    file=selected.file,
                )

        record.emit("validation_started", "Running final local validation...", 82)
        engine = FileAnalysisEngine(settings, _gemini_client(fast_analysis=True))
        local = asyncio.run(_scan_repository(record, engine, emit_progress=False))
        with record.lock:
            if record.analysis is None:
                raise RepositoryFixError(
                    "Repository analysis state was lost.", job_id=repo_id
                )
            retained_ai = [
                item for item in record.analysis.analysis if item.source == "gemini"
            ]
            base = AnalyzeResponse(
                job_id=repo_id,
                repository=record.analysis.repository,
                language="python",
                docker_status="success",
                tests=record.analysis.tests,
                status=record.analysis.status,
                message=record.analysis.message,
                analysis_error=record.analysis.analysis_error,
            )
            updated = _repository_response(record, base, [*retained_ai, *local])
            record.analysis = updated
        record.emit("validation_completed", "Final local validation completed", 90)
        record.emit("zip_started", "Generating fixed repository ZIP...", 94)
        archive = _build_repository_zip(record)
        with record.lock:
            record.archive_path = archive
            record.zip_ready = True
            record.status = "repaired"
        repository_store.save(record)
        record.emit("zip_created", "Fixed repository ZIP is ready", 98)
        record.emit(
            "repair_completed",
            f"Repair complete: {fixed} fixed, {already_resolved} already resolved, {failed} failed",
            100,
            current=total,
            total=total,
        )
        return RepositoryRepairResponse(
            success=failed == 0,
            repo_id=repo_id,
            total=total,
            fixed=fixed,
            already_resolved=already_resolved,
            failed=failed,
            results=results,
            updated_analysis=updated,
            remaining_issue_count=len(updated.analysis),
            zip_ready=True,
            reason=(
                None
                if failed == 0
                else f"{failed} issue(s) could not be safely repaired."
            ),
        )
    except Exception as exc:
        with record.lock:
            record.status = "failed"
            failure_progress = record.events[-1].progress if record.events else 0
        reason = exc.message if isinstance(exc, FixFlowError) else str(exc)
        record.emit(
            "repair_failed", reason or "Repository repair failed.", failure_progress
        )
        repository_store.save(record)
        if isinstance(exc, FixFlowError):
            raise
        raise RepositoryFixError(
            "Repository repair failed unexpectedly.",
            job_id=repo_id,
            details={"exception_type": type(exc).__name__},
        ) from exc


@router.get("/{repo_id}/download")
def download_repository(repo_id: str) -> FileResponse:
    record = repository_store.get(repo_id)
    if (
        not record.zip_ready
        or record.archive_path is None
        or not record.archive_path.is_file()
    ):
        raise RepositoryFixError(
            "The fixed repository ZIP is available only after repository repair completes.",
            job_id=repo_id,
        )
    return FileResponse(
        record.archive_path,
        media_type="application/zip",
        filename=record.archive_path.name,
    )
