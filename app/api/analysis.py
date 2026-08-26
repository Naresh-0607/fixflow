from fastapi import APIRouter

from ..core.config import settings
from ..core.exceptions import FixFlowError, GeminiAnalysisError, PipelineExecutionError
from ..core.logging import log_stage, logger
from ..llm.gemini_client import GeminiClient
from ..models.requests import AnalyzeRequest
from ..models.responses import AnalyzeResponse
from ..services.analysis_service import AnalysisService
from ..services.docker_service import DockerService
from ..services.pytest_service import PytestService
from ..services.python_detector import PythonDetector
from ..services.repository_service import RepositoryService


router = APIRouter(prefix="/api", tags=["analysis"])


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze_repository(request: AnalyzeRequest) -> AnalyzeResponse:
    try:
        return _run_analysis(request)
    except FixFlowError as exc:
        logger.error(
            "[FixFlow] job=%s analysis failed code=%s message=%s details=%s",
            exc.job_id or "unassigned",
            exc.error_code,
            exc.message,
            exc.details,
        )
        raise
    except Exception as exc:
        logger.exception(
            "[FixFlow] job=unassigned unexpected pipeline failure error=%s",
            exc,
        )
        raise PipelineExecutionError(
            "FixFlow encountered an unexpected pipeline error.",
            details={"exception_type": type(exc).__name__},
        ) from exc


def _run_analysis(request: AnalyzeRequest) -> AnalyzeResponse:
    repository_service = RepositoryService(settings)
    python_detector = PythonDetector()
    docker_service = DockerService(settings)
    pytest_service = PytestService()

    job = repository_service.create_job(request.repository_url)
    log_stage(job.job_id, "cloning repository")
    repository_service.clone(job)
    log_stage(job.job_id, "clone completed")
    log_stage(job.job_id, "detecting project")
    project = python_detector.detect(job.repository_path, job_id=job.job_id)
    log_stage(
        job.job_id,
        "Python detected marker=%s strategy=%s",
        project.marker,
        project.dependency_strategy,
    )
    log_stage(job.job_id, "checking Docker")
    docker_path = docker_service.ensure_available(job_id=job.job_id)
    log_stage(job.job_id, "Docker available path=%s", docker_path)
    log_stage(job.job_id, "pulling Python base image")
    docker_service.pull_base_image(job_id=job.job_id)
    log_stage(job.job_id, "Python base image ready")
    log_stage(job.job_id, "preparing Python environment")
    dockerfile = docker_service.prepare_dockerfile(
        job.workspace,
        project.dependency_strategy,
    )

    image_tag: str | None = None
    try:
        log_stage(job.job_id, "installing dependencies")
        image_tag = docker_service.build_image(
            job_id=job.job_id,
            repository_path=job.repository_path,
            dockerfile=dockerfile,
        )
        log_stage(job.job_id, "dependencies installed image=%s", image_tag)
        log_stage(job.job_id, "running pytest")
        run = docker_service.run_pytest(
            job_id=job.job_id,
            image_tag=image_tag,
            artifacts_path=job.workspace / "artifacts",
        )
        log_stage(job.job_id, "pytest completed exit_code=%s", run.exit_code)
    finally:
        if image_tag:
            log_stage(job.job_id, "cleaning temporary Docker image")
            docker_service.remove_image(image_tag, job_id=job.job_id)
            log_stage(job.job_id, "Docker image cleanup completed")

    log_stage(job.job_id, "parsing failures")
    test_result = pytest_service.parse(run)

    if test_result.status == "no_tests":
        log_stage(job.job_id, "analysis completed status=no_tests")
        return AnalyzeResponse(
            job_id=job.job_id,
            language="python",
            repository=job.repository_name,
            docker_status="success",
            tests=test_result,
            status="no_tests",
            message="pytest completed, but no tests were collected.",
        )

    if test_result.status == "passed":
        log_stage(job.job_id, "analysis completed status=tests_passed")
        return AnalyzeResponse(
            job_id=job.job_id,
            language="python",
            repository=job.repository_name,
            docker_status="success",
            tests=test_result,
            status="tests_passed",
            message=(
                "All tests passed. Repository is ready for the refactoring "
                "analysis phase."
            ),
        )

    gemini_client = GeminiClient(
        api_key=settings.gemini_api_key,
        primary_model=settings.gemini_model_primary,
        fallback_model=settings.gemini_model_fallback,
        timeout_seconds=settings.gemini_timeout_seconds,
        max_retries=settings.gemini_max_retries,
        retry_backoff_seconds=settings.gemini_retry_backoff_seconds,
    )
    analysis_service = AnalysisService(settings, gemini_client)
    try:
        log_stage(job.job_id, "sending analysis to Gemini")
        analysis = analysis_service.analyze(
            job_id=job.job_id,
            repository_path=job.repository_path,
            test_result=test_result,
        )
    except GeminiAnalysisError as exc:
        logger.error(
            "[FixFlow] job=%s Gemini analysis failed message=%s details=%s",
            job.job_id,
            exc.message,
            exc.details,
        )
        log_stage(job.job_id, "analysis completed status=analysis_failed")
        return AnalyzeResponse(
            job_id=job.job_id,
            language="python",
            repository=job.repository_name,
            docker_status="success",
            tests=test_result,
            status="analysis_failed",
            message="Tests completed, but Gemini analysis was unavailable.",
            analysis_error=exc.message,
        )

    log_stage(job.job_id, "Gemini response received")
    log_stage(job.job_id, "analysis completed status=issues_found")
    return AnalyzeResponse(
        job_id=job.job_id,
        language="python",
        repository=job.repository_name,
        docker_status="success",
        tests=test_result,
        status="issues_found",
        analysis=analysis,
        message=f"Gemini analyzed {len(test_result.failures)} pytest failures.",
    )
