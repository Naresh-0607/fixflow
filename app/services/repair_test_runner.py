from pathlib import Path

from ..models.responses import TestResult
from .docker_service import DockerService
from .pytest_service import PytestService


class DockerTestRunner:
    """Rebuilds from the current workspace and runs the complete pytest suite."""

    def __init__(
        self,
        *,
        docker_service: DockerService,
        pytest_service: PytestService,
        job_id: str,
        repository_path: Path,
        dockerfile: Path,
    ) -> None:
        self.docker_service = docker_service
        self.pytest_service = pytest_service
        self.job_id = job_id
        self.repository_path = repository_path
        self.dockerfile = dockerfile
        self.run_number = 0

    def run_tests(self) -> TestResult:
        self.run_number += 1
        image_tag: str | None = None
        try:
            image_tag = self.docker_service.build_image(
                job_id=self.job_id,
                repository_path=self.repository_path,
                dockerfile=self.dockerfile,
                image_suffix=f"repair-{self.run_number}",
            )
            run = self.docker_service.run_pytest(
                job_id=self.job_id,
                image_tag=image_tag,
                artifacts_path=self.repository_path.parent / "artifacts",
            )
            return self.pytest_service.parse(run)
        finally:
            if image_tag:
                self.docker_service.remove_image(image_tag, job_id=self.job_id)
