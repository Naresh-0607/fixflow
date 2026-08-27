import ast
import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import files as files_api
from app.core.exceptions import GeminiAnalysisError, MercuryRepairError
from app.main import app
from app.models.file_workspace import FileCodeMap
from app.services import file_workspace_service
from app.services.file_workspace_service import FileAnalysisEngine, FileWorkspaceStore


class SequenceGemini:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def analyze(self, prompt, *, job_id):
        self.prompts.append((job_id, prompt))
        response = self.responses.pop(0) if self.responses else {"issues": []}
        if isinstance(response, Exception):
            raise response
        return response


def install_file_api(monkeypatch, test_settings, gemini, mercury=None):
    monkeypatch.setattr(files_api, "settings", test_settings)
    monkeypatch.setattr(files_api, "_gemini_client", lambda **_: gemini)
    if mercury is not None:
        monkeypatch.setattr(files_api, "_mercury_client", lambda: mercury)
    return TestClient(app)


def test_python_upload_preserves_original_and_returns_structured_issues(
    monkeypatch, test_settings
):
    gemini = SequenceGemini(
        {
            "issues": [
                {
                    "line": 1,
                    "end_line": 1,
                    "severity": "high",
                    "type": "validation",
                    "title": "Input is not checked",
                    "description": "The input accepts unsupported values.",
                    "suggestion": "Validate the input first.",
                }
            ]
        }
    )
    client = install_file_api(monkeypatch, test_settings, gemini)
    source = b"def calculate(a, b):\n    return a / b\n"

    response = client.post(
        "/api/files/analyze", files={"file": ("calculator.py", source, "text/x-python")}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "calculator.py"
    assert payload["source"].encode() == source
    assert payload["issues"]
    assert all(
        {"id", "line", "end_line", "severity", "type", "title", "description", "suggestion"}
        <= issue.keys()
        for issue in payload["issues"]
    )
    original = (
        test_settings.workspace_root
        / "files"
        / payload["file_id"]
        / "original"
        / "calculator.py"
    )
    assert original.read_bytes() == source


def test_syntax_error_is_detected_without_executing_source(monkeypatch, test_settings):
    client = install_file_api(monkeypatch, test_settings, SequenceGemini({"issues": []}))
    response = client.post(
        "/api/files/analyze",
        files={"file": ("broken.py", b"def broken(:\n    pass\n", "text/x-python")},
    )
    assert response.status_code == 200
    syntax = next(issue for issue in response.json()["issues"] if issue["type"] == "syntax")
    assert syntax["severity"] == "critical"
    assert syntax["line"] == 1


def test_upload_sanitizes_safe_filename_characters(monkeypatch, test_settings):
    client = install_file_api(monkeypatch, test_settings, SequenceGemini({"issues": []}))
    response = client.post(
        "/api/files/analyze",
        files={"file": ("my@module.py", b"answer = 42\n", "text/x-python")},
    )
    assert response.status_code == 200
    assert response.json()["filename"] == "my_module.py"


def test_path_traversal_filename_is_rejected(monkeypatch, test_settings):
    client = install_file_api(monkeypatch, test_settings, SequenceGemini())
    response = client.post(
        "/api/files/analyze",
        files={"file": ("../escape.py", b"value = 1\n", "text/x-python")},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_file_upload"
    assert not (test_settings.workspace_root / "escape.py").exists()


def test_oversized_file_is_rejected(monkeypatch, test_settings):
    limited = replace(test_settings, max_file_upload_bytes=16)
    client = install_file_api(monkeypatch, limited, SequenceGemini())
    response = client.post(
        "/api/files/analyze",
        files={"file": ("large.py", b"x" * 17, "text/x-python")},
    )
    assert response.status_code == 413
    assert response.json()["error"] == "file_too_large"


def test_binary_file_is_rejected(monkeypatch, test_settings):
    client = install_file_api(monkeypatch, test_settings, SequenceGemini())
    response = client.post(
        "/api/files/analyze",
        files={"file": ("binary.py", b"value = 1\x00\xff", "application/octet-stream")},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_file_upload"


def test_gemini_failure_keeps_local_analysis(monkeypatch, test_settings):
    unavailable = GeminiAnalysisError("provider unavailable")
    client = install_file_api(monkeypatch, test_settings, SequenceGemini(unavailable))
    response = client.post(
        "/api/files/analyze",
        files={"file": ("risk.py", b"result = 10 / divisor\n", "text/x-python")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["gemini_status"] == "unavailable"
    assert any(issue["type"] == "runtime_risk" for issue in payload["issues"])


def test_fix_validates_reanalyzes_diffs_and_preserves_original(
    monkeypatch, test_settings
):
    original = b"def calculate(a, b):\n    return a / b\n"
    repaired = (
        "def calculate(a, b):\n"
        "    if b == 0:\n"
        "        raise ValueError('b cannot be zero')\n"
        "    return a / b\n"
    )
    gemini = SequenceGemini(
        {"issues": []},
        {"fixed_source": repaired, "summary": "Validate the divisor."},
        {"issues": []},
    )
    client = install_file_api(monkeypatch, test_settings, gemini)
    analyzed = client.post(
        "/api/files/analyze",
        files={"file": ("calculator.py", original, "text/x-python")},
    ).json()

    response = client.post(f"/api/files/{analyzed['file_id']}/fix")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "fixed"
    assert payload["after_count"] == 0
    assert payload["issues_fixed"] >= 1
    assert "+    if b == 0:" in payload["diff"]
    assert "     return a / b" in payload["diff"]
    assert payload["lines_changed"] > 0
    root = test_settings.workspace_root / "files" / analyzed["file_id"]
    assert (root / "original" / "calculator.py").read_bytes() == original
    assert (root / "current" / "calculator.py").read_text(encoding="utf-8") == repaired


def test_fixed_file_download_preserves_filename(monkeypatch, test_settings):
    repaired = "answer = 42\n"
    gemini = SequenceGemini(
        {"issues": [{"line": 1, "severity": "low", "type": "logic", "title": "Use answer", "description": "Set answer.", "suggestion": "Set it."}]},
        {"fixed_source": repaired},
        {"issues": []},
    )
    client = install_file_api(monkeypatch, test_settings, gemini)
    analyzed = client.post(
        "/api/files/analyze", files={"file": ("answer.py", b"answer = 0\n", "text/x-python")}
    ).json()
    assert client.post(f"/api/files/{analyzed['file_id']}/fix").status_code == 200

    response = client.get(f"/api/files/{analyzed['file_id']}/download")

    assert response.status_code == 200
    assert response.content == repaired.encode()
    assert 'filename="answer.py"' in response.headers["content-disposition"]
    assert "zip" not in response.headers["content-type"]


def test_file_chat_uses_only_file_context(monkeypatch, test_settings):
    gemini = SequenceGemini({"issues": []}, {"answer": "Line 1 returns a constant."})
    client = install_file_api(monkeypatch, test_settings, gemini)
    analyzed = client.post(
        "/api/files/analyze", files={"file": ("answer.py", b"answer = 42\n", "text/x-python")}
    ).json()
    response = client.post(
        f"/api/files/{analyzed['file_id']}/chat",
        json={"message": "What does line 1 do?"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "file_id": analyzed["file_id"],
        "answer": "Line 1 returns a constant.",
        "read_only": True,
    }
    assert "answer.py" in gemini.prompts[-1][1]
    assert "repository" not in gemini.prompts[-1][1].lower()


def test_chat_modification_request_is_read_only(monkeypatch, test_settings):
    gemini = SequenceGemini({"issues": []})
    client = install_file_api(monkeypatch, test_settings, gemini)
    analyzed = client.post(
        "/api/files/analyze", files={"file": ("safe.py", b"answer = 42\n", "text/x-python")}
    ).json()
    current = test_settings.workspace_root / "files" / analyzed["file_id"] / "current" / "safe.py"
    checksum = current.read_bytes()

    response = client.post(
        f"/api/files/{analyzed['file_id']}/chat",
        json={"message": "Change line 1 and rewrite this code"},
    )

    assert response.status_code == 200
    assert "Fix Now" in response.json()["answer"]
    assert current.read_bytes() == checksum
    assert len(gemini.prompts) == 1


def test_chat_can_explain_previous_changes(monkeypatch, test_settings):
    gemini = SequenceGemini({"issues": []}, {"answer": "I added a validation guard."})
    client = install_file_api(monkeypatch, test_settings, gemini)
    analyzed = client.post(
        "/api/files/analyze", files={"file": ("safe.py", b"answer = 42\n", "text/x-python")}
    ).json()
    response = client.post(
        f"/api/files/{analyzed['file_id']}/chat",
        json={"message": "What did you change?"},
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "I added a validation guard."


def test_api_key_is_never_returned(monkeypatch, test_settings):
    secret = "super-secret-gemini-key"
    configured = replace(test_settings, gemini_api_key=secret)
    client = install_file_api(monkeypatch, configured, SequenceGemini({"issues": []}))
    response = client.post(
        "/api/files/analyze", files={"file": ("safe.py", b"answer = 42\n", "text/x-python")}
    )
    assert response.status_code == 200
    assert secret not in response.text


def test_expired_file_workspace_cleanup(test_settings):
    settings = replace(test_settings, file_workspace_ttl_seconds=1)
    store = FileWorkspaceStore(settings)
    workspace = store.create("old.py", b"value = 1\n")
    metadata = store.read_metadata(workspace)
    metadata["updated_at"] = 0
    store.write_metadata(workspace, metadata)
    store.cleanup_expired()
    assert not workspace.root.exists()


def test_static_analysis_detects_undefined_names(test_settings):
    engine = FileAnalysisEngine(test_settings, SequenceGemini({"issues": []}))
    issues, _, _ = engine.analyze_source(
        file_id="a" * 32,
        filename="undefined.py",
        source="def get_value():\n    return missing_value\n",
    )
    assert any(issue.title == "Undefined name: missing_value" for issue in issues)


def test_sha256_cache_reuses_only_identical_source(monkeypatch, test_settings):
    gemini = SequenceGemini({"issues": []})
    client = install_file_api(monkeypatch, test_settings, gemini)

    first = client.post(
        "/api/files/analyze", files={"file": ("one.py", b"value = 1\n", "text/x-python")}
    ).json()
    second = client.post(
        "/api/files/analyze", files={"file": ("two.py", b"value = 1\n", "text/x-python")}
    ).json()
    assert len(gemini.prompts) == 1

    modified = client.post(
        "/api/files/analyze", files={"file": ("one.py", b"value = 2\n", "text/x-python")}
    ).json()

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["sha256"] == second["sha256"]
    assert first["file_id"] != second["file_id"]
    assert modified["cache_hit"] is False
    assert modified["sha256"] != first["sha256"]
    assert len(gemini.prompts) == 2


def test_code_map_and_severity_ranking_are_returned(monkeypatch, test_settings):
    source = b"import os\n\nclass Worker:\n    def run(self, divisor):\n        return eval('1') / divisor\n"
    client = install_file_api(monkeypatch, test_settings, SequenceGemini({"issues": []}))

    payload = client.post(
        "/api/files/analyze", files={"file": ("worker.py", source, "text/x-python")}
    ).json()

    assert payload["code_map"]["imports"][0]["names"] == ["os"]
    assert payload["code_map"]["classes"][0]["qualified_name"] == "Worker"
    function = payload["code_map"]["functions"][0]
    assert function["qualified_name"] == "Worker.run"
    assert function["line"] == 4 and function["end_line"] == 5
    assert function["complexity"] >= 1
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    severities = [order[issue["severity"]] for issue in payload["issues"]]
    assert severities == sorted(severities)


def test_large_file_sends_one_bounded_gemini_prompt(monkeypatch, test_settings):
    source = "\n".join(
        ["def suspicious(value):", "    return 10 / value", ""]
        + [f"padding_{line} = {line}" for line in range(4, 1001)]
    )
    gemini = SequenceGemini({"issues": []})
    client = install_file_api(monkeypatch, test_settings, gemini)

    response = client.post(
        "/api/files/analyze",
        files={"file": ("large.py", source.encode(), "text/x-python")},
    )

    assert response.status_code == 200
    assert len(gemini.prompts) == 1
    prompt = gemini.prompts[0][1]
    assert "L2:     return 10 / value" in prompt
    assert "padding_900 = 900" not in prompt
    assert len(prompt) < len(source) + 10_000
    assert "at most 10 issues" in prompt


def test_gemini_timeout_returns_local_findings(monkeypatch, test_settings):
    class SlowGemini:
        def analyze(self, prompt, *, job_id):
            time.sleep(0.1)
            return {"issues": []}

    monkeypatch.setattr(file_workspace_service, "GEMINI_FILE_TIMEOUT_SECONDS", 0.01)
    client = install_file_api(monkeypatch, test_settings, SlowGemini())

    payload = client.post(
        "/api/files/analyze",
        files={"file": ("risk.py", b"result = 10 / divisor\n", "text/x-python")},
    ).json()

    assert payload["gemini_status"] == "timed_out"
    assert "timed out" in payload["gemini_error"].lower()
    assert any(issue["source"] == "ast" for issue in payload["issues"])


def test_fix_now_applies_ruff_safe_fix_without_gemini_repair(monkeypatch, test_settings):
    gemini = SequenceGemini({"issues": []})
    client = install_file_api(monkeypatch, test_settings, gemini)
    original = b"import os\n\nanswer = 42\n"
    analyzed = client.post(
        "/api/files/analyze",
        files={"file": ("unused.py", original, "text/x-python")},
    ).json()

    assert any(issue["source"] == "ruff" for issue in analyzed["issues"])
    selected = next(issue for issue in analyzed["issues"] if issue["rule"] == "F401")
    response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "\nanswer = 42\n"
    assert "-import os" in payload["diff"]
    assert payload["success"] is True
    assert payload["issue_id"] == selected["issue_id"]
    assert payload["fixed_code"] == payload["source"]
    assert payload["summary"] == "Applied Ruff's safe F401 fix."
    assert len(gemini.prompts) == 1


def test_local_scanners_are_scheduled_concurrently(monkeypatch, test_settings):
    engine = FileAnalysisEngine(test_settings, SequenceGemini({"issues": []}))

    def ast_scan(source, filename):
        time.sleep(0.08)
        return [], FileCodeMap()

    def local_scan(source, filename):
        time.sleep(0.08)
        return []

    async def ruff_scan(source, filename):
        await asyncio.sleep(0.08)
        return []

    monkeypatch.setattr(engine, "_ast_analysis", ast_scan)
    monkeypatch.setattr(engine, "_security_analysis", local_scan)
    monkeypatch.setattr(engine, "_complexity_analysis", local_scan)
    monkeypatch.setattr(engine, "_ruff_analysis", ruff_scan)

    started = time.perf_counter()
    asyncio.run(engine._concurrent_local_analysis("value = 1\n", "value.py"))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.25


def test_findings_have_stable_normalized_issue_ids(monkeypatch, test_settings):
    gemini = SequenceGemini({"issues": []})
    client = install_file_api(monkeypatch, test_settings, gemini)
    source = b"import os\nvalue = 1\n"

    first = client.post(
        "/api/files/analyze", files={"file": ("stable.py", source, "text/x-python")}
    ).json()
    cached = client.post(
        "/api/files/analyze", files={"file": ("renamed.py", source, "text/x-python")}
    ).json()

    required = {
        "issue_id",
        "source",
        "rule",
        "category",
        "line",
        "end_line",
        "message",
        "severity",
        "fixable",
    }
    assert first["issues"]
    assert all(required <= issue.keys() for issue in first["issues"])
    assert len({issue["issue_id"] for issue in first["issues"]}) == len(first["issues"])
    assert [issue["issue_id"] for issue in cached["issues"]] == [
        issue["issue_id"] for issue in first["issues"]
    ]


def test_targeted_ai_fix_applies_to_current_workspace_and_reanalyzes(
    monkeypatch, test_settings
):
    original = b"def answer():\n    return 0\n"
    gemini = SequenceGemini(
        {
            "issues": [
                {
                    "line": 2,
                    "severity": "high",
                    "category": "logic",
                    "message": "The answer is incorrect",
                    "suggestion": "Return 42.",
                }
            ]
        },
        {
            "replacement": {
                "start_line": 2,
                "end_line": 2,
                "code": "    return 42",
            },
            "summary": "Correct the answer.",
        },
        {"issues": []},
    )
    client = install_file_api(monkeypatch, test_settings, gemini)
    analyzed = client.post(
        "/api/files/analyze",
        files={"file": ("answer.py", original, "text/x-python")},
    ).json()
    selected = next(issue for issue in analyzed["issues"] if issue["source"] == "gemini")

    response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["fixed_code"] == "def answer():\n    return 42\n"
    ast.parse(payload["fixed_code"])
    assert payload["analysis"]["issues"] == []
    assert payload["selected_issue_resolved"] is True
    assert "+    return 42" in payload["diff"]
    root = test_settings.workspace_root / "files" / analyzed["file_id"]
    assert (root / "original" / "answer.py").read_bytes() == original
    assert (root / "current" / "answer.py").read_text(encoding="utf-8") == payload["fixed_code"]
    repair_prompt = gemini.prompts[1][1]
    assert selected["issue_id"] in repair_prompt
    assert "Containing symbol: answer" in repair_prompt
    assert "Current source section" in repair_prompt


def test_fix_invalidates_old_cache_and_stores_new_analysis(monkeypatch, test_settings):
    original = b"import os\nanswer = 42\n"
    gemini = SequenceGemini({"issues": []}, {"issues": []})
    client = install_file_api(monkeypatch, test_settings, gemini)
    analyzed = client.post(
        "/api/files/analyze",
        files={"file": ("cache_fix.py", original, "text/x-python")},
    ).json()
    selected = next(issue for issue in analyzed["issues"] if issue["rule"] == "F401")
    cache = file_workspace_service.FileAnalysisCache(test_settings)
    assert cache.read(analyzed["sha256"]) is not None

    payload = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    ).json()

    assert payload["old_sha256"] == analyzed["sha256"]
    assert payload["new_sha256"] != payload["old_sha256"]
    assert cache.read(payload["old_sha256"]) is None
    new_cached = cache.read(payload["new_sha256"])
    assert new_cached is not None
    assert new_cached["issues"] == payload["analysis"]["issues"]
    assert all(issue["issue_id"] != selected["issue_id"] for issue in payload["analysis"]["issues"])


def test_invalid_ai_replacement_is_rejected_without_changing_workspace(
    monkeypatch, test_settings
):
    original = b"def answer():\n    return 0\n"
    gemini = SequenceGemini(
        {"issues": [{"line": 2, "severity": "high", "category": "logic", "message": "Wrong result", "suggestion": "Fix it."}]},
        {"replacement": {"start_line": 2, "end_line": 2, "code": "    return ("}},
    )
    client = install_file_api(monkeypatch, test_settings, gemini)
    analyzed = client.post(
        "/api/files/analyze", files={"file": ("invalid.py", original, "text/x-python")}
    ).json()
    selected = next(issue for issue in analyzed["issues"] if issue["source"] == "gemini")

    response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    )

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert "syntax" in response.json()["reason"].lower()
    root = test_settings.workspace_root / "files" / analyzed["file_id"]
    assert (root / "current" / "invalid.py").read_bytes() == original
    assert (root / "original" / "invalid.py").read_bytes() == original


def test_unchanged_ai_repair_never_reports_success(monkeypatch, test_settings):
    original = b"def answer():\n    return 0\n"
    gemini = SequenceGemini(
        {"issues": [{"line": 2, "severity": "medium", "category": "logic", "message": "Review result", "suggestion": "Fix it."}]},
        {"replacement": {"start_line": 2, "end_line": 2, "code": "    return 0"}},
    )
    client = install_file_api(monkeypatch, test_settings, gemini)
    analyzed = client.post(
        "/api/files/analyze", files={"file": ("unchanged.py", original, "text/x-python")}
    ).json()
    selected = next(issue for issue in analyzed["issues"] if issue["source"] == "gemini")

    response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert "does not change" in payload["reason"]


def test_multiple_sequential_fixes_use_latest_current_source(monkeypatch, test_settings):
    original = b"import os\ndef calculate(a, b):\n    return a / b\n"
    configured = replace(test_settings, inception_api_key="test-inception-key")
    gemini = SequenceGemini({"issues": []})
    mercury = SequenceGemini(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    if b == 0:\n        raise ValueError('b cannot be zero')\n    return a / b",
            "summary": "Guard the divisor.",
        },
    )
    client = install_file_api(monkeypatch, configured, gemini, mercury)
    analyzed = client.post(
        "/api/files/analyze", files={"file": ("sequential.py", original, "text/x-python")}
    ).json()
    ruff_issue = next(issue for issue in analyzed["issues"] if issue["rule"] == "F401")

    first = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": ruff_issue["issue_id"]},
    ).json()
    runtime_issue = next(
        issue for issue in first["analysis"]["issues"] if issue["type"] == "runtime_risk"
    )
    second_response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": runtime_issue["issue_id"]},
    )

    assert second_response.status_code == 200
    second = second_response.json()
    assert second["success"] is True
    assert "import os" not in second["fixed_code"]
    assert "if b == 0:" in second["fixed_code"]
    assert second["old_sha256"] == first["new_sha256"]
    ast.parse(second["fixed_code"])
    root = test_settings.workspace_root / "files" / analyzed["file_id"]
    assert (root / "current" / "sequential.py").read_text(encoding="utf-8") == second["fixed_code"]
    assert (root / "original" / "sequential.py").read_bytes() == original
    assert second["repair_provider"] == "mercury"
    assert "import os" not in mercury.prompts[0][1]
    assert len(gemini.prompts) == 1
    assert len(mercury.prompts) == 1


def test_standalone_frontend_sends_file_and_issue_ids():
    script = (Path(__file__).parents[1] / "frontend" / "file-analyzer.js").read_text(
        encoding="utf-8"
    )

    assert "`/api/files/${state.fileId}/fix`" in script
    assert "JSON.stringify({ issue_id: requestedIssueId })" in script
    assert "payload.fixed_code || payload.source" in script
    assert "payload.analysis?.issues || payload.issues" in script


def test_mercury_repairs_non_ruff_issue_with_bounded_context(monkeypatch, test_settings):
    configured = replace(test_settings, inception_api_key="test-inception-key")
    source = (
        "def answer():\n"
        "    return 0\n\n"
        + "\n".join(f"unrelated_{line} = {line}" for line in range(4, 301))
        + "\n"
    ).encode()
    gemini = SequenceGemini(
        {
            "issues": [
                {
                    "line": 2,
                    "severity": "high",
                    "category": "logic",
                    "message": "The answer is incorrect",
                    "suggestion": "Return 42.",
                }
            ]
        }
    )
    mercury = SequenceGemini(
        {
            "start_line": 2,
            "end_line": 2,
            "replacement": "    return 42",
            "summary": "Correct return value.",
        }
    )
    client = install_file_api(monkeypatch, configured, gemini, mercury)
    analyzed = client.post(
        "/api/files/analyze", files={"file": ("mercury.py", source, "text/x-python")}
    ).json()
    selected = next(issue for issue in analyzed["issues"] if issue["source"] == "gemini")

    response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["repair_provider"] == "mercury"
    assert payload["analysis"]["gemini_status"] == "skipped"
    assert "return 42" in payload["fixed_code"]
    assert len(mercury.prompts) == 1
    assert len(gemini.prompts) == 1
    prompt = mercury.prompts[0][1]
    assert selected["issue_id"] in prompt
    assert "L2:     return 0" in prompt
    assert "unrelated_250" not in prompt
    assert len(prompt) < len(source.decode())


def test_ruff_safe_fix_does_not_call_mercury(monkeypatch, test_settings):
    configured = replace(test_settings, inception_api_key="test-inception-key")
    gemini = SequenceGemini({"issues": []})
    mercury = SequenceGemini(
        AssertionError("Mercury must not run for a Ruff-safe finding")
    )
    client = install_file_api(monkeypatch, configured, gemini, mercury)
    analyzed = client.post(
        "/api/files/analyze",
        files={"file": ("ruff_only.py", b"import os\nanswer = 42\n", "text/x-python")},
    ).json()
    selected = next(issue for issue in analyzed["issues"] if issue["rule"] == "F401")

    response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    )

    assert response.status_code == 200
    assert response.json()["repair_provider"] == "ruff"
    assert mercury.prompts == []
    assert len(gemini.prompts) == 1


@pytest.mark.parametrize(
    "mercury_failure",
    [
        MercuryRepairError("Mercury repair timed out."),
        MercuryRepairError("Unable to contact the Inception API."),
    ],
)
def test_mercury_transport_failure_falls_back_to_gemini(
    monkeypatch, test_settings, mercury_failure
):
    configured = replace(test_settings, inception_api_key="test-inception-key")
    gemini = SequenceGemini(
        {"issues": [{"line": 2, "severity": "high", "category": "logic", "message": "Wrong result", "suggestion": "Return 42."}]},
        {"replacement": {"start_line": 2, "end_line": 2, "code": "    return 42"}, "summary": "Fallback repair."},
    )
    mercury = SequenceGemini(mercury_failure)
    client = install_file_api(monkeypatch, configured, gemini, mercury)
    analyzed = client.post(
        "/api/files/analyze",
        files={"file": ("fallback.py", b"def answer():\n    return 0\n", "text/x-python")},
    ).json()
    selected = next(issue for issue in analyzed["issues"] if issue["source"] == "gemini")

    response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    )

    assert response.status_code == 200
    assert response.json()["repair_provider"] == "gemini"
    assert "return 42" in response.json()["fixed_code"]
    assert len(mercury.prompts) == 1
    assert len(gemini.prompts) == 2


@pytest.mark.parametrize(
    "mercury_patch",
    [
        {"start_line": 2, "end_line": 2, "replacement": "    return (", "summary": "Invalid."},
        {"start_line": 2, "end_line": 2, "replacement": "    return 0", "summary": "No-op."},
    ],
)
def test_unusable_mercury_patch_falls_back_to_gemini(
    monkeypatch, test_settings, mercury_patch
):
    configured = replace(test_settings, inception_api_key="test-inception-key")
    gemini = SequenceGemini(
        {"issues": [{"line": 2, "severity": "high", "category": "logic", "message": "Wrong result", "suggestion": "Return 42."}]},
        {"replacement": {"start_line": 2, "end_line": 2, "code": "    return 42"}},
    )
    mercury = SequenceGemini(mercury_patch)
    client = install_file_api(monkeypatch, configured, gemini, mercury)
    analyzed = client.post(
        "/api/files/analyze",
        files={"file": ("fallback_patch.py", b"def answer():\n    return 0\n", "text/x-python")},
    ).json()
    selected = next(issue for issue in analyzed["issues"] if issue["source"] == "gemini")

    response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    )

    assert response.status_code == 200
    assert response.json()["repair_provider"] == "gemini"
    assert "return 42" in response.json()["fixed_code"]


def test_both_ai_repair_providers_fail_explicitly(monkeypatch, test_settings):
    configured = replace(test_settings, inception_api_key="test-inception-key")
    gemini = SequenceGemini(
        {"issues": [{"line": 2, "severity": "high", "category": "logic", "message": "Wrong result", "suggestion": "Return 42."}]},
        GeminiAnalysisError("Gemini fallback unavailable."),
    )
    mercury = SequenceGemini(MercuryRepairError("Mercury unavailable."))
    client = install_file_api(monkeypatch, configured, gemini, mercury)
    analyzed = client.post(
        "/api/files/analyze",
        files={"file": ("both_fail.py", b"def answer():\n    return 0\n", "text/x-python")},
    ).json()
    selected = next(issue for issue in analyzed["issues"] if issue["source"] == "gemini")

    response = client.post(
        f"/api/files/{analyzed['file_id']}/fix",
        json={"issue_id": selected["issue_id"]},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert "mercury:" in payload["reason"]
    assert "gemini:" in payload["reason"]
