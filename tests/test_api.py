from fastapi.testclient import TestClient

from app.api import analysis as analysis_api
from app.core.exceptions import DockerExecutionError
from app.main import app
from app.services.repository_service import RepositoryJob


def test_health_endpoint_reports_analysis_only_phase():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "phase": "analysis-only"}


def test_frontend_and_static_assets_are_served():
    client = TestClient(app)

    page = client.get("/")
    stylesheet = client.get("/static/styles.css")
    script = client.get("/static/app.js")

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "FixFlow" in page.text
    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.headers["content-type"]
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


def test_analyze_rejects_non_github_repository_url():
    response = TestClient(app).post(
        "/api/analyze",
        json={"repository_url": "https://example.com/project.git"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_repository_url"


def test_repair_rejects_non_github_repository_url():
    response = TestClient(app).post(
        "/api/repair",
        json={"repository_url": "https://example.com/project.git"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_repository_url"


def test_dependency_install_failure_returns_clear_analysis_state(
    monkeypatch, test_settings, tmp_path
):
    workspace = tmp_path / "job"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    job = RepositoryJob(
        job_id="dependency-failure",
        repository_name="example",
        workspace=workspace,
        repository_path=repository,
        clone_url="https://github.com/openai/example.git",
    )

    monkeypatch.setattr(analysis_api, "settings", test_settings)
    monkeypatch.setattr(analysis_api.RepositoryService, "clone", lambda self, job: None)
    monkeypatch.setattr(
        analysis_api.DockerService,
        "ensure_available",
        lambda self, **kwargs: "docker",
    )
    monkeypatch.setattr(
        analysis_api.DockerService,
        "pull_base_image",
        lambda self, **kwargs: None,
    )
    monkeypatch.setattr(
        analysis_api.DockerService,
        "prepare_dockerfile",
        lambda self, workspace, strategy: workspace / "Dockerfile.fixflow",
    )

    def fail_build(self, **kwargs):
        raise DockerExecutionError(
            "Docker could not install dependencies or build the test image.",
            job_id=job.job_id,
            details={"operation": "docker build", "docker_output": "pip failed"},
        )

    monkeypatch.setattr(analysis_api.DockerService, "build_image", fail_build)

    result = analysis_api.run_analysis_job(job)

    assert result.status == "analysis_failed"
    assert result.docker_status == "failed"
    assert result.tests.status == "error"
    assert result.tests.output == "pip failed"
    assert "installing dependencies" in result.message.lower()
    assert repository.is_dir()
