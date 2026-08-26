from fastapi.testclient import TestClient

from app.main import app


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
