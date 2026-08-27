from app.services.docker_service import DockerRunResult
from app.services.pytest_service import PytestService

FAILED_OUTPUT = """
============================= test session starts =============================
collected 3 items

tests/test_math.py::test_add PASSED
tests/test_math.py::test_subtract FAILED
tests/test_math.py::test_optional SKIPPED

================================== FAILURES ===================================
_______________________________ test_subtract ________________________________
def test_subtract():
>   assert subtract(5, 2) == 4
E   assert 3 == 4

tests/test_math.py:9: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_math.py::test_subtract - assert 3 == 4
==================== 1 failed, 1 passed, 1 skipped in 0.10s ====================
"""


def write_junit(tmp_path, xml):
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def test_parser_extracts_junit_counts_and_raw_output_failure_details(tmp_path):
    run = DockerRunResult(
        stdout=FAILED_OUTPUT,
        stderr="",
        exit_code=1,
        junit_xml_path=write_junit(
            tmp_path,
            '<testsuite tests="3" failures="1" errors="0" skipped="1" />',
        ),
    )

    result = PytestService().parse(run)

    assert result.total == 3
    assert result.passed == 1
    assert result.failed == 1
    assert result.skipped == 1
    assert result.status == "failed"
    assert result.failures[0].test == "tests/test_math.py::test_subtract"
    assert "assert 3 == 4" in result.failures[0].trace
    assert result.output == FAILED_OUTPUT


def test_parser_handles_testsuites_wrapper_and_all_tests_passing(tmp_path):
    output = "collected 2 items\n\n2 passed in 0.05s\n"

    result = PytestService().parse(
        DockerRunResult(
            stdout=output,
            stderr="",
            exit_code=0,
            junit_xml_path=write_junit(
                tmp_path,
                """<testsuites>
                    <testsuite tests="1" failures="0" errors="0" skipped="0" />
                    <testsuite tests="1" failures="0" errors="0" skipped="0" />
                </testsuites>""",
            ),
        )
    )

    assert result.total == 2
    assert result.passed == 2
    assert result.status == "passed"
    assert result.failures == []


def test_parser_handles_no_tests_collected(tmp_path):
    output = "collected 0 items\n\nno tests ran in 0.01s\n"

    result = PytestService().parse(
        DockerRunResult(
            stdout=output,
            stderr="",
            exit_code=5,
            junit_xml_path=write_junit(
                tmp_path,
                '<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0" /></testsuites>',
            ),
        )
    )

    assert result.total == 0
    assert result.status == "no_tests"


def test_parser_captures_collection_errors_and_trace(tmp_path):
    output = """
============================= test session starts =============================
collected 0 items / 1 error

==================================== ERRORS ====================================
__________________ ERROR collecting tests/test_broken.py ___________________
ImportError while importing test module '/workspace/tests/test_broken.py'.
E   ModuleNotFoundError: No module named 'app.missing'
=========================== short test summary info ===========================
ERROR tests/test_broken.py - ModuleNotFoundError: No module named 'app.missing'
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
=============================== 1 error in 0.10s ===============================
"""

    result = PytestService().parse(
        DockerRunResult(
            stdout=output,
            stderr="",
            exit_code=2,
            junit_xml_path=write_junit(
                tmp_path,
                '<testsuite tests="1" failures="0" errors="1" skipped="0" />',
            ),
        )
    )

    assert result.total == 1
    assert result.errors == 1
    assert result.status == "error"
    assert result.failures[0].file == "tests/test_broken.py"
    assert "ModuleNotFoundError" in result.failures[0].trace


def test_http_status_values_in_output_cannot_corrupt_junit_statistics(tmp_path):
    output = """
tests/test_api.py::test_missing FAILED
Expected status 404
received 200
expected 422
E   AssertionError: 404 failed upstream response; 404 passed through proxy
FAILED tests/test_api.py::test_missing - Expected status 404, received 200
"""
    run = DockerRunResult(
        stdout=output,
        stderr="",
        exit_code=1,
        junit_xml_path=write_junit(
            tmp_path,
            '<testsuites><testsuite tests="10" failures="4" errors="0" skipped="0" /></testsuites>',
        ),
    )

    result = PytestService().parse(run)

    assert result.total == 10
    assert result.passed == 6
    assert result.failed == 4
    assert result.errors == 0
    assert result.skipped == 0
    assert "Expected status 404" in result.output
    assert result.failures[0].error == "Expected status 404, received 200"


def test_parser_falls_back_when_junit_results_are_missing(tmp_path):
    run = DockerRunResult(
        stdout="404 failed, 404 passed",
        stderr="",
        exit_code=1,
        junit_xml_path=tmp_path / "missing.xml",
        job_id="job-missing-junit",
    )

    result = PytestService().parse(run)

    assert result.status == "failed"
    assert result.total == 1
    assert result.failed == 1
    assert result.failures[0].test == "pytest::unparsed_failure"
    assert "could not be parsed" in result.failures[0].error


def test_parser_handles_multiple_failures_without_junit(tmp_path):
    output = """
FAILED tests/test_user.py::test_login - AssertionError
FAILED tests/test_user.py::test_logout - RuntimeError
2 failed, 4 passed in 0.20s
"""
    result = PytestService().parse(
        DockerRunResult(
            stdout=output,
            stderr="",
            exit_code=1,
            junit_xml_path=tmp_path / "missing.xml",
        )
    )

    assert result.status == "failed"
    assert (result.total, result.passed, result.failed) == (6, 4, 2)
    assert [item.test for item in result.failures] == [
        "tests/test_user.py::test_login",
        "tests/test_user.py::test_logout",
    ]


def test_parser_strips_ansi_and_extracts_traceback(tmp_path):
    output = """
=================================== FAILURES ===================================
_______________________________ test_login ________________________________
    assert result == expected
E   AssertionError: assert 'no' == 'yes'
tests/test_user.py:12: AssertionError
\x1b[31mFAILED\x1b[0m tests/test_user.py::test_login - AssertionError
\x1b[31m1 failed\x1b[0m in 0.10s
"""
    result = PytestService().parse(
        DockerRunResult(
            stdout=output,
            stderr="",
            exit_code=1,
            junit_xml_path=tmp_path / "missing.xml",
        )
    )

    assert result.failed == 1
    assert result.failures[0].file == "tests/test_user.py"
    assert "assert 'no' == 'yes'" in result.failures[0].trace
    assert "\x1b" not in result.output


def test_parser_handles_exit_five_without_junit(tmp_path):
    result = PytestService().parse(
        DockerRunResult(
            stdout="collected 0 items\n\nno tests ran in 0.01s\n",
            stderr="",
            exit_code=5,
            junit_xml_path=tmp_path / "missing.xml",
        )
    )

    assert result.status == "no_tests"
    assert result.total == 0
    assert result.failures == []
