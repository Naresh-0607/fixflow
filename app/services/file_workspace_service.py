import ast
import asyncio
import builtins
import difflib
import hashlib
import json
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..core.config import Settings
from ..core.exceptions import (
    FileRepairError,
    FileTooLargeError,
    FileWorkspaceNotFound,
    GeminiAnalysisError,
    InvalidFileUpload,
)
from ..llm.gemini_client import GeminiClient
from ..models.file_workspace import (
    CodeMapImport,
    CodeMapSymbol,
    FileCodeMap,
    FileIssue,
)

FILE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._ -]")
WRITE_INTENT_PATTERN = re.compile(
    r"^\s*(?:(?:please\s+)?|(?:can|could|would|will)\s+you\s+|i\s+want\s+you\s+to\s+)"
    r"(change|edit|fix|modify|rewrite|replace|remove|delete|add|implement|patch)\b",
    re.IGNORECASE,
)
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_TYPES = {
    "syntax",
    "logic",
    "runtime_risk",
    "validation",
    "security",
    "concurrency",
    "resource_leak",
    "api_usage",
    "complexity",
    "code_quality",
    "error_handling",
}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
FILE_ANALYSIS_CACHE_VERSION = 2
GEMINI_FILE_TIMEOUT_SECONDS = 10.0
MAX_GEMINI_FINDINGS = 10
MAX_GEMINI_CONTEXT_CHARACTERS = 14_000


@dataclass(frozen=True)
class FileWorkspace:
    file_id: str
    filename: str
    root: Path
    original_path: Path
    current_path: Path
    metadata_path: Path


class FileWorkspaceStore:
    """Owns isolated, expiring storage for untrusted single-file uploads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.workspace_root / "files"

    def create(self, filename: str, content: bytes) -> FileWorkspace:
        self.cleanup_expired()
        safe_name = self._safe_filename(filename)
        if len(content) > self.settings.max_file_upload_bytes:
            raise FileTooLargeError(
                f"The file exceeds the {self.settings.max_file_upload_bytes}-byte limit."
            )
        source = self._decode_source(content)
        self.root.mkdir(parents=True, exist_ok=True)
        file_id = uuid.uuid4().hex
        workspace_root = self.root / file_id
        original = workspace_root / "original" / safe_name
        current = workspace_root / "current" / safe_name
        artifacts = workspace_root / "artifacts"
        original.parent.mkdir(parents=True, exist_ok=False)
        current.parent.mkdir(parents=True, exist_ok=False)
        artifacts.mkdir(parents=True, exist_ok=False)
        original.write_bytes(content)
        current.write_text(source, encoding="utf-8", newline="")
        workspace = FileWorkspace(
            file_id=file_id,
            filename=safe_name,
            root=workspace_root,
            original_path=original,
            current_path=current,
            metadata_path=artifacts / "metadata.json",
        )
        self.write_metadata(
            workspace,
            {
                "file_id": file_id,
                "filename": safe_name,
                "created_at": time.time(),
                "updated_at": time.time(),
                "analyzed": False,
                "repaired": False,
                "issues": [],
            },
        )
        return workspace

    def get(self, file_id: str) -> FileWorkspace:
        self.cleanup_expired()
        if not FILE_ID_PATTERN.fullmatch(file_id):
            raise FileWorkspaceNotFound("The requested file workspace does not exist.")
        root = (self.root / file_id).resolve()
        try:
            root.relative_to(self.root.resolve())
        except ValueError as exc:
            raise FileWorkspaceNotFound(
                "The requested file workspace does not exist."
            ) from exc
        metadata_path = root / "artifacts" / "metadata.json"
        if not metadata_path.is_file():
            raise FileWorkspaceNotFound("The requested file workspace has expired.")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        filename = self._safe_filename(str(metadata.get("filename", "")))
        workspace = FileWorkspace(
            file_id=file_id,
            filename=filename,
            root=root,
            original_path=root / "original" / filename,
            current_path=root / "current" / filename,
            metadata_path=metadata_path,
        )
        if not workspace.original_path.is_file() or not workspace.current_path.is_file():
            raise FileWorkspaceNotFound("The requested file workspace is incomplete.")
        metadata["updated_at"] = time.time()
        self.write_metadata(workspace, metadata)
        return workspace

    @staticmethod
    def read_metadata(workspace: FileWorkspace) -> dict[str, Any]:
        return json.loads(workspace.metadata_path.read_text(encoding="utf-8"))

    @staticmethod
    def write_metadata(workspace: FileWorkspace, metadata: dict[str, Any]) -> None:
        temporary = workspace.metadata_path.with_name(
            f"metadata-{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(workspace.metadata_path)

    def cleanup_expired(self) -> None:
        if not self.root.is_dir():
            return
        cutoff = time.time() - self.settings.file_workspace_ttl_seconds
        for candidate in self.root.iterdir():
            if not candidate.is_dir() or not FILE_ID_PATTERN.fullmatch(candidate.name):
                continue
            metadata_path = candidate / "artifacts" / "metadata.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                last_used = float(metadata.get("updated_at", candidate.stat().st_mtime))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                last_used = candidate.stat().st_mtime
            if last_used < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        value = (filename or "").strip()
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise InvalidFileUpload("The uploaded filename is unsafe.")
        if Path(value).suffix.lower() != ".py":
            raise InvalidFileUpload("Only Python (.py) files are supported.")
        sanitized = SAFE_FILENAME_PATTERN.sub("_", value).strip(" .")
        if not sanitized or sanitized.startswith(".") or len(sanitized) > 180:
            raise InvalidFileUpload("The uploaded filename is unsafe.")
        return sanitized

    @staticmethod
    def _decode_source(content: bytes) -> str:
        if b"\x00" in content:
            raise InvalidFileUpload("Binary files are not supported.")
        try:
            source = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InvalidFileUpload("The file must be UTF-8 text, not binary data.") from exc
        control_count = sum(
            ord(character) < 32 and character not in "\n\r\t\f"
            for character in source
        )
        if control_count > max(1, len(source) // 100):
            raise InvalidFileUpload("Binary files are not supported.")
        return source


class FileAnalysisCache:
    """Content-addressed cache for source-independent analysis artifacts."""

    def __init__(self, settings: Settings) -> None:
        self.root = settings.workspace_root / "files" / ".analysis-cache"

    def read(self, source_hash: str) -> dict[str, Any] | None:
        path = self.root / f"{source_hash}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            return None
        if (
            payload.get("version") != FILE_ANALYSIS_CACHE_VERSION
            or payload.get("sha256") != source_hash
        ):
            return None
        return payload

    def write(self, source_hash: str, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{source_hash}.json"
        temporary = self.root / f".{source_hash}-{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "version": FILE_ANALYSIS_CACHE_VERSION,
                    "sha256": source_hash,
                    **payload,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    def invalidate(self, source_hash: str) -> None:
        try:
            (self.root / f"{source_hash}.json").unlink()
        except FileNotFoundError:
            pass


class ComplexityCounter(ast.NodeVisitor):
    """Small McCabe-style counter that deliberately skips nested functions."""

    def __init__(self) -> None:
        self.value = 1

    def visit_If(self, node: ast.If) -> None:
        self.value += 1
        self.generic_visit(node)

    visit_IfExp = visit_If

    def visit_For(self, node: ast.For) -> None:
        self.value += 1
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self.value += 1
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.value += len(node.handlers)
        if node.orelse:
            self.value += 1
        self.generic_visit(node)

    visit_TryStar = visit_Try

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.value += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.value += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.value += max(0, len(node.cases) - 1)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    @classmethod
    def for_function(cls, node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
        counter = cls()
        for statement in node.body:
            counter.visit(statement)
        return counter.value


class CodeMapBuilder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: list[CodeMapImport] = []
        self.classes: list[CodeMapSymbol] = []
        self.functions: list[CodeMapSymbol] = []
        self.scope: list[str] = []

    def build(self, tree: ast.AST) -> FileCodeMap:
        self.visit(tree)
        return FileCodeMap(
            imports=self.imports,
            classes=self.classes,
            functions=self.functions,
        )

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.append(
            CodeMapImport(
                module="",
                names=[alias.name for alias in node.names],
                line=node.lineno,
            )
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        self.imports.append(
            CodeMapImport(
                module=module,
                names=[alias.name for alias in node.names],
                line=node.lineno,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = ".".join([*self.scope, node.name])
        method_complexities = [
            ComplexityCounter.for_function(child)
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.classes.append(
            CodeMapSymbol(
                name=node.name,
                qualified_name=qualified,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                complexity=max(method_complexities, default=1),
            )
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        qualified = ".".join([*self.scope, node.name])
        self.functions.append(
            CodeMapSymbol(
                name=node.name,
                qualified_name=qualified,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                complexity=ComplexityCounter.for_function(node),
            )
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


class PythonSecurityAnalyzer(ast.NodeVisitor):
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []

    def run(self, tree: ast.AST) -> list[dict[str, Any]]:
        self.visit(tree)
        return self.issues

    def add(self, node: ast.AST, *, severity: str, title: str, description: str, suggestion: str) -> None:
        line = max(1, getattr(node, "lineno", 1))
        self.issues.append(
            {
                "line": line,
                "end_line": max(line, getattr(node, "end_lineno", line)),
                "severity": severity,
                "type": "security",
                "title": title,
                "description": description,
                "suggestion": suggestion,
                "source": "security",
            }
        )

    def visit_Call(self, node: ast.Call) -> None:
        function_name = PythonStaticAnalyzer._call_name(node.func)
        if function_name in {"eval", "exec"}:
            self.add(
                node,
                severity="critical",
                title=f"Unsafe {function_name}() usage",
                description="Executing dynamically supplied code can allow arbitrary code execution.",
                suggestion="Replace dynamic execution with an explicit parser or allow-listed operation.",
            )
        elif function_name in {"os.system", "os.popen"}:
            self.add(
                node,
                severity="high",
                title="Shell command execution",
                description="Building or executing shell commands can enable command injection.",
                suggestion="Use a non-shell API with fixed arguments and validate all inputs.",
            )
        elif function_name in {"subprocess.run", "subprocess.call", "subprocess.Popen"} and any(
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            self.add(
                node,
                severity="high",
                title="Subprocess runs through a shell",
                description="shell=True increases command-injection risk for untrusted input.",
                suggestion="Pass a fixed argument list and keep shell=False.",
            )
        self.generic_visit(node)


class PythonStaticAnalyzer(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self.source = source
        self.issues: list[dict[str, Any]] = []
        self.imports: dict[str, ast.AST] = {}
        self.loaded_names: set[str] = set()
        self.guarded_divisors: set[str] = set()

    def run(self, tree: ast.AST) -> list[dict[str, Any]]:
        self.guarded_divisors = self._find_guarded_divisors(tree)
        self.visit(tree)
        self.issues.extend(self._undefined_name_issues(tree))
        for name, node in self.imports.items():
            if name not in self.loaded_names:
                self.add(
                    node,
                    severity="low",
                    issue_type="code_quality",
                    title=f"Unused import: {name}",
                    description="This imported name is never referenced in the file.",
                    suggestion="Remove the import or use it where intended.",
                )
        return self.issues

    def add(
        self,
        node: ast.AST,
        *,
        severity: str,
        issue_type: str,
        title: str,
        description: str,
        suggestion: str,
    ) -> None:
        self.issues.append(
            {
                "line": max(1, getattr(node, "lineno", 1)),
                "end_line": max(
                    getattr(node, "lineno", 1), getattr(node, "end_lineno", 1)
                ),
                "severity": severity,
                "type": issue_type,
                "title": title,
                "description": description,
                "suggestion": suggestion,
                "source": "ast",
            }
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports[alias.asname or alias.name.split(".")[0]] = node
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.imports[alias.asname or alias.name] = node
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.loaded_names.add(node.id)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and not (
            isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, (int, float))
            and node.right.value != 0
        ) and not (
            isinstance(node.right, ast.Name) and node.right.id in self.guarded_divisors
        ):
            self.add(
                node,
                severity="medium",
                issue_type="runtime_risk",
                title="Possible division by zero",
                description="The divisor is not a known non-zero constant and may raise an exception.",
                suggestion="Validate the divisor before performing the operation.",
            )
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.add(
                node,
                severity="medium",
                issue_type="error_handling",
                title="Bare exception handler",
                description="A bare except catches system-exiting exceptions and can hide defects.",
                suggestion="Catch the narrowest expected exception and handle it explicitly.",
            )
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self.add(
                node,
                severity="medium",
                issue_type="error_handling",
                title="Exception is silently ignored",
                description="Discarding an exception without logging or recovery hides failures.",
                suggestion="Handle, log, or intentionally re-raise the exception.",
            )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self.add(
                    default,
                    severity="medium",
                    issue_type="logic",
                    title="Mutable default argument",
                    description="The same mutable object is reused across calls.",
                    suggestion="Default to None and create the collection inside the function.",
                )
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = PythonStaticAnalyzer._call_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    @staticmethod
    def _find_guarded_divisors(tree: ast.AST) -> set[str]:
        guarded: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.If) or not any(
                isinstance(child, (ast.Raise, ast.Return)) for child in ast.walk(node)
            ):
                continue
            test = node.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                continue
            left, comparator = test.left, test.comparators[0]
            is_zero = lambda value: isinstance(value, ast.Constant) and value.value == 0
            if isinstance(left, ast.Name) and is_zero(comparator):
                guarded.add(left.id)
            elif isinstance(comparator, ast.Name) and is_zero(left):
                guarded.add(comparator.id)
        return guarded

    @staticmethod
    def _target_names(node: ast.AST) -> set[str]:
        names: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
        return names

    @classmethod
    def _undefined_name_issues(cls, tree: ast.AST) -> list[dict[str, Any]]:
        class ScopeCollector(ast.NodeVisitor):
            def __init__(self) -> None:
                self.defined: set[str] = set()
                self.loaded: list[ast.Name] = []

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Load):
                    self.loaded.append(node)
                elif isinstance(node.ctx, ast.Store):
                    self.defined.add(node.id)

            def visit_Import(self, node: ast.Import) -> None:
                self.defined.update(
                    alias.asname or alias.name.split(".")[0] for alias in node.names
                )

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                self.defined.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name != "*"
                )

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.defined.add(node.name)
                for decorator in node.decorator_list:
                    self.visit(decorator)
                for default in [*node.args.defaults, *node.args.kw_defaults]:
                    if default is not None:
                        self.visit(default)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.defined.add(node.name)
                for base in node.bases:
                    self.visit(base)
                for decorator in node.decorator_list:
                    self.visit(decorator)

        builtin_names = set(dir(builtins))
        module_collector = ScopeCollector()
        for statement in getattr(tree, "body", []):
            module_collector.visit(statement)
        module_defined = module_collector.defined
        issues: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        scopes: list[ast.AST] = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        ]
        collectors: list[ScopeCollector] = [module_collector]
        for scope in scopes:
            collector = ScopeCollector()
            collector.defined.update(argument.arg for argument in scope.args.args)
            collector.defined.update(argument.arg for argument in scope.args.posonlyargs)
            collector.defined.update(argument.arg for argument in scope.args.kwonlyargs)
            if scope.args.vararg:
                collector.defined.add(scope.args.vararg.arg)
            if scope.args.kwarg:
                collector.defined.add(scope.args.kwarg.arg)
            if isinstance(scope, ast.Lambda):
                collector.visit(scope.body)
            else:
                for statement in scope.body:
                    collector.visit(statement)
            collectors.append(collector)

        for collector in collectors:
            allowed = collector.defined | module_defined | builtin_names
            for node in collector.loaded:
                if node.id in allowed:
                    continue
                signature = (node.id, node.lineno)
                if signature in seen:
                    continue
                seen.add(signature)
                issues.append(
                    {
                        "line": node.lineno,
                        "end_line": getattr(node, "end_lineno", node.lineno),
                        "severity": "high",
                        "type": "runtime_risk",
                        "title": f"Undefined name: {node.id}",
                        "description": "This name is referenced but is not defined in the visible file scope.",
                        "suggestion": "Define or import the name before it is used.",
                        "source": "ast",
                    }
                )
        return issues


class FileAnalysisEngine:
    def __init__(self, settings: Settings, gemini_client: GeminiClient) -> None:
        self.settings = settings
        self.gemini_client = gemini_client

    def analyze_source(
        self, *, file_id: str, filename: str, source: str
    ) -> tuple[list[FileIssue], str, str | None]:
        """Synchronous compatibility wrapper used by direct service callers."""
        issues, status, error, _ = asyncio.run(
            self.analyze_source_detailed(
                file_id=file_id,
                filename=filename,
                source=source,
            )
        )
        return issues, status, error

    async def analyze_source_detailed(
        self, *, file_id: str, filename: str, source: str
    ) -> tuple[list[FileIssue], str, str | None, FileCodeMap]:
        issues, code_map = await self.analyze_source_local_detailed(
            filename=filename, source=source
        )
        gemini_status = "complete"
        gemini_error = None
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(
                    self.gemini_client.analyze,
                    self._analysis_prompt(filename, source, issues, code_map),
                    job_id=file_id,
                ),
                timeout=GEMINI_FILE_TIMEOUT_SECONDS,
            )
            issues.extend(self._normalize_gemini_issues(raw, source))
        except asyncio.TimeoutError:
            gemini_status = "timed_out"
            gemini_error = "Gemini file review timed out after 10 seconds."
        except GeminiAnalysisError as exc:
            gemini_status = "unavailable"
            gemini_error = exc.message
        except Exception:  # noqa: BLE001 - provider failures must not discard local results
            gemini_status = "unavailable"
            gemini_error = "Gemini file review was unavailable."
        issues = self._deduplicate_and_identify(issues)
        return issues, gemini_status, gemini_error, code_map

    async def analyze_source_local_detailed(
        self, *, filename: str, source: str
    ) -> tuple[list[FileIssue], FileCodeMap]:
        local_groups, code_map = await self._concurrent_local_analysis(source, filename)
        issues = self._deduplicate_and_identify(
            [issue for group in local_groups for issue in group]
        )
        return issues, code_map

    async def apply_ruff_safe_fix(
        self, source: str, filename: str, issue: FileIssue
    ) -> str:
        """Apply and verify a selected Ruff safe fix against the complete current source."""
        if issue.source != "ruff" or not issue.fixable or not issue.rule:
            raise FileRepairError(
                "The selected issue does not have a deterministic Ruff safe fix."
            )
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--no-cache",
                "--fix-only",
                "--select",
                issue.rule,
                "--stdin-filename",
                filename,
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError):
            raise FileRepairError("Ruff is unavailable for the selected safe fix.") from None
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(source.encode("utf-8")), timeout=3.0
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise FileRepairError("Ruff timed out while applying the selected safe fix.")
        if process.returncode not in {0, 1}:
            raise FileRepairError("Ruff could not apply the selected safe fix.")
        try:
            fixed = stdout.decode("utf-8")
            compile(fixed, filename, "exec", ast.PyCF_ONLY_AST)
        except (UnicodeDecodeError, SyntaxError, IndentationError, ValueError, TypeError):
            raise FileRepairError("Ruff produced invalid Python source.") from None
        if fixed == source:
            raise FileRepairError("Ruff produced no safe fix for this issue.")
        remaining = await self._ruff_analysis(fixed, filename)
        selected_message = re.sub(
            r"[^a-z0-9]+", " ", issue.message.casefold()
        ).strip()
        if any(
            candidate.rule == issue.rule
            and re.sub(r"[^a-z0-9]+", " ", candidate.message.casefold()).strip()
            == selected_message
            for candidate in remaining
        ):
            raise FileRepairError("Ruff did not remove the selected issue.")
        return fixed

    @staticmethod
    async def apply_ruff_safe_fixes(source: str, filename: str) -> str:
        """Backward-compatible bulk safe-fix helper."""
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--no-cache",
                "--fix-only",
                "--stdin-filename",
                filename,
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(source.encode("utf-8")), timeout=3.0
            )
            fixed = stdout.decode("utf-8")
            compile(fixed, filename, "exec", ast.PyCF_ONLY_AST)
            return fixed
        except (OSError, UnicodeDecodeError, asyncio.TimeoutError, SyntaxError, ValueError, TypeError):
            return source

    def repair_source(
        self,
        *,
        file_id: str,
        filename: str,
        source: str,
        issue: FileIssue,
    ) -> tuple[str, str | None]:
        raw = self.gemini_client.analyze(
            self._repair_prompt(filename, source, issue), job_id=file_id
        )
        return self.apply_repair_response(
            raw=raw,
            file_id=file_id,
            filename=filename,
            source=source,
            issue=issue,
        )

    def build_repair_prompt(
        self, *, filename: str, source: str, issue: FileIssue
    ) -> str:
        return self._repair_prompt(filename, source, issue)

    def apply_repair_response(
        self,
        *,
        raw: Any,
        file_id: str,
        filename: str,
        source: str,
        issue: FileIssue,
    ) -> tuple[str, str | None]:
        repaired, summary = self._extract_repaired_source(raw, source, issue)
        if "\x00" in repaired:
            raise FileRepairError("The AI provider returned invalid binary content.", job_id=file_id)
        if len(repaired.encode("utf-8")) > self.settings.max_file_upload_bytes:
            raise FileRepairError(
                "The AI provider returned a repair larger than the file size limit.",
                job_id=file_id,
            )
        try:
            compile(repaired, filename, "exec", ast.PyCF_ONLY_AST)
        except (SyntaxError, ValueError, TypeError) as exc:
            raise FileRepairError(
                "The AI provider returned a repair that does not pass Python syntax validation.",
                job_id=file_id,
                details={"line": getattr(exc, "lineno", None)},
            ) from exc
        if repaired == source:
            raise FileRepairError(
                "The AI provider returned a repair that does not change the current source.",
                job_id=file_id,
            )
        return repaired, summary

    def answer_chat(
        self,
        *,
        file_id: str,
        filename: str,
        message: str,
        original: str,
        current: str,
        issues: list[FileIssue],
        diff: str,
    ) -> str:
        if WRITE_INTENT_PATTERN.search(message):
            return (
                "Chat is read-only. Review the findings and use Fix Now to make "
                "changes through FixFlow's validated repair pipeline."
            )
        prompt = f"""You are FixFlow's read-only file assistant.
Answer the user's question using only the supplied single-file context. Never
claim to edit the file and never return replacement code intended to be applied.
Return JSON exactly as {{"answer": "concise explanation"}}.

Filename: {filename}
Question: {message}
Findings: {json.dumps([item.model_dump() for item in issues])}
Original source:\n```python\n{original}\n```
Current source:\n```python\n{current}\n```
Current diff:\n```diff\n{diff}\n```
"""
        raw = self.gemini_client.analyze(prompt, job_id=file_id)
        if isinstance(raw, dict) and isinstance(raw.get("answer"), str):
            return raw["answer"].strip()
        if isinstance(raw, str):
            return raw.strip()
        raise GeminiAnalysisError(
            "Gemini returned an invalid chat response.", job_id=file_id
        )

    async def _concurrent_local_analysis(
        self, source: str, filename: str
    ) -> tuple[list[list[FileIssue]], FileCodeMap]:
        ast_task = asyncio.to_thread(self._ast_analysis, source, filename)
        security_task = asyncio.to_thread(self._security_analysis, source, filename)
        complexity_task = asyncio.to_thread(self._complexity_analysis, source, filename)
        ruff_task = self._ruff_analysis(source, filename)
        ast_result, ruff, security, complexity = await asyncio.gather(
            ast_task,
            ruff_task,
            security_task,
            complexity_task,
        )
        ast_issues, code_map = ast_result
        return [ast_issues, ruff, security, complexity], code_map

    @staticmethod
    def _ast_analysis(
        source: str, filename: str
    ) -> tuple[list[FileIssue], FileCodeMap]:
        try:
            tree = compile(source, filename, "exec", ast.PyCF_ONLY_AST)
        except (SyntaxError, IndentationError) as exc:
            line = max(1, exc.lineno or 1)
            return (
                [
                    FileIssue(
                        id="pending",
                        issue_id="pending",
                        line=line,
                        end_line=max(line, exc.end_lineno or line),
                        severity="critical",
                        type="syntax",
                        title=type(exc).__name__.replace("Error", " error"),
                        description=exc.msg,
                        suggestion="Correct the syntax or indentation before running the file.",
                        source="syntax",
                        rule="PY-SYNTAX",
                        category="syntax",
                        message=exc.msg,
                        fixable=True,
                    )
                ],
                FileCodeMap(),
            )
        raw_issues = PythonStaticAnalyzer(source).run(tree)
        return (
            TypeAdapter(list[FileIssue]).validate_python(
                [{"id": "pending", **issue} for issue in raw_issues]
            ),
            CodeMapBuilder().build(tree),
        )

    @staticmethod
    def _security_analysis(source: str, filename: str) -> list[FileIssue]:
        try:
            tree = compile(source, filename, "exec", ast.PyCF_ONLY_AST)
        except (SyntaxError, IndentationError, ValueError, TypeError):
            return []
        raw = PythonSecurityAnalyzer().run(tree)
        return TypeAdapter(list[FileIssue]).validate_python(
            [{"id": "pending", **issue} for issue in raw]
        )

    @staticmethod
    def _complexity_analysis(source: str, filename: str) -> list[FileIssue]:
        try:
            tree = compile(source, filename, "exec", ast.PyCF_ONLY_AST)
        except (SyntaxError, IndentationError, ValueError, TypeError):
            return []
        findings: list[dict[str, Any]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            complexity = ComplexityCounter.for_function(node)
            if complexity < 11:
                continue
            findings.append(
                {
                    "id": "pending",
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "severity": "high" if complexity >= 21 else "medium",
                    "type": "complexity",
                    "title": f"High cyclomatic complexity ({complexity})",
                    "description": f"Function {node.name} has cyclomatic complexity {complexity}, which makes defects and missed branches more likely.",
                    "suggestion": "Split independent branches into small helpers and simplify nested conditions.",
                    "source": "complexity",
                    "rule": "C901",
                    "category": "complexity",
                    "message": f"Function {node.name} has cyclomatic complexity {complexity}.",
                    "fixable": True,
                }
            )
        return TypeAdapter(list[FileIssue]).validate_python(findings)

    @staticmethod
    async def _ruff_analysis(source: str, filename: str) -> list[FileIssue]:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--no-cache",
                "--output-format=json",
                "--stdin-filename",
                filename,
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError):
            return []
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(source.encode("utf-8")), timeout=3.0
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return []
        try:
            values = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(values, list):
            return []
        findings: list[dict[str, Any]] = []
        for item in values[:MAX_GEMINI_FINDINGS]:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "RUFF")
            location = item.get("location") if isinstance(item.get("location"), dict) else {}
            end_location = item.get("end_location") if isinstance(item.get("end_location"), dict) else {}
            try:
                line = max(1, int(location.get("row", 1)))
                end_line = max(line, int(end_location.get("row", line)))
            except (TypeError, ValueError):
                line = end_line = 1
            if code in {"E999", "F821", "F822", "F823"}:
                severity = "high"
                issue_type = "syntax" if code == "E999" else "runtime_risk"
            elif code == "F401":
                severity = "low"
                issue_type = "code_quality"
            else:
                severity = "medium" if code.startswith("F") else "low"
                issue_type = "code_quality"
            fix = item.get("fix") if isinstance(item.get("fix"), dict) else None
            safely_fixable = bool(fix and fix.get("applicability") == "safe")
            suggestion = (
                "This Ruff finding has a deterministic fix that can be applied safely."
                if safely_fixable
                else "Correct this finding using the referenced Ruff rule."
            )
            message = str(item.get("message") or "Ruff reported a static-analysis issue.")
            findings.append(
                {
                    "id": "pending",
                    "line": line,
                    "end_line": end_line,
                    "severity": severity,
                    "type": issue_type,
                    "title": f"{code}: {message}"[:300],
                    "description": message[:2000],
                    "suggestion": suggestion,
                    "source": "ruff",
                    "rule": code,
                    "category": "lint",
                    "message": message[:2000],
                    "fixable": safely_fixable,
                }
            )
        return TypeAdapter(list[FileIssue]).validate_python(findings)

    @classmethod
    def _analysis_prompt(
        cls,
        filename: str,
        source: str,
        local: list[FileIssue],
        code_map: FileCodeMap,
    ) -> str:
        local_json = json.dumps(
            [
                {
                    "line": item.line,
                    "end_line": item.end_line,
                    "severity": item.severity,
                    "category": item.type,
                    "message": item.title,
                    "source": item.source,
                }
                for item in local[:60]
            ],
            separators=(",", ":"),
        )
        map_json = json.dumps(
            {
                "imports": [item.model_dump() for item in code_map.imports[:30]],
                "classes": [item.model_dump() for item in code_map.classes[:30]],
                "functions": [
                    item.model_dump()
                    for item in sorted(
                        code_map.functions,
                        key=lambda value: (-(value.complexity or 1), value.line),
                    )[:50]
                ],
            },
            separators=(",", ":"),
        )
        context = cls._review_context(source, local, code_map)
        return f"""You are the final static reviewer for one untrusted Python file. Never execute code.
Review ONLY the bounded suspicious sections below for important issues missed by local tools:
logic bugs, runtime bugs, security problems, concurrency issues, resource leaks, and incorrect API usage.
Do not report formatting, naming, imports, lint, complexity, or any issue already listed in local findings.
Return JSON only, with no markdown or commentary, using exactly this shape:
{{"issues":[{{"line":120,"severity":"critical|high|medium|low","category":"logic|runtime_risk|validation|security|concurrency|resource_leak|api_usage|error_handling","message":"Possible issue","suggestion":"Recommended fix"}}]}}
Return at most {MAX_GEMINI_FINDINGS} issues, ordered by importance. Use original file line numbers. If no additional important issue is supported, return {{"issues":[]}}.

Filename: {filename}
AST code map (bounded to the most relevant symbols): {map_json}
Already reported locally (first 60, do not repeat any local-tool category): {local_json}
Bounded source sections (L<number> is the original line):
{context}"""

    @staticmethod
    def _review_context(
        source: str, local: list[FileIssue], code_map: FileCodeMap
    ) -> str:
        lines = source.splitlines()
        if not lines:
            return "(empty file)"
        symbols = sorted(
            [*code_map.functions, *code_map.classes],
            key=lambda item: (item.end_line - item.line, item.line),
        )
        selected: list[tuple[int, int, int]] = []
        important = sorted(local, key=lambda item: (SEVERITY_ORDER[item.severity], item.line))
        for issue in important:
            containing = next(
                (
                    symbol
                    for symbol in symbols
                    if symbol.line <= issue.line <= symbol.end_line
                ),
                None,
            )
            if containing:
                start, end = containing.line, containing.end_line
                if end - start > 120:
                    start, end = max(containing.line, issue.line - 35), min(containing.end_line, issue.end_line + 35)
            else:
                start, end = max(1, issue.line - 5), min(len(lines), issue.end_line + 5)
            selected.append((SEVERITY_ORDER[issue.severity], start, end))

        prioritized_ranges: list[tuple[int, int, int]] = []
        seen_ranges: set[tuple[int, int]] = set()
        for priority, start, end in sorted(selected):
            if (start, end) not in seen_ranges:
                prioritized_ranges.append((priority, start, end))
                seen_ranges.add((start, end))
        for symbol in sorted(
            code_map.functions,
            key=lambda item: (-(item.complexity or 1), item.line),
        ):
            if len(prioritized_ranges) >= 6:
                break
            end = min(symbol.end_line, symbol.line + 119)
            if (symbol.line, end) not in seen_ranges:
                prioritized_ranges.append((4, symbol.line, end))
                seen_ranges.add((symbol.line, end))
        if not prioritized_ranges:
            if len(lines) <= 200:
                prioritized_ranges.append((4, 1, len(lines)))
            else:
                prioritized_ranges.extend(
                    [(4, 1, 60), (4, max(61, len(lines) - 39), len(lines))]
                )

        chunks: list[str] = []
        used = 0
        for _, start, end in prioritized_ranges:
            rendered = "\n".join(
                f"L{number}: {lines[number - 1]}"
                for number in range(start, min(end, len(lines)) + 1)
            )
            header = f"\n--- lines {start}-{min(end, len(lines))} ---\n"
            remaining = MAX_GEMINI_CONTEXT_CHARACTERS - used
            if remaining <= len(header):
                break
            chunk = (header + rendered)[:remaining]
            chunks.append(chunk)
            used += len(chunk)
        return "".join(chunks).strip()

    @classmethod
    def _repair_prompt(cls, filename: str, source: str, issue: FileIssue) -> str:
        context, symbol, context_start, context_end = cls._repair_context(source, issue)
        return f"""Repair exactly one selected issue in the CURRENT editable Python source.
Make the smallest possible change. Do not rewrite the whole file, change unrelated
behavior, or address other findings. The server will apply your replacement to the
complete current source and reject unchanged or syntactically invalid output.
Return JSON only in exactly this shape:
{{"start_line":1,"end_line":1,"replacement":"replacement code","summary":"brief description"}}
The inclusive replacement range must be inside the supplied current-source section.

Filename: {filename}
Current source SHA-256: {hashlib.sha256(source.encode()).hexdigest()}
Selected issue: {json.dumps(issue.model_dump(), separators=(",", ":"))}
Containing symbol: {symbol}
Allowed replacement range: lines {context_start}-{context_end}
Current source section (L<number> is the original line):
{context}"""

    @staticmethod
    def _repair_context(source: str, issue: FileIssue) -> tuple[str, str, int, int]:
        lines = source.splitlines()
        try:
            tree = compile(source, "<current-file>", "exec", ast.PyCF_ONLY_AST)
            code_map = CodeMapBuilder().build(tree)
            symbols = sorted(
                [*code_map.functions, *code_map.classes],
                key=lambda value: (value.end_line - value.line, value.line),
            )
            containing = next(
                (
                    symbol
                    for symbol in symbols
                    if symbol.line <= issue.line <= symbol.end_line
                ),
                None,
            )
        except (SyntaxError, ValueError, TypeError):
            containing = None
        if containing:
            start, end = containing.line, containing.end_line
            symbol_name = containing.qualified_name
            if end - start > 160:
                start = max(containing.line, issue.line - 60)
                end = min(containing.end_line, issue.end_line + 60)
        else:
            start = max(1, issue.line - 12)
            end = min(len(lines), issue.end_line + 12)
            symbol_name = "module scope"
        context = "\n".join(
            f"L{number}: {lines[number - 1]}"
            for number in range(start, min(end, len(lines)) + 1)
        )
        return context, symbol_name, start, end

    @staticmethod
    def _normalize_gemini_issues(raw: Any, source: str) -> list[FileIssue]:
        values = raw.get("issues", raw.get("analysis", [])) if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            raise GeminiAnalysisError(
                "Gemini returned JSON that does not match the file analysis schema."
            )
        line_count = max(1, len(source.splitlines()))
        normalized: list[dict[str, Any]] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "medium")).lower().replace(" ", "_")
            issue_type = str(item.get("type", item.get("category", "logic"))).lower()
            issue_type = issue_type.replace(" ", "_").replace("-", "_")
            if severity not in VALID_SEVERITIES:
                severity = "medium"
            if issue_type not in VALID_TYPES:
                issue_type = "logic"
            try:
                line = min(line_count, max(1, int(item.get("line", 1))))
                end_line = min(
                    line_count, max(line, int(item.get("end_line", line)))
                )
            except (TypeError, ValueError):
                line = end_line = 1
            message = str(
                item.get("message", item.get("title", "Gemini review finding"))
            ).strip()
            title = str(item.get("title", message)).strip()[:300]
            description = str(
                item.get("description", item.get("why", message or "Review this code path."))
            ).strip()[:2000]
            suggestion = str(
                item.get("suggestion", item.get("suggested_fix", "Review and correct the code."))
            ).strip()[:2000]
            normalized.append(
                {
                    "id": "pending",
                    "line": line,
                    "end_line": end_line,
                    "severity": severity,
                    "type": issue_type,
                    "title": title or "Gemini review finding",
                    "description": description or "Review this code path.",
                    "suggestion": suggestion or "Review and correct the code.",
                    "source": "gemini",
                    "rule": str(item.get("rule") or f"AI-{issue_type.upper()}")[:80],
                    "category": issue_type,
                    "message": message[:2000],
                    "fixable": True,
                }
            )
        try:
            return TypeAdapter(list[FileIssue]).validate_python(normalized)
        except ValidationError as exc:
            raise GeminiAnalysisError(
                "Gemini returned JSON that does not match the file analysis schema."
            ) from exc

    @staticmethod
    def _deduplicate_and_identify(issues: list[FileIssue]) -> list[FileIssue]:
        result: list[FileIssue] = []
        seen: set[tuple[Any, ...]] = set()
        ordered = sorted(
            issues,
            key=lambda value: (
                SEVERITY_ORDER[value.severity],
                value.line,
                value.type,
                value.title.casefold(),
            ),
        )
        for item in ordered:
            message = (item.message or item.title).strip()
            category = item.category or ("lint" if item.source == "ruff" else item.type)
            rule = item.rule or FileAnalysisEngine._default_rule(item)
            normalized_title = re.sub(r"[^a-z0-9]+", " ", message.casefold()).strip()
            if item.type == "code_quality" and (
                "unused import" in normalized_title
                or "imported but unused" in item.description.casefold()
            ):
                signature: tuple[Any, ...] = (item.line, "unused_import")
            else:
                signature = (item.source, item.line, category, rule, normalized_title)
            if signature in seen:
                continue
            seen.add(signature)
            identity = f"{item.source}:{rule}:{item.line}:{item.end_line}:{normalized_title}"
            digest = hashlib.sha1(identity.encode()).hexdigest()[:8]
            safe_rule = re.sub(r"[^A-Za-z0-9_-]+", "-", rule).strip("-") or "finding"
            issue_id = f"{item.source}-{safe_rule}-{item.line}-{digest}"
            result.append(
                item.model_copy(
                    update={
                        "issue_id": issue_id,
                        "id": issue_id,
                        "rule": rule,
                        "category": category,
                        "message": message,
                        "fixable": item.fixable or item.source != "ruff",
                    }
                )
            )
        return result

    @staticmethod
    def _default_rule(issue: FileIssue) -> str:
        if issue.source == "syntax":
            return "PY-SYNTAX"
        prefix = {
            "ast": "AST",
            "static": "AST",
            "security": "SEC",
            "complexity": "C901",
            "gemini": "AI",
        }.get(issue.source, issue.source.upper())
        title_slug = re.sub(r"[^A-Za-z0-9]+", "-", issue.title).strip("-").upper()
        return f"{prefix}-{title_slug[:48]}" if prefix != "C901" else prefix

    @staticmethod
    def issue_is_present(selected: FileIssue, findings: list[FileIssue]) -> bool:
        selected_message = re.sub(
            r"[^a-z0-9]+", " ", selected.message.casefold()
        ).strip()
        return any(
            finding.source == selected.source
            and finding.rule == selected.rule
            and re.sub(r"[^a-z0-9]+", " ", finding.message.casefold()).strip()
            == selected_message
            for finding in findings
        )

    @staticmethod
    def _extract_repaired_source(
        raw: Any, current_source: str, issue: FileIssue
    ) -> tuple[str, str | None]:
        if not isinstance(raw, dict):
            raise FileRepairError("The AI provider returned an invalid repair response.")
        container = raw.get("repair") if isinstance(raw.get("repair"), dict) else raw
        replacement = container.get("replacement")
        if isinstance(replacement, str) and {
            "start_line",
            "end_line",
        } <= container.keys():
            replacement = {
                "start_line": container["start_line"],
                "end_line": container["end_line"],
                "code": replacement,
            }
        if not isinstance(replacement, dict) and isinstance(container.get("patch"), dict):
            replacement = container["patch"]
        if isinstance(replacement, dict):
            code = replacement.get("code", replacement.get("replacement_code"))
            try:
                start_line = int(replacement["start_line"])
                end_line = int(replacement["end_line"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FileRepairError("The AI provider returned an invalid replacement range.") from exc
            current_lines = current_source.splitlines(keepends=True)
            _, _, allowed_start, allowed_end = FileAnalysisEngine._repair_context(
                current_source, issue
            )
            if (
                not isinstance(code, str)
                or start_line < 1
                or end_line < start_line
                or end_line > len(current_lines)
                or start_line < allowed_start
                or end_line > allowed_end
            ):
                raise FileRepairError("The AI provider returned an invalid targeted replacement.")
            needs_line_ending = end_line < len(current_lines) or current_source.endswith(
                ("\n", "\r")
            )
            if code and needs_line_ending and not code.endswith(("\n", "\r")):
                code += "\n"
            repaired = "".join(current_lines[: start_line - 1]) + code + "".join(
                current_lines[end_line:]
            )
            summary = container.get("summary")
            return repaired, str(summary).strip() if summary else None
        source = next(
            (
                container.get(key)
                for key in ("fixed_source", "current_source", "source", "code")
                if isinstance(container.get(key), str)
            ),
            None,
        )
        if source is None:
            raise FileRepairError("The AI provider did not return a usable targeted patch.")
        cleaned = source
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:python|py)?\s*", "", cleaned.strip())
            cleaned = re.sub(r"\s*```$", "", cleaned)
        if source.endswith("\n") and not cleaned.endswith("\n"):
            cleaned += "\n"
        summary = container.get("summary")
        return cleaned, str(summary).strip() if summary else None


def unified_diff(filename: str, before: str, after: str) -> tuple[str, int]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )
    diff = "".join(lines)
    changed = sum(
        1
        for line in lines
        if line.startswith(("+", "-"))
        and not line.startswith(("+++", "---"))
    )
    return diff, changed
