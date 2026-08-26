import json
from urllib.parse import unquote

import httpx
import pytest

from app.core.exceptions import GeminiAnalysisError
from app.llm.gemini_client import GeminiClient


def install_http_sequence(monkeypatch, outcomes):
    remaining = list(outcomes)
    calls = []

    class SequenceClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def post(self, url, **kwargs):
            calls.append(url)
            request = httpx.Request("POST", url)
            outcome = remaining.pop(0)
            if outcome == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if outcome == "network":
                raise httpx.ConnectError("temporarily unavailable", request=request)
            if outcome == "success":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"text": json.dumps({"ok": True})}
                                    ]
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                outcome,
                request=request,
                text=f"provider returned {outcome}",
            )

    monkeypatch.setattr("app.llm.gemini_client.httpx.Client", SequenceClient)
    return calls


def called_models(calls):
    return [
        unquote(url.split("/models/", 1)[1].split(":generateContent", 1)[0])
        for url in calls
    ]


def client(*, fallback="fallback-model", retries=0):
    return GeminiClient(
        api_key="test-key",
        primary_model="primary-model",
        fallback_model=fallback,
        timeout_seconds=7,
        max_retries=retries,
        retry_backoff_seconds=0,
    )


def test_primary_model_succeeds_immediately(monkeypatch):
    calls = install_http_sequence(monkeypatch, ["success"])

    result = client().analyze("analyze this", job_id="job-primary")

    assert result == {"ok": True}
    assert called_models(calls) == ["primary-model"]


def test_primary_temporary_503_succeeds_on_retry(monkeypatch):
    calls = install_http_sequence(monkeypatch, [503, "success"])

    result = client(retries=1).analyze("analyze this", job_id="job-retry")

    assert result == {"ok": True}
    assert called_models(calls) == ["primary-model", "primary-model"]


def test_primary_exhausts_retries_then_fallback_succeeds(monkeypatch):
    calls = install_http_sequence(monkeypatch, [503, 503, "success"])

    result = client(retries=1).analyze("analyze this", job_id="job-fallback")

    assert result == {"ok": True}
    assert called_models(calls) == [
        "primary-model",
        "primary-model",
        "fallback-model",
    ]


def test_primary_timeout_then_fallback_succeeds(monkeypatch):
    calls = install_http_sequence(monkeypatch, ["timeout", "success"])

    result = client().analyze("analyze this", job_id="job-timeout-fallback")

    assert result == {"ok": True}
    assert called_models(calls) == ["primary-model", "fallback-model"]


def test_temporary_network_failure_then_fallback_succeeds(monkeypatch):
    calls = install_http_sequence(monkeypatch, ["network", "success"])

    result = client().analyze("analyze this", job_id="job-network-fallback")

    assert result == {"ok": True}
    assert called_models(calls) == ["primary-model", "fallback-model"]


def test_both_models_exhaust_temporary_failures(monkeypatch):
    calls = install_http_sequence(monkeypatch, [503, 503, 504, 504])

    with pytest.raises(GeminiAnalysisError) as error:
        client(retries=1).analyze("analyze this", job_id="job-all-unavailable")

    assert error.value.error_code == "gemini_analysis_failed"
    assert error.value.details["status_code"] == 504
    assert error.value.details["model"] == "fallback-model"
    assert called_models(calls) == [
        "primary-model",
        "primary-model",
        "fallback-model",
        "fallback-model",
    ]


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_permanent_auth_and_request_errors_do_not_trigger_fallback(
    monkeypatch,
    status_code,
):
    calls = install_http_sequence(monkeypatch, [status_code])

    with pytest.raises(GeminiAnalysisError) as error:
        client(retries=2).analyze("analyze this", job_id="job-permanent")

    assert error.value.details["status_code"] == status_code
    assert called_models(calls) == ["primary-model"]


def test_missing_fallback_preserves_single_model_behavior(monkeypatch):
    calls = install_http_sequence(monkeypatch, [503, 503])

    with pytest.raises(GeminiAnalysisError):
        client(fallback=None, retries=1).analyze(
            "analyze this",
            job_id="job-no-fallback",
        )

    assert called_models(calls) == ["primary-model", "primary-model"]


def test_duplicate_primary_and_fallback_model_is_attempted_once(monkeypatch):
    calls = install_http_sequence(monkeypatch, [503])
    duplicate_client = GeminiClient(
        api_key="test-key",
        primary_model="same-model",
        fallback_model="same-model",
        max_retries=0,
        retry_backoff_seconds=0,
    )

    with pytest.raises(GeminiAnalysisError):
        duplicate_client.analyze("analyze this", job_id="job-duplicate")

    assert called_models(calls) == ["same-model"]


def test_legacy_model_argument_preserves_single_model_behavior(monkeypatch):
    calls = install_http_sequence(monkeypatch, ["success"])
    legacy_client = GeminiClient(api_key="test-key", model="legacy-model")

    assert legacy_client.analyze("analyze this", job_id="job-legacy") == {
        "ok": True
    }
    assert called_models(calls) == ["legacy-model"]


def test_missing_gemini_key_fails_without_network_call():
    missing_key_client = GeminiClient(
        api_key=None,
        primary_model="primary-model",
        fallback_model="fallback-model",
        timeout_seconds=7,
    )

    with pytest.raises(GeminiAnalysisError) as error:
        missing_key_client.analyze("analyze this", job_id="job-no-key")

    assert "GEMINI_API_KEY" in error.value.message
