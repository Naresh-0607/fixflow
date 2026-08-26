import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..core.exceptions import TestExecutionError
from ..core.logging import log_stage
from ..models.responses import TestFailure, TestResult
from .docker_service import DockerRunResult


FAILED_LINE_PATTERN = re.compile(
    r"^FAILED\s+(?P<node>\S+)(?:\s+-\s+(?P<error>.*))?$",
    re.MULTILINE,
)
ERROR_LINE_PATTERN = re.compile(
    r"^ERROR\s+(?P<node>\S+)(?:\s+-\s+(?P<error>.*))?$",
    re.MULTILINE,
)
TRACE_HEADER_PATTERN = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}\s*$", re.MULTILINE)


class PytestService:
    def parse(self, run: DockerRunResult) -> TestResult:
        output = self._combine_output(run.stdout, run.stderr)
        counts = self._parse_junit_counts(run.junit_xml_path, job_id=run.job_id)
        failures = self._parse_failures(output)
        total = counts["total"]

        if run.exit_code == 5 or (total == 0 and "no tests ran" in output.lower()):
            status = "no_tests"
        elif run.exit_code == 0:
            status = "passed"
        elif run.exit_code == 1:
            status = "failed"
        else:
            status = "error"

        test_result = TestResult(
            total=total,
            passed=counts["passed"],
            failed=counts["failed"],
            skipped=counts["skipped"],
            errors=counts["errors"],
            status=status,
            exit_code=run.exit_code,
            failures=failures,
            stdout=run.stdout,
            stderr=run.stderr,
            output=output,
        )
        if run.job_id:
            log_stage(
                run.job_id,
                "failure parsing completed total=%s passed=%s failed=%s skipped=%s errors=%s",
                test_result.total,
                test_result.passed,
                test_result.failed,
                test_result.skipped,
                test_result.errors,
            )
        return test_result

    @staticmethod
    def _combine_output(stdout: str, stderr: str) -> str:
        if stdout and stderr:
            return f"{stdout.rstrip()}\n\n--- STDERR ---\n{stderr.rstrip()}\n"
        return stdout or stderr

    @classmethod
    def _parse_junit_counts(
        cls,
        junit_xml_path: Path,
        *,
        job_id: str | None,
    ) -> dict[str, int]:
        if not junit_xml_path.is_file():
            raise TestExecutionError(
                "pytest did not produce the expected JUnit XML result file.",
                job_id=job_id,
                details={"junit_xml_path": str(junit_xml_path)},
            )
        try:
            root = ET.parse(junit_xml_path).getroot()
            suites = cls._top_level_suites(root)
            total = sum(cls._integer_attribute(suite, "tests") for suite in suites)
            failed = sum(
                cls._integer_attribute(suite, "failures") for suite in suites
            )
            errors = sum(cls._integer_attribute(suite, "errors") for suite in suites)
            skipped = sum(
                cls._integer_attribute(suite, "skipped") for suite in suites
            )
        except (ET.ParseError, OSError, ValueError) as exc:
            raise TestExecutionError(
                "pytest produced invalid JUnit XML results.",
                job_id=job_id,
                details={"junit_xml_path": str(junit_xml_path)},
            ) from exc

        non_passing = failed + errors + skipped
        if non_passing > total:
            raise TestExecutionError(
                "pytest produced inconsistent JUnit XML counts.",
                job_id=job_id,
                details={
                    "total": total,
                    "failed": failed,
                    "errors": errors,
                    "skipped": skipped,
                },
            )
        return {
            "total": total,
            "passed": total - failed - errors - skipped,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
        }

    @classmethod
    def _top_level_suites(cls, root: ET.Element) -> list[ET.Element]:
        root_name = cls._local_name(root.tag)
        if root_name == "testsuite":
            return [root]
        if root_name != "testsuites":
            raise ValueError("JUnit root must be testsuite or testsuites")
        suites = [
            child for child in root if cls._local_name(child.tag) == "testsuite"
        ]
        if not suites:
            raise ValueError("JUnit testsuites root contains no testsuite elements")
        return suites

    @staticmethod
    def _integer_attribute(element: ET.Element, name: str) -> int:
        value = int(element.attrib.get(name, "0"))
        if value < 0:
            raise ValueError(f"JUnit attribute {name} cannot be negative")
        return value

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _parse_failures(self, output: str) -> list[TestFailure]:
        trace_blocks = self._trace_blocks(output)
        failures: list[TestFailure] = []
        seen: set[str] = set()

        for pattern in (FAILED_LINE_PATTERN, ERROR_LINE_PATTERN):
            for match in pattern.finditer(output):
                node = match.group("node")
                if node in seen:
                    continue
                seen.add(node)
                file_name = node.split("::", 1)[0].replace("\\", "/")
                error = (match.group("error") or "pytest reported a failure").strip()
                test_name = node.rsplit("::", 1)[-1]
                trace = self._find_trace(test_name, trace_blocks)
                failures.append(
                    TestFailure(
                        test=node,
                        file=file_name,
                        error=error,
                        trace=trace,
                    )
                )
        return failures

    @staticmethod
    def _trace_blocks(output: str) -> list[tuple[str, str]]:
        section_starts = [
            start
            for heading in ("FAILURES", "ERRORS")
            if (start := output.find(heading)) != -1
        ]
        summary_start = output.find("short test summary info")
        if not section_starts:
            return []
        section_start = min(section_starts)
        section = output[
            section_start : summary_start if summary_start != -1 else None
        ]
        headers = list(TRACE_HEADER_PATTERN.finditer(section))
        blocks: list[tuple[str, str]] = []
        for index, header in enumerate(headers):
            start = header.start()
            end = headers[index + 1].start() if index + 1 < len(headers) else len(section)
            blocks.append((header.group(1).strip(), section[start:end].strip()))
        return blocks

    @staticmethod
    def _find_trace(test_name: str, blocks: list[tuple[str, str]]) -> str:
        base_name = test_name.split("[", 1)[0]
        for header, block in blocks:
            if header == test_name or header == base_name or base_name in header:
                return block
        return ""
