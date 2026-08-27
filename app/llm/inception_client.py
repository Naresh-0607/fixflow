import json
from typing import Any, ClassVar

import httpx

from ..core.exceptions import MercuryRepairError
from ..core.logging import log_stage


class InceptionClient:
    """Low-latency Mercury client for targeted single-file repairs only."""

    ENDPOINT = "https://api.inceptionlabs.ai/v1/chat/completions"
    PATCH_SCHEMA: ClassVar[dict[str, Any]] = {
        "name": "FixFlowTargetedPatch",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "replacement": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["start_line", "end_line", "replacement", "summary"],
            "additionalProperties": False,
        },
    }

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "mercury-2",
        timeout_seconds: int = 6,
    ) -> None:
        self.api_key = api_key
        self.model = model.strip() or "mercury-2"
        self.timeout_seconds = max(1, timeout_seconds)

    def analyze(self, prompt: str, *, job_id: str) -> dict[str, Any]:
        if not self.api_key:
            raise MercuryRepairError(
                "INCEPTION_API_KEY is not configured.", job_id=job_id
            )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only the requested minimal Python patch as strict JSON. "
                        "Do not include markdown or unrelated edits."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "reasoning_effort": "instant",
            "max_tokens": 2048,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": self.PATCH_SCHEMA,
            },
        }
        timeout = httpx.Timeout(
            self.timeout_seconds,
            connect=min(3, self.timeout_seconds),
        )
        log_stage(job_id, "Mercury repair request started model=%s", self.model)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    self.ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise MercuryRepairError(
                "Mercury repair timed out.",
                job_id=job_id,
                details={"model": self.model},
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise MercuryRepairError(
                f"Mercury returned HTTP {exc.response.status_code}.",
                job_id=job_id,
                details={"model": self.model, "status_code": exc.response.status_code},
            ) from exc
        except httpx.HTTPError as exc:
            raise MercuryRepairError(
                "Unable to contact the Inception API.",
                job_id=job_id,
                details={"model": self.model},
            ) from exc
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise MercuryRepairError(
                "Mercury returned malformed structured JSON.",
                job_id=job_id,
                details={"model": self.model},
            ) from exc
        if not isinstance(result, dict):
            raise MercuryRepairError(
                "Mercury returned an unusable patch.",
                job_id=job_id,
                details={"model": self.model},
            )
        return result
