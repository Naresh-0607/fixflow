from app.models.responses import TestFailure as FailureModel
from app.models.responses import TestResult as ResultModel
from app.services.analysis_service import AnalysisService


class FakeGeminiClient:
    def __init__(self):
        self.prompt = ""

    def analyze(self, prompt, *, job_id):
        self.prompt = prompt
        return [
            {
                "test": "tests/test_app.py::test_value",
                "what_failed": "The value was incorrect.",
                "why": "The implementation returned 1.",
                "root_cause": "app.logic.value has an incorrect constant.",
                "file": "app/logic.py",
                "symbol": "value",
                "suggested_fix": "Return the expected calculated value.",
                "confidence": "high",
            }
        ]


def test_analysis_selects_failing_test_and_imported_source(tmp_path, test_settings):
    repository = tmp_path / "repository"
    (repository / "tests").mkdir(parents=True)
    (repository / "app").mkdir()
    (repository / "tests" / "test_app.py").write_text(
        "from app.logic import value\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    (repository / "app" / "logic.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    result = ResultModel(
        total=1,
        failed=1,
        status="failed",
        exit_code=1,
        output="FAILED tests/test_app.py::test_value - assert 1 == 2",
        failures=[
            FailureModel(
                test="tests/test_app.py::test_value",
                file="tests/test_app.py",
                error="assert 1 == 2",
            )
        ],
    )
    gemini = FakeGeminiClient()
    service = AnalysisService(test_settings, gemini)

    analysis = service.analyze(
        job_id="job-1",
        repository_path=repository,
        test_result=result,
    )

    assert analysis[0].file == "app/logic.py"
    assert "--- FILE: tests/test_app.py ---" in gemini.prompt
    assert "--- FILE: app/logic.py ---" in gemini.prompt
    assert "COMPLETE PYTEST OUTPUT" in gemini.prompt
