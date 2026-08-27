import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.analysis import router as analysis_router
from .api.files import router as files_router
from .api.repair import router as repair_router
from .api.repositories import router as repositories_router
from .core.config import PROJECT_ROOT, settings
from .core.exceptions import FixFlowError
from .services.file_workspace_service import FileWorkspaceStore


async def _clean_file_workspaces() -> None:
    interval = max(30, min(60, settings.file_workspace_ttl_seconds))
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(FileWorkspaceStore(settings).cleanup_expired)


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_task = asyncio.create_task(_clean_file_workspaces())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(
    title="FixFlow",
    version="0.2.0",
    description="Python repository QA analysis and autonomous repair backend.",
    lifespan=lifespan,
)
app.include_router(analysis_router)
app.include_router(repair_router)
app.include_router(files_router)
app.include_router(repositories_router)
frontend_directory = PROJECT_ROOT / "frontend"
app.mount("/static", StaticFiles(directory=frontend_directory), name="static")


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(frontend_directory / "index.html")


@app.get("/file-analyzer", include_in_schema=False)
def file_analyzer_frontend() -> FileResponse:
    return FileResponse(frontend_directory / "file-analyzer.html")


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "phase": "analysis-only"}


@app.exception_handler(FixFlowError)
async def fixflow_error_handler(
    request: Request,
    exc: FixFlowError,
) -> JSONResponse:
    content: dict[str, object] = {
        "error": exc.error_code,
        "message": exc.message,
    }
    if exc.job_id:
        content["job_id"] = exc.job_id
    if exc.details:
        content["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=content)
