import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..core.config import Settings
from ..core.exceptions import (
    DockerCLIUnavailableError,
    DockerDaemonUnavailableError,
    DockerExecutionError,
    DockerTimeoutError,
    TestExecutionError,
)
from ..core.logging import logger
from .python_detector import DependencyStrategy


@dataclass(frozen=True)
class DockerRunResult:
    stdout: str
    stderr: str
    exit_code: int
    junit_xml_path: Path
    job_id: str | None = None


class DockerService:
    BASE_IMAGE = "python:3.12-slim"

    def __init__(self, settings: Settings, *, docker_path: str | None = None) -> None:
        self.settings = settings
        self._docker_path = docker_path

    def ensure_available(self, *, job_id: str) -> str:
        docker_path = shutil.which("docker")
        if docker_path is None:
            logger.error("[FixFlow] job=%s Docker CLI was not found on PATH", job_id)
            raise DockerCLIUnavailableError(
                "Docker CLI is not installed or is not available on PATH.",
                job_id=job_id,
            )

        self._docker_path = docker_path
        try:
            result = self._run_command(
                [docker_path, "info", "--format", "{{.ServerVersion}}"],
                timeout=self.settings.docker_info_timeout_seconds,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            logger.error(
                "[FixFlow] job=%s Docker CLI could not execute path=%s error=%s",
                job_id,
                docker_path,
                exc,
            )
            raise DockerCLIUnavailableError(
                f"Docker CLI was found at {docker_path}, but it could not be executed.",
                job_id=job_id,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "[FixFlow] job=%s Docker info timed out after %ss",
                job_id,
                self.settings.docker_info_timeout_seconds,
            )
            raise DockerTimeoutError(
                "Docker daemon check exceeded the configured timeout.",
                job_id=job_id,
                details={"operation": "docker info"},
            ) from exc

        if result.returncode != 0:
            error = self._combined_output(result).strip()
            logger.error(
                "[FixFlow] job=%s Docker daemon unavailable exit_code=%s stderr=%s",
                job_id,
                result.returncode,
                self._one_line(error),
            )
            raise DockerDaemonUnavailableError(
                "Docker CLI is installed, but the Docker daemon cannot be contacted.",
                job_id=job_id,
                details={
                    "docker_path": docker_path,
                    "exit_code": result.returncode,
                    "docker_error": error[-3000:],
                },
            )
        return docker_path

    def pull_base_image(self, *, job_id: str) -> None:
        docker_path = self._docker_executable(job_id)
        try:
            result = self._run_command(
                [docker_path, "pull", self.BASE_IMAGE],
                timeout=self.settings.docker_pull_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "[FixFlow] job=%s Docker pull timed out image=%s timeout=%ss",
                job_id,
                self.BASE_IMAGE,
                self.settings.docker_pull_timeout_seconds,
            )
            raise DockerTimeoutError(
                "Docker base-image pull exceeded the configured timeout.",
                job_id=job_id,
                details={"operation": "docker pull", "image": self.BASE_IMAGE},
            ) from exc

        if result.returncode != 0:
            output = self._combined_output(result)
            logger.error(
                "[FixFlow] job=%s Docker pull failed exit_code=%s stderr=%s",
                job_id,
                result.returncode,
                self._one_line(output),
            )
            raise DockerExecutionError(
                "Docker could not pull the Python base image.",
                job_id=job_id,
                details={
                    "operation": "docker pull",
                    "exit_code": result.returncode,
                    "docker_output": output[-12_000:],
                },
            )

    def prepare_dockerfile(
        self,
        workspace: Path,
        strategy: DependencyStrategy,
    ) -> Path:
        install_command = self._dependency_install_command(strategy)
        dockerfile = workspace / "FixFlow.Dockerfile"
        dockerignore = workspace / "FixFlow.Dockerfile.dockerignore"
        content = "\n".join(
            [
                f"FROM {self.BASE_IMAGE}",
                "ENV PYTHONDONTWRITEBYTECODE=1 \\",
                "    PYTHONUNBUFFERED=1 \\",
                "    PIP_DISABLE_PIP_VERSION_CHECK=1 \\",
                "    PIP_NO_CACHE_DIR=1",
                "RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \\",
                "    && rm -rf /var/lib/apt/lists/*",
                "WORKDIR /workspace",
                "COPY . /workspace",
                "RUN python -m pip install --upgrade pip setuptools wheel",
                f"RUN {install_command}",
                "RUN python -m pip install pytest",
                'CMD ["python", "-m", "pytest", "-v", "--tb=long", "-ra"]',
                "",
            ]
        )
        dockerfile.write_text(content, encoding="utf-8")
        dockerignore.write_text(
            "\n".join(
                [
                    ".git",
                    ".venv",
                    "venv",
                    "__pycache__",
                    ".pytest_cache",
                    ".mypy_cache",
                    ".ruff_cache",
                    "*.pyc",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return dockerfile

    def build_image(
        self,
        *,
        job_id: str,
        repository_path: Path,
        dockerfile: Path,
        image_suffix: str = "phase1",
    ) -> str:
        docker_path = self._docker_executable(job_id)
        safe_suffix = "".join(
            character if character.isalnum() or character in "_.-" else "-"
            for character in image_suffix
        )
        image_tag = f"fixflow-{job_id}:{safe_suffix}"
        command = [
            docker_path,
            "build",
            "--file",
            str(dockerfile),
            "--tag",
            image_tag,
            str(repository_path),
        ]
        try:
            result = self._run_command(
                command,
                timeout=self.settings.docker_build_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "[FixFlow] job=%s Docker build timed out after %ss",
                job_id,
                self.settings.docker_build_timeout_seconds,
            )
            raise DockerTimeoutError(
                "Docker dependency installation and image build exceeded the configured timeout.",
                job_id=job_id,
                details={"operation": "docker build"},
            ) from exc

        if result.returncode != 0:
            output = self._combined_output(result)
            logger.error(
                "[FixFlow] job=%s Docker build failed exit_code=%s stderr=%s",
                job_id,
                result.returncode,
                self._one_line(output),
            )
            raise DockerExecutionError(
                "Docker could not install dependencies or build the test image.",
                job_id=job_id,
                details={
                    "operation": "docker build",
                    "exit_code": result.returncode,
                    "build_output": output[-12_000:],
                },
            )
        return image_tag

    def run_pytest(
        self,
        *,
        job_id: str,
        image_tag: str,
        artifacts_path: Path,
    ) -> DockerRunResult:
        docker_path = self._docker_executable(job_id)
        container_name = f"fixflow-test-{job_id}"
        if artifacts_path.is_symlink():
            raise DockerExecutionError(
                "The pytest artifact path cannot be a symbolic link.",
                job_id=job_id,
            )
        artifacts_path.mkdir(parents=True, exist_ok=True)
        resolved_artifacts = artifacts_path.resolve()
        if not resolved_artifacts.is_dir():
            raise DockerExecutionError(
                "The pytest artifact path is not a regular directory.",
                job_id=job_id,
            )
        junit_xml_path = resolved_artifacts / "junit.xml"
        junit_xml_path.unlink(missing_ok=True)
        command = [
            docker_path,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--memory",
            self.settings.docker_memory,
            "--cpus",
            self.settings.docker_cpus,
            "--pids-limit",
            str(self.settings.docker_pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--mount",
            f"type=bind,source={resolved_artifacts},target=/fixflow-results",
            image_tag,
            "python",
            "-m",
            "pytest",
            "-v",
            "--tb=long",
            "-ra",
            "--junitxml=/fixflow-results/junit.xml",
        ]
        try:
            result = self._run_command(
                command,
                timeout=self.settings.docker_run_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self._remove_container(container_name, job_id=job_id)
            logger.error(
                "[FixFlow] job=%s Docker pytest run timed out after %ss",
                job_id,
                self.settings.docker_run_timeout_seconds,
            )
            raise DockerTimeoutError(
                "pytest execution inside Docker exceeded the configured timeout.",
                job_id=job_id,
                details={"operation": "docker run"},
            ) from exc

        output = self._combined_output(result)
        if result.returncode == 125:
            logger.error(
                "[FixFlow] job=%s Docker run failed exit_code=125 stderr=%s",
                job_id,
                self._one_line(output),
            )
            raise DockerExecutionError(
                "Docker could not start the pytest container.",
                job_id=job_id,
                details={
                    "operation": "docker run",
                    "exit_code": result.returncode,
                    "docker_output": output[-12_000:],
                },
            )

        # pytest exit codes 1-5 are completed pytest runs, not malformed API
        # requests. Preserve their output so the analysis layer can report a
        # failed/no-tests/setup-error state instead of converting them to 4xx.
        if result.returncode not in {0, 1, 2, 3, 4, 5}:
            if result.returncode == 127 or "No module named pytest" in output:
                message = "pytest was not found inside the container."
            elif result.returncode == 3:
                message = "pytest encountered an internal error inside the container."
            elif result.returncode == 4:
                message = (
                    "pytest was invoked with invalid arguments inside the container."
                )
            else:
                message = "pytest could not complete inside the container."
            logger.error(
                "[FixFlow] job=%s pytest execution failed exit_code=%s stderr=%s",
                job_id,
                result.returncode,
                self._one_line(output),
            )
            raise TestExecutionError(
                message,
                job_id=job_id,
                details={
                    "exit_code": result.returncode,
                    "pytest_output": output[-12_000:],
                },
            )

        return DockerRunResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            junit_xml_path=junit_xml_path,
            job_id=job_id,
        )

    def remove_image(self, image_tag: str, *, job_id: str) -> None:
        try:
            docker_path = self._docker_executable(job_id)
            result = self._run_command(
                [docker_path, "image", "rm", "--force", image_tag],
                timeout=20,
            )
            if result.returncode != 0:
                logger.warning(
                    "[FixFlow] job=%s Docker image cleanup failed exit_code=%s stderr=%s",
                    job_id,
                    result.returncode,
                    self._one_line(self._combined_output(result)),
                )
        except (
            DockerCLIUnavailableError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ) as exc:
            logger.warning(
                "[FixFlow] job=%s Docker image cleanup did not complete error=%s",
                job_id,
                exc,
            )

    def _remove_container(self, container_name: str, *, job_id: str) -> None:
        try:
            docker_path = self._docker_executable(job_id)
            self._run_command(
                [docker_path, "container", "rm", "--force", container_name],
                timeout=20,
            )
        except (
            DockerCLIUnavailableError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ) as exc:
            logger.warning(
                "[FixFlow] job=%s timed-out container cleanup failed name=%s error=%s",
                job_id,
                container_name,
                exc,
            )

    def _docker_executable(self, job_id: str) -> str:
        if self._docker_path:
            return self._docker_path
        docker_path = shutil.which("docker")
        if docker_path is None:
            raise DockerCLIUnavailableError(
                "Docker CLI is not installed or is not available on PATH.",
                job_id=job_id,
            )
        self._docker_path = docker_path
        return docker_path

    @staticmethod
    def _dependency_install_command(strategy: DependencyStrategy) -> str:
        commands = {
            "requirements": "python -m pip install -r requirements.txt",
            "pyproject": "python -m pip install .",
            "setup": "python -m pip install .",
            "setup_cfg": "python -c \"print('setup.cfg detected; no build script found')\"",
            "pipenv": "python -m pip install pipenv && pipenv install --system --dev",
            "none": "python -c \"print('No dependency manifest found')\"",
        }
        return commands[strategy]

    @staticmethod
    def _run_command(
        command: list[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
        return "\n".join(part for part in (result.stdout, result.stderr) if part)

    @staticmethod
    def _one_line(output: str, limit: int = 1000) -> str:
        return " ".join(output.split())[-limit:]
