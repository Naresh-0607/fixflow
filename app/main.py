from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.analysis import router as analysis_router
from .api.repair import router as repair_router
from .core.config import PROJECT_ROOT
from .core.exceptions import FixFlowError


app = FastAPI(
    title="FixFlow",
    version="0.2.0",
    description="Python repository QA analysis and autonomous repair backend.",
)
app.include_router(analysis_router)
app.include_router(repair_router)
frontend_directory = PROJECT_ROOT / "frontend"
app.mount("/static", StaticFiles(directory=frontend_directory), name="static")


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(frontend_directory / "index.html")


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
