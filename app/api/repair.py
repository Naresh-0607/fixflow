from fastapi import APIRouter

from ..agent.repair_agent import RepairAgent
from ..core.config import settings
from ..core.exceptions import FixFlowError, GeminiAnalysisError, PipelineExecutionError
from ..core.logging import log_stage, logger
from ..llm.gemini_client import GeminiClient
from ..models.requests import RepairRequest
from ..models.responses import GitState, RepairResponse
from ..services.analysis_service import AnalysisService
from ..services.docker_service import DockerService
from ..services.pytest_service import PytestService
from ..services.python_detector import PythonDetector
from ..services.repair_service import RepairService
from ..services.repair_test_runner import DockerTestRunner
from ..services.repository_service import RepositoryService
from ..tools.repository_tools import RepositoryTools


router = APIRouter(prefix="/api", tags=["repair"])


@router.post("/repair", response_model=RepairResponse)
def repair_repository(request: RepairRequest) -> RepairResponse:
    try:
        return _run_repair(request)
    except FixFlowError as exc:
        logger.error(
            "[FixFlow] job=%s repair failed code=%s message=%s details=%s",
            exc.job_id or "unassigned",
            exc.error_code,
            exc.message,
            exc.details,
        )
        raise
    except Exception as exc:
        logger.exception(
            "[FixFlow] job=unassigned unexpected repair pipeline failure error=%s",
            exc,
        )
        raise PipelineExecutionError(
            "FixFlow encountered an unexpected repair pipeline error.",
            details={"exception_type": type(exc).__name__},
        ) from exc


def _run_repair(request: RepairRequest) -> RepairResponse:
    repository_service = RepositoryService(settings)
    python_detector = PythonDetector()
    docker_service = DockerService(settings)
    pytest_service = PytestService()

    job = repository_service.create_job(request.repository_url)
    log_stage(job.job_id, "cloning repository")
    repository_service.clone(job)
    log_stage(job.job_id, "clone completed")

    repository_tools = RepositoryTools(job.repository_path, settings)
    git_state = GitState(
        initial_commit=repository_tools.initial_commit(),
        initial_status=repository_tools.git_status(),
        initial_diff=repository_tools.get_git_diff(),
    )
    log_stage(
        job.job_id,
        "initial Git state recorded commit=%s status_changes=%s",
        git_state.initial_commit,
        bool(git_state.initial_status),
    )

    project = python_detector.detect(job.repository_path, job_id=job.job_id)
    log_stage(
        job.job_id,
        "Python detected marker=%s strategy=%s",
        project.marker,
        project.dependency_strategy,
    )
    docker_path = docker_service.ensure_available(job_id=job.job_id)
    log_stage(job.job_id, "Docker available path=%s", docker_path)
    docker_service.pull_base_image(job_id=job.job_id)
    dockerfile = docker_service.prepare_dockerfile(
        job.workspace,
        project.dependency_strategy,
    )
    test_runner = DockerTestRunner(
        docker_service=docker_service,
        pytest_service=pytest_service,
        job_id=job.job_id,
        repository_path=job.repository_path,
        dockerfile=dockerfile,
    )

    log_stage(job.job_id, "initial pytest started")
    initial_tests = test_runner.run_tests()
    log_stage(
        job.job_id,
        "initial pytest failed=%s errors=%s passed=%s",
        initial_tests.failed,
        initial_tests.errors,
        initial_tests.passed,
    )

    gemini_client = GeminiClient(
        api_key=settings.gemini_api_key,
        primary_model=settings.gemini_model_primary,
        fallback_model=settings.gemini_model_fallback,
        timeout_seconds=settings.gemini_timeout_seconds,
        max_retries=settings.gemini_max_retries,
        retry_backoff_seconds=settings.gemini_retry_backoff_seconds,
    )
    initial_analysis = []
    if initial_tests.status not in {"passed", "no_tests"}:
        analysis_service = AnalysisService(settings, gemini_client)
        try:
            log_stage(job.job_id, "initial root-cause analysis requested")
            initial_analysis = analysis_service.analyze(
                job_id=job.job_id,
                repository_path=job.repository_path,
                test_result=initial_tests,
            )
            log_stage(job.job_id, "initial root-cause analysis completed")
        except GeminiAnalysisError as exc:
            log_stage(job.job_id, "initial root-cause analysis unavailable")
            diff_stats = repository_tools.diff_stats()
            return RepairResponse(
                job_id=job.job_id,
                repository=job.repository_name,
                status="gemini_unavailable",
                initial_tests=initial_tests,
                final_tests=initial_tests,
                modified_files=repository_tools.changed_files(),
                additions=int(diff_stats["additions"]),
                deletions=int(diff_stats["deletions"]),
                diff=repository_tools.get_git_diff(),
                git=git_state,
                message="Tests completed, but Gemini analysis was unavailable.",
                repair_error=exc.message,
            )

    service = RepairService(
        settings=settings,
        repair_agent=RepairAgent(settings, gemini_client),
        repository_tools=repository_tools,
        run_tests=test_runner.run_tests,
    )
    return service.repair(
        job_id=job.job_id,
        repository_name=job.repository_name,
        initial_tests=initial_tests,
        initial_analysis=initial_analysis,
        git_state=git_state,
    )
