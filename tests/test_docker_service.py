import subprocess

import pytest

from app.core.exceptions import (
    DockerCLIUnavailableError,
    DockerDaemonUnavailableError,
    DockerTimeoutError,
)
from app.core.exceptions import (
    TestExecutionError as ExecutionError,
)
from app.services.docker_service import DockerService


def test_generated_docker_environment_is_outside_repository(
    tmp_path,
    test_settings,
):
    workspace = tmp_path / "job"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    service = DockerService(test_settings)

    dockerfile = service.prepare_dockerfile(workspace, "requirements")

    assert dockerfile.parent == workspace
    assert dockerfile.parent != repository
    assert not (repository / "Dockerfile").exists()
    assert "pip install -r requirements.txt" in dockerfile.read_text(encoding="utf-8")


def test_test_container_command_has_resource_and_security_limits(
    monkeypatch,
    test_settings,
    tmp_path,
):
    service = DockerService(test_settings, docker_path="/usr/bin/docker")
    captured = {}

    def fake_run(command, *, timeout):
        captured["command"] = command
        captured["timeout"] = timeout
        from subprocess import CompletedProcess

        return CompletedProcess(command, 0, "1 passed", "")

    monkeypatch.setattr(service, "_run_command", fake_run)

    artifacts = tmp_path / "job" / "artifacts"
    result = service.run_pytest(
        job_id="abc123",
        image_tag="fixflow-test:phase1",
        artifacts_path=artifacts,
    )

    assert result.exit_code == 0
    assert captured["command"][0] == "/usr/bin/docker"
    assert captured["command"][1] == "run"
    assert "--rm" in captured["command"]
    assert "exec" not in captured["command"]
    assert "--network" in captured["command"]
    assert "none" in captured["command"]
    assert "--memory" in captured["command"]
    assert "--cpus" in captured["command"]
    assert "--pids-limit" in captured["command"]
    assert "--cap-drop" in captured["command"]
    assert "no-new-privileges=true" in captured["command"]
    assert "--mount" in captured["command"]
    mount = captured["command"][captured["command"].index("--mount") + 1]
    assert str(artifacts.resolve()) in mount
    assert "target=/fixflow-results" in mount
    assert "--junitxml=/fixflow-results/junit.xml" in captured["command"]
    assert result.junit_xml_path == artifacts.resolve() / "junit.xml"
    assert result.junit_xml_path.parent != tmp_path / "job" / "repository"


def test_pytest_exit_code_one_remains_a_valid_test_result(
    monkeypatch,
    test_settings,
    tmp_path,
):
    service = DockerService(test_settings, docker_path="/usr/bin/docker")

    def fake_run(command, *, timeout):
        return subprocess.CompletedProcess(command, 1, "one failed", "")

    monkeypatch.setattr(service, "_run_command", fake_run)

    result = service.run_pytest(
        job_id="job-failed-tests",
        image_tag="fixflow-test:phase1",
        artifacts_path=tmp_path / "artifacts",
    )

    assert result.exit_code == 1


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5])
def test_standard_pytest_exit_codes_are_preserved_for_analysis(
    monkeypatch,
    test_settings,
    tmp_path,
    exit_code,
):
    service = DockerService(test_settings, docker_path="/usr/bin/docker")

    def fake_run(command, *, timeout):
        return subprocess.CompletedProcess(command, exit_code, "pytest result", "")

    monkeypatch.setattr(service, "_run_command", fake_run)

    result = service.run_pytest(
        job_id="job-pytest-result",
        image_tag="fixflow-test:phase1",
        artifacts_path=tmp_path / "artifacts",
    )

    assert result.exit_code == exit_code


def test_unexpected_pytest_exit_code_is_an_internal_error(
    monkeypatch, test_settings, tmp_path
):
    service = DockerService(test_settings, docker_path="/usr/bin/docker")

    def fake_run(command, *, timeout):
        return subprocess.CompletedProcess(command, 17, "unexpected", "")

    monkeypatch.setattr(service, "_run_command", fake_run)

    with pytest.raises(ExecutionError) as error:
        service.run_pytest(
            job_id="job-unexpected-result",
            image_tag="fixflow-test:phase1",
            artifacts_path=tmp_path / "artifacts",
        )

    assert error.value.status_code == 500


def test_docker_cli_missing_has_specific_error(monkeypatch, test_settings):
    service = DockerService(test_settings)
    monkeypatch.setattr("app.services.docker_service.shutil.which", lambda name: None)

    with pytest.raises(DockerCLIUnavailableError) as error:
        service.ensure_available(job_id="job-cli")

    assert error.value.error_code == "docker_cli_unavailable"


def test_docker_daemon_missing_has_specific_error(monkeypatch, test_settings):
    service = DockerService(test_settings)
    monkeypatch.setattr(
        "app.services.docker_service.shutil.which",
        lambda name: "/usr/bin/docker",
    )

    def fake_run(command, *, timeout):
        return subprocess.CompletedProcess(command, 1, "", "Cannot connect to daemon")

    monkeypatch.setattr(service, "_run_command", fake_run)

    with pytest.raises(DockerDaemonUnavailableError) as error:
        service.ensure_available(job_id="job-daemon")

    assert error.value.error_code == "docker_daemon_unavailable"
    assert error.value.details["docker_path"] == "/usr/bin/docker"


def test_docker_detection_uses_resolved_cli_and_info(monkeypatch, test_settings):
    service = DockerService(test_settings)
    captured = {}
    monkeypatch.setattr(
        "app.services.docker_service.shutil.which",
        lambda name: "/usr/bin/docker",
    )

    def fake_run(command, *, timeout):
        captured["command"] = command
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(command, 0, "27.1.1", "")

    monkeypatch.setattr(service, "_run_command", fake_run)

    docker_path = service.ensure_available(job_id="job-ready")

    assert docker_path == "/usr/bin/docker"
    assert captured["command"][:2] == ["/usr/bin/docker", "info"]
    assert captured["timeout"] == test_settings.docker_info_timeout_seconds


def test_docker_pull_timeout_has_specific_error(monkeypatch, test_settings):
    service = DockerService(test_settings, docker_path="/usr/bin/docker")

    def fake_run(command, *, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(service, "_run_command", fake_run)

    with pytest.raises(DockerTimeoutError) as error:
        service.pull_base_image(job_id="job-timeout")

    assert error.value.error_code == "docker_timeout"
    assert error.value.details["operation"] == "docker pull"
