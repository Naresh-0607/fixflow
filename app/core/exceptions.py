from typing import Any


class FixFlowError(Exception):
    status_code = 500
    error_code = "fixflow_error"

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.job_id = job_id
        self.details = details or {}


class InvalidRepositoryUrl(FixFlowError):
    status_code = 422
    error_code = "invalid_repository_url"


class CloneError(FixFlowError):
    status_code = 502
    error_code = "clone_failed"


class UnsupportedProjectError(FixFlowError):
    status_code = 422
    error_code = "unsupported_project"


class DockerCLIUnavailableError(FixFlowError):
    status_code = 503
    error_code = "docker_cli_unavailable"


class DockerDaemonUnavailableError(FixFlowError):
    status_code = 503
    error_code = "docker_daemon_unavailable"


class DockerExecutionError(FixFlowError):
    status_code = 500
    error_code = "docker_execution_failed"


class DockerTimeoutError(FixFlowError):
    status_code = 408
    error_code = "docker_timeout"


class TestExecutionError(FixFlowError):
    status_code = 500
    error_code = "test_execution_failed"


class ExecutionTimeoutError(FixFlowError):
    status_code = 408
    error_code = "execution_timeout"


class GeminiAnalysisError(FixFlowError):
    status_code = 502
    error_code = "gemini_analysis_failed"


class MercuryRepairError(FixFlowError):
    status_code = 502
    error_code = "mercury_repair_failed"


class UnsafeModificationError(FixFlowError):
    status_code = 422
    error_code = "unsafe_modification"


class PatchApplicationError(FixFlowError):
    status_code = 422
    error_code = "patch_application_failed"


class RepositoryStateError(FixFlowError):
    status_code = 500
    error_code = "repository_state_failed"


class PipelineExecutionError(FixFlowError):
    status_code = 500
    error_code = "pipeline_execution_failed"


class InvalidFileUpload(FixFlowError):
    status_code = 422
    error_code = "invalid_file_upload"


class FileTooLargeError(FixFlowError):
    status_code = 413
    error_code = "file_too_large"


class FileWorkspaceNotFound(FixFlowError):
    status_code = 404
    error_code = "file_workspace_not_found"


class FileRepairError(FixFlowError):
    status_code = 422
    error_code = "file_repair_failed"


class RepositoryWorkspaceNotFound(FixFlowError):
    status_code = 404
    error_code = "repository_workspace_not_found"


class RepositoryFixError(FixFlowError):
    status_code = 422
    error_code = "repository_fix_failed"
