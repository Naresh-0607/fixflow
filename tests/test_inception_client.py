import json

import httpx
import pytest

from app.core.exceptions import MercuryRepairError
from app.llm.inception_client import InceptionClient


def install_inception_response(monkeypatch, outcome):
    calls = []

    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def post(self, url, **kwargs):
            calls.append((url, kwargs, self.timeout))
            request = httpx.Request("POST", url)
            if outcome == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if outcome == "network":
                raise httpx.ConnectError("offline", request=request)
            if isinstance(outcome, int):
                return httpx.Response(outcome, request=request, text="provider error")
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": outcome}}]},
            )

    monkeypatch.setattr("app.llm.inception_client.httpx.Client", FakeClient)
    return calls


def test_mercury_uses_instant_strict_structured_output(monkeypatch):
    patch = {
        "start_line": 4,
        "end_line": 4,
        "replacement": "    return 42",
        "summary": "Correct return value.",
    }
    calls = install_inception_response(monkeypatch, json.dumps(patch))
    client = InceptionClient(api_key="inception-secret", model="mercury-2", timeout_seconds=6)

    result = client.analyze("repair this section", job_id="file-mercury")

    assert result == patch
    url, kwargs, timeout = calls[0]
    assert url == "https://api.inceptionlabs.ai/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer inception-secret"
    payload = kwargs["json"]
    assert payload["model"] == "mercury-2"
    assert payload["reasoning_effort"] == "instant"
    assert payload["stream"] is False
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert timeout.read == 6


def test_mercury_rejects_malformed_json(monkeypatch):
    install_inception_response(monkeypatch, "not-json")
    client = InceptionClient(api_key="test-key")

    with pytest.raises(MercuryRepairError, match="malformed structured JSON"):
        client.analyze("repair", job_id="file-invalid-json")


@pytest.mark.parametrize("outcome", ["timeout", "network", 503])
def test_mercury_transport_failures_are_explicit(monkeypatch, outcome):
    install_inception_response(monkeypatch, outcome)
    client = InceptionClient(api_key="test-key", timeout_seconds=4)

    with pytest.raises(MercuryRepairError):
        client.analyze("repair", job_id="file-provider-failure")


def test_missing_inception_key_never_makes_request(monkeypatch):
    calls = install_inception_response(monkeypatch, "{}")

    with pytest.raises(MercuryRepairError, match="INCEPTION_API_KEY"):
        InceptionClient(api_key=None).analyze("repair", job_id="file-no-key")

    assert calls == []
