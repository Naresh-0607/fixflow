import pytest

from app.core.exceptions import InvalidRepositoryUrl
from app.services.repository_service import RepositoryService


def test_valid_github_url_is_canonicalized(test_settings):
    service = RepositoryService(test_settings)

    url, name = service.validate_public_github_url(
        "https://github.com/openai/example.git"
    )

    assert url == "https://github.com/openai/example.git"
    assert name == "example"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/openai/example.git",
        "https://example.com/openai/example.git",
        "https://github.com/openai/example/issues",
        "https://user:secret@github.com/openai/example.git",
        "https://github.com/openai/example.git?token=secret",
        "file:///tmp/repository",
    ],
)
def test_unsafe_repository_urls_are_rejected(test_settings, url):
    service = RepositoryService(test_settings)

    with pytest.raises(InvalidRepositoryUrl):
        service.validate_public_github_url(url)


def test_each_job_has_an_isolated_workspace(test_settings):
    service = RepositoryService(test_settings)

    first = service.create_job("https://github.com/openai/example.git")
    second = service.create_job("https://github.com/openai/example.git")

    assert first.job_id != second.job_id
    assert first.workspace.is_dir()
    assert second.workspace.is_dir()
    assert first.repository_path.parent == first.workspace
