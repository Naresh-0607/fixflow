import ast
import asyncio
import hashlib
import time

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..core.config import settings
from ..core.exceptions import (
    FileRepairError,
    GeminiAnalysisError,
    InvalidFileUpload,
    MercuryRepairError,
)
from ..core.logging import logger
from ..llm.gemini_client import GeminiClient
from ..llm.inception_client import InceptionClient
from ..models.file_workspace import (
    FileAnalyzeResponse,
    FileChatRequest,
    FileChatResponse,
    FileCodeMap,
    FileFixAnalysis,
    FileFixRequest,
    FileFixResponse,
    FileIssue,
)
from ..services.file_workspace_service import (
    FileAnalysisCache,
    FileAnalysisEngine,
    FileWorkspaceStore,
    unified_diff,
)

router = APIRouter(prefix="/api/files", tags=["file-analyzer"])
_analysis_locks: dict[str, asyncio.Lock] = {}


def _gemini_client(*, fast_analysis: bool = False) -> GeminiClient:
    return GeminiClient(
        api_key=settings.gemini_api_key,
        primary_model=settings.gemini_model_primary,
        fallback_model=None if fast_analysis else settings.gemini_model_fallback,
        timeout_seconds=(
            min(10, settings.gemini_timeout_seconds)
            if fast_analysis
            else settings.gemini_timeout_seconds
        ),
        max_retries=0 if fast_analysis else settings.gemini_max_retries,
        retry_backoff_seconds=settings.gemini_retry_backoff_seconds,
    )


def _mercury_client() -> InceptionClient:
    return InceptionClient(
        api_key=settings.inception_api_key,
        model=settings.fixflow_fix_model,
        timeout_seconds=settings.fixflow_fix_timeout_seconds,
    )


def _fix_log(file_id: str, message: str, *args: object) -> None:
    logger.info(f"[FixFlow Fix] file={file_id} {message}", *args)


async def _repair_with_ai(
    *,
    engine: FileAnalysisEngine,
    file_id: str,
    filename: str,
    source: str,
    issue: FileIssue,
) -> tuple[str, str | None, str]:
    context_started = time.perf_counter()
    prompt = engine.build_repair_prompt(
        filename=filename,
        source=source,
        issue=issue,
    )
    _fix_log(
        file_id,
        "context extraction: %.3fs",
        time.perf_counter() - context_started,
    )
    providers: list[tuple[str, object]]
    if settings.fixflow_fix_provider == "gemini":
        providers = [("gemini", _gemini_client())]
    else:
        providers = [
            ("mercury", _mercury_client()),
            ("gemini", _gemini_client()),
        ]
    failures: list[str] = []
    for provider_name, client in providers:
        request_started = time.perf_counter()
        _fix_log(file_id, "provider: %s", provider_name)
        try:
            raw = await asyncio.to_thread(
                client.analyze,  # type: ignore[attr-defined]
                prompt,
                job_id=file_id,
            )
            _fix_log(
                file_id,
                "AI request: %.3fs provider=%s",
                time.perf_counter() - request_started,
                provider_name,
            )
            apply_started = time.perf_counter()
            repaired, summary = engine.apply_repair_response(
                raw=raw,
                file_id=file_id,
                filename=filename,
                source=source,
                issue=issue,
            )
            _fix_log(
                file_id,
                "patch apply: %.3fs provider=%s",
                time.perf_counter() - apply_started,
                provider_name,
            )
            return repaired, summary, provider_name
        except (MercuryRepairError, GeminiAnalysisError, FileRepairError) as exc:
            failures.append(f"{provider_name}: {exc.message}")
            _fix_log(
                file_id,
                "provider failed: %s elapsed=%.3fs",
                provider_name,
                time.perf_counter() - request_started,
            )
        except Exception:  # noqa: BLE001 - provider failures must trigger fallback
            failures.append(f"{provider_name}: unexpected provider failure")
            _fix_log(
                file_id,
                "provider failed: %s elapsed=%.3fs",
                provider_name,
                time.perf_counter() - request_started,
            )
    raise FileRepairError("; ".join(failures) or "No AI repair provider is available.")


@router.post("/analyze", response_model=FileAnalyzeResponse)
async def analyze_file(file: UploadFile = File(...)) -> FileAnalyzeResponse:  # noqa: B008
    if not file.filename:
        raise InvalidFileUpload("An uploaded filename is required.")
    content = await file.read(settings.max_file_upload_bytes + 1)
    await file.close()
    store = FileWorkspaceStore(settings)
    workspace = store.create(file.filename, content)
    source = workspace.current_path.read_text(encoding="utf-8")
    source_hash = hashlib.sha256(content).hexdigest()
    cache = FileAnalysisCache(settings)
    cache_hit = False
    lock = _analysis_locks.setdefault(source_hash, asyncio.Lock())
    async with lock:
        cached = await asyncio.to_thread(cache.read, source_hash)
        if cached is not None:
            try:
                issues = [FileIssue.model_validate(item) for item in cached["issues"]]
                code_map = FileCodeMap.model_validate(cached.get("code_map", {}))
                gemini_status = str(cached["gemini_status"])
                gemini_error = cached.get("gemini_error")
                cache_hit = True
            except (KeyError, TypeError, ValueError):
                cached = None
        if cached is None:
            engine = FileAnalysisEngine(settings, _gemini_client(fast_analysis=True))
            issues, gemini_status, gemini_error, code_map = (
                await engine.analyze_source_detailed(
                    file_id=workspace.file_id,
                    filename=workspace.filename,
                    source=source,
                )
            )
            await asyncio.to_thread(
                cache.write,
                source_hash,
                {
                    "issues": [issue.model_dump() for issue in issues],
                    "code_map": code_map.model_dump(),
                    "gemini_status": gemini_status,
                    "gemini_error": gemini_error,
                },
            )
    metadata = store.read_metadata(workspace)
    metadata.update(
        {
            "analyzed": True,
            "issues": [issue.model_dump() for issue in issues],
            "gemini_status": gemini_status,
            "gemini_error": gemini_error,
            "sha256": source_hash,
            "cache_hit": cache_hit,
            "code_map": code_map.model_dump(),
        }
    )
    store.write_metadata(workspace, metadata)
    return FileAnalyzeResponse(
        file_id=workspace.file_id,
        filename=workspace.filename,
        source=source,
        issues=issues,
        issue_count=len(issues),
        status="issues_found" if issues else "clean",
        gemini_status=gemini_status,
        gemini_error=gemini_error,
        sha256=source_hash,
        cache_hit=cache_hit,
        code_map=code_map,
    )


@router.post("/{file_id}/fix", response_model=FileFixResponse)
async def fix_file(
    file_id: str, request: FileFixRequest | None = None
) -> FileFixResponse | JSONResponse:
    total_started = time.perf_counter()
    resolution_started = total_started
    store = FileWorkspaceStore(settings)
    workspace = store.get(file_id)
    metadata = store.read_metadata(workspace)
    if not metadata.get("analyzed"):
        raise FileRepairError("Analyze the file before requesting a repair.", job_id=file_id)
    try:
        issues = [FileIssue.model_validate(item) for item in metadata.get("issues", [])]
    except Exception as exc:
        raise FileRepairError("The saved analysis is invalid.", job_id=file_id) from exc
    if not issues:
        raise FileRepairError(
            "No confirmed issues are available to repair.", job_id=file_id
        )
    selected_id = request.issue_id if request else issues[0].issue_id
    selected = next((issue for issue in issues if issue.issue_id == selected_id), None)
    if selected is None:
        return _fix_failure(
            file_id,
            selected_id,
            "The selected issue is not present in the current workspace analysis.",
            status_code=409,
        )
    _fix_log(
        file_id,
        "issue resolution: %.3fs issue=%s",
        time.perf_counter() - resolution_started,
        selected_id,
    )
    before = workspace.current_path.read_text(encoding="utf-8")
    old_hash = str(metadata.get("sha256") or hashlib.sha256(before.encode()).hexdigest())
    repair_engine = FileAnalysisEngine(settings, _gemini_client())
    try:
        if selected.source == "ruff" and selected.fixable:
            provider_name = "ruff"
            ruff_started = time.perf_counter()
            repaired = await repair_engine.apply_ruff_safe_fix(
                before, workspace.filename, selected
            )
            summary = f"Applied Ruff's safe {selected.rule} fix."
            _fix_log(
                file_id,
                "provider: ruff deterministic fix: %.3fs",
                time.perf_counter() - ruff_started,
            )
        else:
            repaired, summary, provider_name = await _repair_with_ai(
                engine=repair_engine,
                file_id=file_id,
                filename=workspace.filename,
                source=before,
                issue=selected,
            )
    except FileRepairError as exc:
        return _fix_failure(
            file_id,
            selected_id,
            exc.message,
            status_code=exc.status_code,
        )
    if repaired == before:
        return _fix_failure(
            file_id,
            selected_id,
            "The repair produced no source change.",
            status_code=409,
        )
    validation_started = time.perf_counter()
    try:
        compile(repaired, workspace.filename, "exec", ast.PyCF_ONLY_AST)
    except (SyntaxError, ValueError, TypeError) as exc:
        return _fix_failure(
            file_id,
            selected_id,
            f"The repaired source failed syntax validation at line {getattr(exc, 'lineno', 1) or 1}.",
            status_code=422,
        )
    _fix_log(
        file_id,
        "validation: %.3fs",
        time.perf_counter() - validation_started,
    )
    diff, lines_changed = unified_diff(workspace.filename, before, repaired)
    if not diff or lines_changed == 0:
        return _fix_failure(
            file_id,
            selected_id,
            "The repair produced no source change.",
            status_code=409,
        )
    temporary = workspace.current_path.with_suffix(".tmp")
    temporary.write_text(repaired, encoding="utf-8", newline="")
    temporary.replace(workspace.current_path)

    cache = FileAnalysisCache(settings)
    current_old_hash = hashlib.sha256(before.encode()).hexdigest()
    await asyncio.to_thread(cache.invalidate, old_hash)
    if current_old_hash != old_hash:
        await asyncio.to_thread(cache.invalidate, current_old_hash)
    new_hash = hashlib.sha256(repaired.encode()).hexdigest()
    local_analysis_started = time.perf_counter()
    analysis_engine = FileAnalysisEngine(settings, _gemini_client(fast_analysis=True))
    after_issues, code_map = await analysis_engine.analyze_source_local_detailed(
        filename=workspace.filename,
        source=repaired,
    )
    gemini_status = "skipped"
    gemini_error = None
    _fix_log(
        file_id,
        "local re-analysis: %.3fs",
        time.perf_counter() - local_analysis_started,
    )
    await asyncio.to_thread(
        cache.write,
        new_hash,
        {
            "issues": [issue.model_dump() for issue in after_issues],
            "code_map": code_map.model_dump(),
            "gemini_status": gemini_status,
            "gemini_error": gemini_error,
        },
    )
    selected_resolved = not analysis_engine.issue_is_present(selected, after_issues)
    before_count = len(issues)
    after_count = len(after_issues)
    fixed_count = max(0, before_count - after_count)
    status = "fixed" if selected_resolved and after_count == 0 else "partially_fixed"
    reason = None if selected_resolved else "The selected issue is still present after re-analysis."
    metadata.update(
        {
            "repaired": repaired != before,
            "issues": [issue.model_dump() for issue in after_issues],
            "before_count": before_count,
            "after_count": after_count,
            "diff": diff,
            "summary": summary,
            "gemini_status": gemini_status,
            "gemini_error": gemini_error,
            "sha256": new_hash,
            "cache_hit": False,
            "code_map": code_map.model_dump(),
            "last_fixed_issue_id": selected_id,
            "last_fix_provider": provider_name,
        }
    )
    store.write_metadata(workspace, metadata)
    _fix_log(file_id, "total: %.3fs", time.perf_counter() - total_started)
    return FileFixResponse(
        success=selected_resolved,
        issue_id=selected_id,
        repair_provider=provider_name,
        file_id=file_id,
        filename=workspace.filename,
        status=status,
        source=repaired,
        fixed_code=repaired,
        issues=after_issues,
        before_count=before_count,
        after_count=after_count,
        issues_fixed=fixed_count,
        issues_remaining=after_count,
        lines_changed=lines_changed,
        diff=diff,
        summary=summary,
        gemini_status=gemini_status,
        gemini_error=gemini_error,
        reason=reason,
        old_sha256=old_hash,
        new_sha256=new_hash,
        selected_issue_resolved=selected_resolved,
        analysis=FileFixAnalysis(
            issues=after_issues,
            issue_count=after_count,
            status="issues_found" if after_issues else "clean",
            sha256=new_hash,
            gemini_status=gemini_status,
            gemini_error=gemini_error,
        ),
    )


def _fix_failure(
    file_id: str, issue_id: str, reason: str, *, status_code: int
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "file_id": file_id,
            "issue_id": issue_id,
            "reason": reason,
            "error": "file_repair_failed",
        },
    )


@router.get("/{file_id}/download")
def download_file(file_id: str) -> FileResponse:
    workspace = FileWorkspaceStore(settings).get(file_id)
    return FileResponse(
        path=workspace.current_path,
        media_type="text/x-python; charset=utf-8",
        filename=workspace.filename,
    )


@router.post("/{file_id}/chat", response_model=FileChatResponse)
def chat_about_file(file_id: str, request: FileChatRequest) -> FileChatResponse:
    store = FileWorkspaceStore(settings)
    workspace = store.get(file_id)
    metadata = store.read_metadata(workspace)
    issues = [FileIssue.model_validate(item) for item in metadata.get("issues", [])]
    original = workspace.original_path.read_bytes().decode("utf-8-sig")
    current = workspace.current_path.read_text(encoding="utf-8")
    answer = FileAnalysisEngine(settings, _gemini_client()).answer_chat(
        file_id=file_id,
        filename=workspace.filename,
        message=request.message,
        original=original,
        current=current,
        issues=issues,
        diff=str(metadata.get("diff", "")),
    )
    return FileChatResponse(file_id=file_id, answer=answer)
