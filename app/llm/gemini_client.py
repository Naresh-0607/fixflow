import json
import time
from typing import Any
from urllib.parse import quote

import httpx

from ..core.exceptions import GeminiAnalysisError
from ..core.logging import log_stage, logger


class GeminiClient:
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    TEMPORARY_STATUS_CODES = frozenset({429, 500, 503, 504})

    def __init__(
        self,
        *,
        api_key: str | None,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 120,
        max_retries: int = 0,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self.api_key = api_key
        self.primary_model = (primary_model or model or "").strip()
        normalized_fallback = (fallback_model or "").strip()
        self.fallback_model = normalized_fallback or None
        self.model = self.primary_model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    def analyze(self, prompt: str, *, job_id: str) -> Any:
        if not self.api_key:
            raise GeminiAnalysisError(
                "GEMINI_API_KEY is not configured. Test results were collected, "
                "but Gemini analysis could not run.",
                job_id=job_id,
            )

        models = self._configured_models()
        if not models:
            raise GeminiAnalysisError(
                "No Gemini model is configured.",
                job_id=job_id,
            )

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        timeout = httpx.Timeout(
            self.timeout_seconds,
            connect=min(10, self.timeout_seconds),
        )
        last_error: GeminiAnalysisError | None = None
        last_cause: Exception | None = None
        for model_index, current_model in enumerate(models):
            for attempt in range(self.max_retries + 1):
                try:
                    response = self._request(
                        model=current_model,
                        payload=payload,
                        timeout=timeout,
                        job_id=job_id,
                        attempt=attempt,
                    )
                    return self._decode_response(
                        response,
                        model=current_model,
                        job_id=job_id,
                    )
                except httpx.TimeoutException as exc:
                    reason = "timeout"
                    last_error = GeminiAnalysisError(
                        "Gemini analysis timed out.",
                        job_id=job_id,
                        details={"model": current_model},
                    )
                    last_cause = exc
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    body = exc.response.text[-2000:]
                    if status not in self.TEMPORARY_STATUS_CODES:
                        logger.error(
                            "[FixFlow] job=%s Gemini API failed model=%s status=%s response=%s",
                            job_id,
                            current_model,
                            status,
                            " ".join(body.split())[-1000:],
                        )
                        raise GeminiAnalysisError(
                            f"Gemini returned HTTP {status}.",
                            job_id=job_id,
                            details={
                                "provider_error": body,
                                "status_code": status,
                                "model": current_model,
                            },
                        ) from exc
                    reason = f"HTTP {status}"
                    last_error = GeminiAnalysisError(
                        f"Gemini returned HTTP {status}.",
                        job_id=job_id,
                        details={
                            "provider_error": body,
                            "status_code": status,
                            "model": current_model,
                        },
                    )
                    last_cause = exc
                except httpx.HTTPError as exc:
                    reason = "connection error"
                    last_error = GeminiAnalysisError(
                        "Unable to contact the Gemini API.",
                        job_id=job_id,
                        details={"model": current_model},
                    )
                    last_cause = exc

                if self._should_retry(attempt):
                    self._backoff(
                        job_id,
                        current_model,
                        attempt,
                        reason,
                    )
                    continue
                log_stage(
                    job_id,
                    "Gemini temporary failure model=%s reason=%s retries_exhausted",
                    current_model,
                    reason,
                )
                break

            if model_index == 0:
                log_stage(job_id, "primary Gemini model unavailable")
                if len(models) > 1:
                    log_stage(
                        job_id,
                        "switching to fallback Gemini model=%s",
                        models[1],
                    )
            else:
                log_stage(job_id, "fallback Gemini model unavailable")

        if last_error is None:
            raise GeminiAnalysisError(
                "Gemini did not return a response.",
                job_id=job_id,
            )
        raise last_error from last_cause

    def _request(
        self,
        *,
        model: str,
        payload: dict[str, object],
        timeout: httpx.Timeout,
        job_id: str,
        attempt: int,
    ) -> httpx.Response:
        endpoint = f"{self.BASE_URL}/{quote(model, safe='')}:generateContent"
        log_stage(
            job_id,
            "Gemini HTTP request started model=%s attempt=%s",
            model,
            attempt + 1,
        )
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                endpoint,
                headers={
                    "x-goog-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        log_stage(
            job_id,
            "Gemini HTTP response received model=%s status_code=%s",
            model,
            response.status_code,
        )
        response.raise_for_status()
        return response

    def _decode_response(
        self,
        response: httpx.Response,
        *,
        model: str,
        job_id: str,
    ) -> Any:
        try:
            body = response.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            return self._decode_json(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            logger.error(
                "[FixFlow] job=%s Gemini returned invalid JSON model=%s error=%s",
                job_id,
                model,
                exc,
            )
            raise GeminiAnalysisError(
                "Gemini returned an invalid or blocked analysis response.",
                job_id=job_id,
                details={"model": model},
            ) from exc

    @staticmethod
    def _decode_json(text: str) -> Any:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```")
            cleaned = cleaned.removesuffix("```").strip()
        return json.loads(cleaned)

    def _should_retry(self, attempt: int) -> bool:
        return attempt < self.max_retries

    def _configured_models(self) -> list[str]:
        models: list[str] = []
        for model in (self.primary_model, self.fallback_model):
            if model and model not in models:
                models.append(model)
        return models

    def _backoff(
        self,
        job_id: str,
        model: str,
        attempt: int,
        reason: str,
    ) -> None:
        delay = self.retry_backoff_seconds * (2**attempt)
        log_stage(
            job_id,
            "Gemini temporary failure model=%s reason=%s retry_in=%.2fs",
            model,
            reason,
            delay,
        )
        if delay:
            time.sleep(delay)
