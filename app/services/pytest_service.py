import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..core.exceptions import TestExecutionError
from ..core.logging import log_stage, logger
from ..models.responses import TestFailure, TestResult
from .docker_service import DockerRunResult

FAILED_LINE_PATTERN = re.compile(
    r"^\s*FAILED\s+(?P<node>\S+)(?:\s+-\s+(?P<error>.*))?$",
    re.MULTILINE,
)
ERROR_LINE_PATTERN = re.compile(
    r"^\s*ERROR\s+(?P<node>\S+)(?:\s+-\s+(?P<error>.*))?$",
    re.MULTILINE,
)
TRACE_HEADER_PATTERN = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}\s*$", re.MULTILINE)
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SUMMARY_COUNT_PATTERN = re.compile(
    r"(?P<count>\d+)\s+"
    r"(?P<label>failed|passed|skipped|error|errors|xfailed|xpassed)\b",
    re.IGNORECASE,
)
COLLECTED_PATTERN = re.compile(
    r"\bcollected\s+(?P<count>\d+)\s+items?\b", re.IGNORECASE
)
FALLBACK_TRACE_LIMIT = 12_000


class PytestService:
    def parse(self, run: DockerRunResult) -> TestResult:
        output = self._strip_ansi(self._combine_output(run.stdout, run.stderr))
        if run.job_id:
            log_stage(
                run.job_id,
                "failure parsing started exit_code=%s stdout_length=%s stderr_length=%s",
                run.exit_code,
                len(run.stdout),
                len(run.stderr),
            )
        failures = self._parse_failures(output)
        counts = self._counts_with_fallback(run, output, failures)
        total = counts["total"]

        if run.exit_code == 5 or (total == 0 and "no tests ran" in output.lower()):
            status = "no_tests"
        elif run.exit_code == 0:
            status = "passed"
        elif run.exit_code == 1:
            status = "failed"
        else:
            status = "error"

        if run.exit_code == 1 and len(failures) < counts["failed"]:
            missing_count = max(1, counts["failed"] - len(failures))
            failures.append(self._unparsed_failure(output, missing_count))
            counts["failed"] = max(1, counts["failed"])
            counts["total"] = max(counts["total"], counts["failed"])
            counts["passed"] = max(
                0,
                counts["total"]
                - counts["failed"]
                - counts["errors"]
                - counts["skipped"],
            )
            total = counts["total"]

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
                "failure parsing completed total=%s passed=%s failed=%s skipped=%s errors=%s parsed_failure_count=%s",
                test_result.total,
                test_result.passed,
                test_result.failed,
                test_result.skipped,
                test_result.errors,
                len(test_result.failures),
            )
        return test_result

    def _counts_with_fallback(
        self,
        run: DockerRunResult,
        output: str,
        failures: list[TestFailure],
    ) -> dict[str, int]:
        try:
            return self._parse_junit_counts(run.junit_xml_path, job_id=run.job_id)
        except TestExecutionError as exc:
            logger.warning(
                "[FixFlow] job=%s pytest result parser fallback "
                "exit_code=%s exception_type=%s message=%s "
                "stdout_length=%s stderr_length=%s parsed_failure_count=%s",
                run.job_id or "unassigned",
                run.exit_code,
                type(exc).__name__,
                exc.message,
                len(run.stdout),
                len(run.stderr),
                len(failures),
            )
            return self._parse_output_counts(output, run.exit_code, failures)

    @staticmethod
    def _combine_output(stdout: str, stderr: str) -> str:
        if stdout and stderr:
            return f"{stdout.rstrip()}\n\n--- STDERR ---\n{stderr.rstrip()}\n"
        return stdout or stderr

    @staticmethod
    def _strip_ansi(output: str) -> str:
        return ANSI_PATTERN.sub("", output)

    @staticmethod
    def _parse_output_counts(
        output: str,
        exit_code: int,
        failures: list[TestFailure],
    ) -> dict[str, int]:
        counts = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        # Pytest's final summary is the most reliable text fallback. Searching
        # from the bottom avoids treating numbers in assertion messages as counts.
        summary_matches: list[re.Match[str]] = []
        for line in reversed(output.splitlines()):
            matches = list(SUMMARY_COUNT_PATTERN.finditer(line))
            if matches:
                summary_total = sum(int(item.group("count")) for item in matches)
                looks_like_summary = (
                    " in " in line.lower()
                    or "=" in line
                    or summary_total <= max(100, len(failures) * 10)
                )
                if not looks_like_summary:
                    continue
                summary_matches = matches
                break
        for match in summary_matches:
            value = int(match.group("count"))
            label = match.group("label").lower()
            if label == "failed":
                counts["failed"] += value
            elif label == "passed":
                counts["passed"] += value
            elif label in {"error", "errors"}:
                counts["errors"] += value
            elif label in {"skipped", "xfailed"}:
                counts["skipped"] += value
            elif label == "xpassed":
                counts["passed"] += value

        if not summary_matches:
            counts["failed"] = len(failures) if exit_code == 1 else 0
            counts["errors"] = len(failures) if exit_code in {2, 3, 4} else 0

        counted_total = sum(
            counts[key] for key in ("passed", "failed", "errors", "skipped")
        )
        collected = list(COLLECTED_PATTERN.finditer(output))
        collected_total = int(collected[-1].group("count")) if collected else 0
        counts["total"] = (
            counted_total if summary_matches else max(counted_total, collected_total)
        )
        if exit_code == 1 and counts["failed"] == 0:
            counts["failed"] = max(1, len(failures))
            counts["total"] = max(counts["total"], counts["failed"])
        if exit_code == 5:
            return {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        counts["passed"] = max(
            counts["passed"],
            counts["total"] - counts["failed"] - counts["errors"] - counts["skipped"],
        )
        return counts

    @staticmethod
    def _unparsed_failure(output: str, missing_count: int = 1) -> TestFailure:
        bounded = output[-FALLBACK_TRACE_LIMIT:].strip()
        return TestFailure(
            test="pytest::unparsed_failure",
            file="",
            error=(
                f"pytest reported {missing_count} failing test(s), but detailed "
                "failure records could not be parsed. Review the bounded pytest output."
            ),
            trace=bounded,
        )

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
            failed = sum(cls._integer_attribute(suite, "failures") for suite in suites)
            errors = sum(cls._integer_attribute(suite, "errors") for suite in suites)
            skipped = sum(cls._integer_attribute(suite, "skipped") for suite in suites)
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
        suites = [child for child in root if cls._local_name(child.tag) == "testsuite"]
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
        section = output[section_start : summary_start if summary_start != -1 else None]
        headers = list(TRACE_HEADER_PATTERN.finditer(section))
        blocks: list[tuple[str, str]] = []
        for index, header in enumerate(headers):
            start = header.start()
            end = (
                headers[index + 1].start() if index + 1 < len(headers) else len(section)
            )
            blocks.append((header.group(1).strip(), section[start:end].strip()))
        return blocks

    @staticmethod
    def _find_trace(test_name: str, blocks: list[tuple[str, str]]) -> str:
        base_name = test_name.split("[", 1)[0]
        for header, block in blocks:
            if header == test_name or header == base_name or base_name in header:
                return block
        return ""
