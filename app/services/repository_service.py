import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..core.config import Settings
from ..core.exceptions import CloneError, ExecutionTimeoutError, InvalidRepositoryUrl
from ..core.logging import logger

GITHUB_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class RepositoryJob:
    job_id: str
    repository_name: str
    workspace: Path
    repository_path: Path
    clone_url: str
    original_path: Path | None = None


class RepositoryService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate_public_github_url(self, repository_url: str) -> tuple[str, str]:
        parsed = urlparse(repository_url.strip())
        try:
            has_port = parsed.port is not None
        except ValueError as exc:
            raise InvalidRepositoryUrl("The GitHub URL contains an invalid port.") from exc
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "github.com"
            or parsed.username
            or parsed.password
            or has_port
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidRepositoryUrl(
                "Only public HTTPS GitHub repository URLs are supported."
            )

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2:
            raise InvalidRepositoryUrl(
                "Repository URL must have the form https://github.com/owner/repository.git."
            )

        owner, repository = parts
        repository = repository.removesuffix(".git")
        if not owner or not repository or not all(
            GITHUB_PART_PATTERN.fullmatch(part) for part in (owner, repository)
        ):
            raise InvalidRepositoryUrl("The GitHub owner or repository name is invalid.")

        clone_url = f"https://github.com/{owner}/{repository}.git"
        return clone_url, repository

    def create_job(self, repository_url: str) -> RepositoryJob:
        clone_url, repository_name = self.validate_public_github_url(repository_url)
        job_id = uuid.uuid4().hex
        self.settings.workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = self.settings.workspace_root / job_id
        repository_path = workspace / "repository"
        workspace.mkdir(parents=False, exist_ok=False)
        return RepositoryJob(
            job_id=job_id,
            repository_name=repository_name,
            workspace=workspace,
            repository_path=repository_path,
            clone_url=clone_url,
        )

    def create_managed_job(self, repository_url: str) -> RepositoryJob:
        """Create the persistent original/current layout used by repository issue fixes."""
        clone_url, repository_name = self.validate_public_github_url(repository_url)
        job_id = uuid.uuid4().hex
        self.settings.workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = self.settings.workspace_root / job_id
        repository_root = workspace / "repo"
        repository_root.mkdir(parents=True, exist_ok=False)
        return RepositoryJob(
            job_id=job_id,
            repository_name=repository_name,
            workspace=workspace,
            repository_path=repository_root / "current",
            original_path=repository_root / "original",
            clone_url=clone_url,
        )

    @staticmethod
    def snapshot_original(job: RepositoryJob) -> None:
        if job.original_path is None:
            return
        if not job.repository_path.is_dir() or job.original_path.exists():
            raise CloneError(
                "The managed repository workspace could not be initialized.",
                job_id=job.job_id,
            )
        shutil.copytree(job.repository_path, job.original_path, symlinks=True)

    def clone(self, job: RepositoryJob) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_LFS_SKIP_SMUDGE": "1",
            }
        )
        command = [
            "git",
            "-c",
            "protocol.file.allow=never",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            job.clone_url,
            str(job.repository_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.settings.clone_timeout_seconds,
                env=environment,
                check=False,
            )
        except FileNotFoundError as exc:
            logger.error("[FixFlow] job=%s Git CLI was not found on PATH", job.job_id)
            raise CloneError(
                "Git is not installed or is not available on PATH.",
                job_id=job.job_id,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "[FixFlow] job=%s git clone timed out after %ss",
                job.job_id,
                self.settings.clone_timeout_seconds,
            )
            raise ExecutionTimeoutError(
                "Repository cloning exceeded the configured timeout.",
                job_id=job.job_id,
            ) from exc

        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip()
            logger.error(
                "[FixFlow] job=%s git clone failed exit_code=%s stderr=%s",
                job.job_id,
                result.returncode,
                " ".join(error.split())[-1000:],
            )
            raise CloneError(
                "Unable to clone the public GitHub repository.",
                job_id=job.job_id,
                details={"git_error": error[-3000:]},
            )
